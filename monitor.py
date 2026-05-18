"""
日元套息交易风险监控系统
每天定时运行，推送结构化报告到Discord

数据来源：
- yfinance: AUD/JPY, USD/JPY, VIX, 日本国债ETF
- FRED API: MOVE指数替代指标
- CFTC: 日元期货持仓（每周五更新）
"""

import os
import json
import csv
import io
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
import yfinance as yf

# ============================================================
# 配置区 - 修改这里
# ============================================================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")  # 免费申请: https://fred.stlouisfed.org/docs/api/api_key.html

# 报警阈值
THRESHOLDS = {
    "audjpy_daily_drop_pct": -1.5,      # AUD/JPY单日跌幅触发阈值（%）
    "usdjpy_daily_move_pct": 1.5,        # USD/JPY单日波动触发阈值（%，绝对值）
    "vix_daily_rise_pct": 15.0,          # VIX单日涨幅触发阈值（%）
    "vix_level_warning": 25.0,           # VIX绝对水平警戒线
    "vix_level_danger": 35.0,            # VIX绝对水平危险线
    "move_level_warning": 100.0,         # MOVE指数警戒线
    "move_level_danger": 130.0,          # MOVE指数危险线
    "usdjpy_implied_vol_warning": 12.0,  # USD/JPY隐波警戒（近似用历史波动率）
}

# ============================================================
# 数据获取函数
# ============================================================

def fetch_price_data():
    """获取价格数据：AUD/JPY, USD/JPY, VIX"""
    tickers = {
        "AUDJPY": "AUDJPY=X",
        "USDJPY": "USDJPY=X",
        "VIX":    "^VIX",
        "JGB":    "2621.T",   # 日本国债ETF（iShares 20年期日债，东京上市）
    }
    
    result = {}
    for name, ticker in tickers.items():
        try:
            data = yf.Ticker(ticker)
            hist = data.history(period="5d", interval="1d")
            if len(hist) >= 2:
                latest = hist["Close"].iloc[-1]
                prev   = hist["Close"].iloc[-2]
                change_pct = (latest - prev) / prev * 100
                result[name] = {
                    "current": round(latest, 4),
                    "prev":    round(prev, 4),
                    "change_pct": round(change_pct, 2),
                    "5d_high": round(hist["High"].max(), 4),
                    "5d_low":  round(hist["Low"].min(), 4),
                }
            else:
                result[name] = None
        except Exception as e:
            result[name] = {"error": str(e)}
    
    return result


def _fred_observations(series_id, limit=5):
    if not FRED_API_KEY:
        return None, "未配置FRED_API_KEY"

    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    }

    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            return None, f"FRED {resp.status_code}: {resp.text[:500]}"


        data = resp.json()
        if "error_message" in data:
            return None, f"FRED: {data['error_message']}"

        obs = [
            item for item in data.get("observations", [])
            if item.get("value") not in (None, ".")
        ]
        return obs, None
    except Exception as e:
        return None, f"FRED请求异常: {e}"


def _latest_pair_from_yfinance(ticker, period="10d"):
    data = yf.Ticker(ticker)
    hist = data.history(period=period, interval="1d")
    if len(hist) < 1:
        raise ValueError(f"{ticker} 无可用行情")

    closes = hist["Close"].dropna()
    if len(closes) < 1:
        raise ValueError(f"{ticker} 收盘价为空")

    latest = closes.iloc[-1]
    prev = closes.iloc[-2] if len(closes) >= 2 else latest
    latest_date = closes.index[-1].strftime("%Y-%m-%d")
    return float(latest), float(prev), latest_date


def fetch_move_index():
    """优先通过Yahoo Finance获取MOVE指数，失败时再尝试FRED并保留真实错误。"""
    errors = []

    try:
        val, val_prev, date = _latest_pair_from_yfinance("^MOVE")
        return {
            "value": round(val, 2),
            "prev": round(val_prev, 2),
            "change": round(val - val_prev, 2),
            "date": date,
            "source": "Yahoo Finance ^MOVE",
        }
    except Exception as e:
        errors.append(f"Yahoo ^MOVE: {e}")

    obs, err = _fred_observations("ICEMOVE")
    if obs:
        latest = obs[0]
        prev = obs[1] if len(obs) > 1 else obs[0]
        val = float(latest["value"])
        val_prev = float(prev["value"])
        return {
            "value": round(val, 2),
            "prev": round(val_prev, 2),
            "change": round(val - val_prev, 2),
            "date": latest["date"],
            "source": "FRED ICEMOVE",
        }
    if err:
        errors.append(err)

    return {
        "error": "；".join(errors),
        "value": None,
    }


