# 日元套息交易风险监控系统

每天自动推送风险报告到Discord，监控套息平仓的早期信号。

## 监控指标

| 指标 | 来源 | 触发阈值 |
|------|------|---------|
| AUD/JPY 日变化 | Yahoo Finance | 单日跌幅 > 1.5% |
| VIX 恐慌指数 | Yahoo Finance | 单日涨幅 > 15% 或绝对值 > 25 |
| USD/JPY 波动 | Yahoo Finance | 单日波动 > 1.5% |
| USD/JPY 历史波动率 | Yahoo Finance | 20日年化 > 12% |
| MOVE 债市波动率 | FRED API | 绝对值 > 100 |
| CFTC 日元期货持仓 | FRED API | 净空头 > 10万张 |

## 快速部署

### 1. 克隆到你的 GitHub 仓库

把 `monitor.py` 放在仓库根目录，`.github/workflows/yen_monitor.yml` 放在对应目录。

### 2. 配置 Secrets

在 GitHub 仓库 → Settings → Secrets and variables → Actions，添加：

| Secret 名称 | 说明 |
|-------------|------|
| `DISCORD_WEBHOOK_URL` | 你的Discord频道Webhook地址 |
| `FRED_API_KEY` | 免费申请：https://fred.stlouisfed.org/docs/api/api_key.html |

> FRED API Key 免费，注册后即可获得，用于获取 MOVE 指数和 CFTC 数据。

### 3. 运行时间

- 每个交易日 **08:30 SGT**（早盘前看昨夜美股收盘情况）
- 每个交易日 **21:00 SGT**（美股开盘后30分钟）
- 支持在 GitHub Actions 页面手动触发

### 4. 本地测试

```bash
pip install -r requirements.txt

export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/your_webhook"
export FRED_API_KEY="your_fred_api_key"

python monitor.py
```

## 如何解读报告

### 风险评分
- **0-29** 🟢 正常：套息交易环境稳定
- **30-59** 🟡 警戒：开始出现压力信号，关注持仓
- **60+**  🔴 高危：套息平仓可能正在发生，AI算力股面临无差别抛售

### 核心组合信号
**AUD/JPY 跌 >1.5% + VIX 单日涨 >15%** 同时触发：
- 套息平仓大概率正在发生
- 接下来 24-48 小时 NVDA/MRVL/CRDO 等高贝塔成长股面临流动性冲击
- 套息引发的闪崩往往 V 形反弹，**不建议恐慌止损**

## 自定义阈值

修改 `monitor.py` 顶部的 `THRESHOLDS` 字典：

```python
THRESHOLDS = {
    "audjpy_daily_drop_pct": -1.5,   # 调低绝对值 = 更敏感
    "vix_daily_rise_pct": 15.0,      # 调低 = 更敏感
    ...
}
```
