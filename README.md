# 白夜交易系统 (Baiye Trading System)

> 🦞 均值回归量化策略 | 多周期 | 高频纸交易验证 | v8.1

---

## 🗂️ 仓库结构

```
killer-trading-system/
├── main_v73.py          # 主引擎 — 实时行情+信号+执行+状态管理
├── config.py            # 全局配置 v8.1 — 所有参数在此修改
├── optimize_params.py   # 参数优化器 — 接入Binance实时数据网格搜索
├── validate_100_v8.py   # 验证框架 — Walk-Forward OOS验证
├── watchdog_v73.sh      # 守护脚本 — 引擎崩溃自动重启
├── start_paper.sh       # 一键启动
│
├── data/
│   ├── state_v73.json   # 实时账户状态 (净值/持仓/完成笔数)
│   ├── trades_v73.jsonl # 历史交易明细
│   └── baiye_v73.pid    # 引擎进程PID
│
├── logs/
│   └── baiye_v73.log    # 完整运行日志
│
└── research/
    ├── optimize_result_v2.json   # v8.0优化结果 (8品种1920组)
    ├── optimize_extra.json       # 新4品种优化结果
    ├── validate_100_v8.json      # v8.1验证结果 (663笔OOS)
    └── ...                       # 历史研究数据
```

---

## ⚡ 快速启动

```bash
# 守护模式启动（推荐）
bash watchdog_v73.sh

# 直接启动
python3 main_v73.py

# 运行验证
python3 validate_100_v8.py

# 参数重新优化
python3 optimize_params.py
```

---

## 📊 策略原理

**核心策略：均值回归（MeanReversion）**

| 维度 | 说明 |
|------|------|
| 信号触发 | 连涨/连跌N根 + 累计涨跌幅 ≥ 阈值 |
| 趋势过滤 | ADX ≥ adx_th（过热才开仓，过滤震荡） |
| 方向过滤 | EMA200方向过滤（顺势回调） |
| 止盈设置 | tp_mult × ATR（per品种优化） |
| 止损设置 | sl_mult × ATR（per品种优化） |
| 追踪止损 | 浮盈 ≥ 0.5ATR 激活，0.4ATR距离追踪 |
| 信号质量 | 6层评分 ≥ 2.0 方可开仓 |
| 仓位管理 | Kelly公式（25%缩减） + WRGuard保护 |

---

## 🏆 v8.1 有效品种（OOS Walk-Forward验证通过）

| 品种 | WR | PF | n | adx_th | tp | sl |
|------|----|----|---|--------|----|----|
| 🔥 SUI | 86.4% | 3.43 | 22 | 25 | 0.6x | 1.5x |
| 🔥 TON | 72.8% | 1.51 | 92 | 15 | 0.8x | 1.5x |
| 🔥 POL | 73.1% | 1.62 | 26 | 25 | 1.2x | 1.5x |
| ✅ SOL | 75.0% | 1.38 | 16 | 15 | 0.8x | 1.5x |
| ✅ DOGE| 63.4% | 1.34 | 71 | 15 | 1.5x | 1.8x |
| ✅ DOT | 60.0% | 1.16 | 20 | 30 | 0.8x | 1.0x |
| ✅ BTC | 75.0% | 1.20 | 12 | 35 | 0.8x | 1.5x |

**7品种组合：WR=70.7% PF=1.51 总PnL=+1.49U**

---

## 🔧 核心参数说明

```python
# config.py 关键参数

MAX_HOLD_BARS = 25          # 最大持仓根数（超时强制平仓）
SIGNAL_MIN_SCORE = 2.0      # 最低开仓评分（6层满分7分）
DYNAMIC_TP_ADX_TH = 30      # ADX超过此值启用动态TP放大
DYNAMIC_TP_MULT = 1.3       # 动态TP放大倍数
TRAILING_STOP_THRESH = 0.5  # 追踪止损激活门槛（ATR）
WR_GUARD_MIN_WR = 0.42      # WRGuard最低胜率（低于暂停开仓）
KELLY_FRACTION = 0.25       # Kelly缩减系数（保守）
RISK_PCT = 0.02             # 每笔风险比例（2%）
```

---

## 📈 当前运行状态

| 指标 | 数值 |
|------|------|
| 运行版本 | v8.4 (2026-05-16) |
| 模式 | paper（纸交易） |
| 初始净值 | 150.0 U |
| 品种数 | 8个（TON/SUI/BTC/SOL/POL/DOT/DOGE/XRP）|
| 守护方式 | watchdog_v73.sh + cron(5min) |

---

## 📋 版本变更日志

详见 [CHANGELOG.md](CHANGELOG.md)

---

## ⚠️ 免责声明

本系统仅用于学习研究，纸交易模式不涉及真实资金。历史回测和纸交易结果不代表未来实盘表现。

---

*GitHub: https://github.com/Siyebai/killer-trading-system*