def _cftc_int(row, *names):
    for name in names:
        raw = row.get(name)
        if raw not in (None, ""):
            return int(float(str(raw).replace(",", "").strip()))
    raise KeyError(f"缺少字段: {', '.join(names)}")


def _fetch_cftc_current_legacy_jpy():
    url = "https://www.cftc.gov/dea/newcot/deafut.txt"
    resp = requests.get(url, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f"CFTC {resp.status_code}: {resp.text[:300]}")

    text = resp.text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise RuntimeError("CFTC返回内容没有表头")

    for row in reader:
        market = (
            row.get("Market_and_Exchange_Names")
            or row.get("Market and Exchange Names")
            or row.get("Market_and_Exchange_Name")
            or ""
        ).upper()
        if "JAPANESE YEN" not in market:
            continue

        long_pos = _cftc_int(row, "NonComm_Positions_Long_All", "Noncommercial Positions-Long (All)")
        short_pos = _cftc_int(row, "NonComm_Positions_Short_All", "Noncommercial Positions-Short (All)")
        long_change = _cftc_int(row, "Change_in_NonComm_Long_All", "Change in Noncommercial-Long (All)")
        short_change = _cftc_int(row, "Change_in_NonComm_Short_All", "Change in Noncommercial-Short (All)")
        net_position = long_pos - short_pos
        weekly_change = long_change - short_change
        date = (
            row.get("As_of_Date_In_Form_YYMMDD")
            or row.get("Report_Date_as_YYYY-MM-DD")
            or row.get("As of Date in Form YYMMDD")
            or "N/A"
        )

        return {
            "net_position": net_position,
            "prev_position": net_position - weekly_change,
            "change": weekly_change,
            "date": date,
            "is_net_short": net_position < 0,
            "source": "CFTC Legacy Futures Only",
        }

    raise RuntimeError("CFTC当前报告中未找到Japanese Yen行")


def fetch_cftc_jpy():
    """优先直接读取CFTC当前报告；失败时再尝试旧FRED series并保留真实错误。"""
    errors = []


    try:
        return _fetch_cftc_current_legacy_jpy()
    except Exception as e:
        errors.append(f"CFTC: {e}")

    obs, err = _fred_observations("JPYNTPOSNI")
    if obs:
        latest = obs[0]
        prev = obs[1] if len(obs) > 1 else obs[0]
        val = float(latest["value"])
        val_prev = float(prev["value"])
        return {
            "net_position": int(val),
            "prev_position": int(val_prev),
            "change": int(val - val_prev),
            "date": latest["date"],
            "is_net_short": val < 0,
            "source": "FRED JPYNTPOSNI",
        }
    if err:
        errors.append(err)

    return {
        "error": "；".join(errors),
        "value": None,
    }

def compute_usdjpy_hist_vol(days=20):
    """计算USD/JPY近20日历史波动率作为隐波的近似"""
    try:
        data = yf.Ticker("USDJPY=X")
        hist = data.history(period="30d", interval="1d")
        if len(hist) >= days:
            returns = hist["Close"].pct_change().dropna()
            vol_daily = returns.tail(days).std()
            vol_annualized = vol_daily * (252 ** 0.5) * 100
            return round(vol_annualized, 2)
    except Exception as e:
        print(f"USD/JPY历史波动率计算失败: {e}")
    return None


# ============================================================
# 风险评分
# ============================================================

def compute_risk_score(prices, move, cftc, usdjpy_vol):
    """
    综合风险评分 0-100
    返回: (score, level, triggered_alerts)
    """
    score = 0
    alerts = []

    # --- AUD/JPY ---
    if prices.get("AUDJPY") and "change_pct" in prices["AUDJPY"]:
        chg = prices["AUDJPY"]["change_pct"]
        if chg <= THRESHOLDS["audjpy_daily_drop_pct"]:
            pts = min(30, int(abs(chg) * 10))
            score += pts
            alerts.append(f"🔴 AUD/JPY 单日跌 {chg:.2f}%（阈值 {THRESHOLDS['audjpy_daily_drop_pct']}%）")
        elif chg <= 0:
            score += 5

    # --- VIX ---
    if prices.get("VIX") and "current" in prices["VIX"]:
        vix = prices["VIX"]["current"]
        vix_chg = prices["VIX"]["change_pct"]
        
        if vix >= THRESHOLDS["vix_level_danger"]:
            score += 30
            alerts.append(f"🔴 VIX={vix:.1f}，超过危险线 {THRESHOLDS['vix_level_danger']}")
        elif vix >= THRESHOLDS["vix_level_warning"]:
            score += 15
            alerts.append(f"🟡 VIX={vix:.1f}，超过警戒线 {THRESHOLDS['vix_level_warning']}")
        
        if vix_chg >= THRESHOLDS["vix_daily_rise_pct"]:
            score += 20
            alerts.append(f"🔴 VIX 单日暴涨 {vix_chg:.1f}%（阈值 {THRESHOLDS['vix_daily_rise_pct']}%）")

    # --- USD/JPY 波动 ---
    if prices.get("USDJPY") and "change_pct" in prices["USDJPY"]:
        chg = abs(prices["USDJPY"]["change_pct"])
        if chg >= THRESHOLDS["usdjpy_daily_move_pct"]:
            score += 15
            alerts.append(f"🟡 USD/JPY 单日波动 {chg:.2f}%（阈值 {THRESHOLDS['usdjpy_daily_move_pct']}%）")

    # --- MOVE指数 ---
    if move and move.get("value"):
        mv = move["value"]
        if mv >= THRESHOLDS["move_level_danger"]:
            score += 20
            alerts.append(f"🔴 MOVE指数={mv}，超过危险线 {THRESHOLDS['move_level_danger']}")
        elif mv >= THRESHOLDS["move_level_warning"]:
            score += 10
            alerts.append(f"🟡 MOVE指数={mv}，超过警戒线 {THRESHOLDS['move_level_warning']}")

    # --- USD/JPY 历史波动率 ---
    if usdjpy_vol and usdjpy_vol >= THRESHOLDS["usdjpy_implied_vol_warning"]:
        score += 10
        alerts.append(f"🟡 USD/JPY 20日年化波动率={usdjpy_vol}%（警戒 {THRESHOLDS['usdjpy_implied_vol_warning']}%）")

    # --- CFTC 持仓极端拥挤 ---
    if cftc and cftc.get("net_position") is not None:
        pos = cftc["net_position"]
        if pos < -100000:
            score += 10
            alerts.append(f"🟡 CFTC日元净空头极端拥挤：{pos:,} 张合约")

    score = min(score, 100)

    if score >= 60:
        level = "🔴 高危"
    elif score >= 30:
        level = "🟡 警戒"
    else:
        level = "🟢 正常"

    return score, level, alerts


# ============================================================
# Discord 消息构建
# ============================================================

def build_discord_message(prices, move, cftc, usdjpy_vol, score, level, alerts):
    """构建Discord embed消息"""
    
    now_sg = datetime.now(ZoneInfo("Asia/Singapore"))
    now_str = now_sg.strftime("%Y-%m-%d %H:%M SGT")

    def fmt_change(val, suffix=""):
        if val is None:
            return "N/A"
        arrow = "▲" if val > 0 else "▼" if val < 0 else "─"
        return f"{arrow} {val:+.2f}{suffix}"

    def fmt_price(d, key="current"):
        if not d or key not in d:
            return "N/A"
        return f"{d[key]}"

    def fred_unavailable_text(data, default_note):
        if data and data.get("error"):
            return f"FRED请求失败：{data['error']}"
        if data and data.get("note"):
            return data["note"]
        return default_note

    # 主标题颜色
    color_map = {"🔴 高危": 0xFF0000, "🟡 警戒": 0xFFA500, "🟢 正常": 0x00AA00}
    color = color_map.get(level, 0x888888)

    # 字段组装
    fields = []

    # AUD/JPY
    audjpy = prices.get("AUDJPY", {})
    fields.append({
        "name": "🇦🇺/🇯🇵 AUD/JPY",
        "value": (
            f"现价：**{fmt_price(audjpy)}**\n"
            f"日变化：{fmt_change(audjpy.get('change_pct'), '%')}\n"
            f"5日区间：{audjpy.get('5d_low','N/A')} – {audjpy.get('5d_high','N/A')}"
        ),
        "inline": True
    })

    # USD/JPY
    usdjpy = prices.get("USDJPY", {})
    fields.append({
        "name": "🇺🇸/🇯🇵 USD/JPY",
        "value": (
            f"现价：**{fmt_price(usdjpy)}**\n"
            f"日变化：{fmt_change(usdjpy.get('change_pct'), '%')}\n"
            f"20日历史波动率：{usdjpy_vol or 'N/A'}%"
        ),
        "inline": True
    })

    # VIX
    vix = prices.get("VIX", {})
    fields.append({
        "name": "😱 VIX 恐慌指数",
        "value": (
            f"现价：**{fmt_price(vix)}**\n"
            f"日变化：{fmt_change(vix.get('change_pct'), '%')}\n"
            f"警戒线：{THRESHOLDS['vix_level_warning']} / 危险线：{THRESHOLDS['vix_level_danger']}"
        ),
        "inline": True
    })

    # MOVE指数
    if move and move.get("value"):
        fields.append({
            "name": "📊 MOVE 债市波动率",
            "value": (
                f"最新：**{move['value']}**（{move.get('date','N/A')}）\n"
                f"日变化：{fmt_change(move.get('change'))}\n"
                f"警戒线：{THRESHOLDS['move_level_warning']} / 危险线：{THRESHOLDS['move_level_danger']}"
            ),
            "inline": True
        })
    else:
        fields.append({
            "name": "📊 MOVE 债市波动率",
            "value": fred_unavailable_text(move, "数据不可用（需配置FRED_API_KEY）"),
            "inline": True
        })

    # CFTC
    if cftc and cftc.get("net_position") is not None:
        pos = cftc["net_position"]
        chg = cftc.get("change", 0)
        crowded = "⚠️ 极度拥挤" if pos < -100000 else ("偏拥挤" if pos < -50000 else "正常")
        fields.append({
            "name": "📋 CFTC 日元净持仓",
            "value": (
                f"净仓位：**{pos:,}** 张\n"
                f"周变化：{fmt_change(chg)} 张\n"
                f"拥挤程度：{crowded}\n"
                f"数据日期：{cftc.get('date','N/A')}"
            ),
            "inline": True
        })
    else:
        fields.append({
            "name": "📋 CFTC 日元净持仓",
            "value": fred_unavailable_text(cftc, "数据不可用（需配置FRED_API_KEY）"),
            "inline": True
        })

    # 触发的报警
    if alerts:
        alert_text = "\n".join(alerts)
    else:
        alert_text = "✅ 无触发报警，市场平稳"

    fields.append({
        "name": f"⚡ 触发信号（风险评分 {score}/100）",
        "value": alert_text,
        "inline": False
    })

    # 解读提示
    fields.append({
        "name": "📌 操作提示",
        "value": (
            "**套息平仓信号组合：** AUD/JPY跌 >1.5% + VIX单日涨 >15%\n"
            "→ 高贝塔AI算力股（NVDA/MRVL/CRDO等）面临无差别抛售风险\n"
            "→ 套息引发闪崩往往V形反弹，避免恐慌止损"
        ),
        "inline": False
    })

    embed = {
        "title": f"日元套息交易风险日报 {level}",
        "description": f"综合风险评分：**{score}/100** | {now_str}",
        "color": color,
        "fields": fields,
        "footer": {"text": "数据来源：Yahoo Finance / FRED / CFTC | 子金研究 风控系统"},
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }

    return {"embeds": [embed]}


def send_to_discord(payload):
    """发送到Discord"""
    if not DISCORD_WEBHOOK_URL:
        print("❌ 未配置 DISCORD_WEBHOOK_URL")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return False
    
    resp = requests.post(
        DISCORD_WEBHOOK_URL,
        json=payload,
        timeout=10
    )
    if resp.status_code in (200, 204):
        print("✅ Discord推送成功")
        return True
    else:
        print(f"❌ Discord推送失败: {resp.status_code} {resp.text}")
        return False


# ============================================================
# 主函数
# ============================================================

def run():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始拉取数据...")

    prices     = fetch_price_data()
    move       = fetch_move_index()
    cftc       = fetch_cftc_jpy()
    usdjpy_vol = compute_usdjpy_hist_vol()

    print(f"价格数据: {json.dumps(prices, ensure_ascii=False)}")
    print(f"MOVE: {move}")
    print(f"CFTC: {cftc}")
    print(f"USD/JPY 历史波动率: {usdjpy_vol}%")

    score, level, alerts = compute_risk_score(prices, move, cftc, usdjpy_vol)
    print(f"风险评分: {score}/100 ({level})")
    print(f"触发报警: {alerts}")

    payload = build_discord_message(prices, move, cftc, usdjpy_vol, score, level, alerts)
    send_to_discord(payload)


if __name__ == "__main__":
    run()
