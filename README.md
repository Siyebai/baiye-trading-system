# 白夜交易系统 v7.3

> **Baiye Trading System** — 高性能加密货币量化交易引擎  
> 策略：均值回归 + 多周期信号过滤 + Kelly仓位 + EMA200趋势对齐  
> 状态：纸交易验证中 | 目标：胜率≥55%，月均+8%

---

## 📁 仓库结构

```
killer-trading-system/
├── config.py              # 核心配置（所有参数集中管理）
├── main_v73.py            # 主引擎 v7.3（当前稳定版）
├── main_v72.py            # 主引擎 v7.2（存档）
├── main_v71.py            # 主引擎 v7.1（存档）
│
├── data/                  # 真实历史数据（来自 Binance 合约 API）
│   ├── BTCUSDT_15m_180d.csv    # BTC 15分钟 180天
│   ├── BTCUSDT_5m_180d.csv     # BTC 5分钟 180天
│   ├── ETHUSDT_15m_180d.csv
│   ├── ETHUSDT_5m_180d.csv
│   ├── SOLUSDT_15m_180d.csv
│   ├── SOLUSDT_5m_180d.csv
│   ├── BNBUSDT_15m_180d.csv
│   ├── BNBUSDT_5m_180d.csv
│   ├── LINKUSDT_15m_180d.csv
│   ├── LINKUSDT_5m_180d.csv
│   ├── SUIUSDT_15m_180d.csv
│   ├── SUIUSDT_5m_180d.csv
│   ├── POLUSDT_15m_180d.csv
│   ├── POLUSDT_5m_180d.csv
│   ├── DOTUSDT_15m_180d.csv
│   └── DOTUSDT_5m_180d.csv
│
├── engine/                # 回测引擎
│   └── backtest_engine_v3.py
│
├── logs/                  # 运行日志
├── research/              # 回测结果 JSON（180天验证数据）
├── guardian_scripts/      # 守护脚本（自动重启）
│   └── start_baiye_v73.sh
│
├── docs/                  # 文档
│   ├── BACKTEST_RESULTS.md
│   └── MONTHLY_STABILITY.md
│
├── AGENT_GUIDE.md         # 智能体使用指南（详细）
├── CHANGELOG.md           # 版本变更记录
└── requirements.txt       # 依赖列表
```

---

## 🚀 快速启动

### 环境要求
```bash
Python 3.10+
pip install -r requirements.txt
```

### 纸交易模式（推荐新智能体使用）
```bash
# 1. 克隆仓库
git clone https://github.com/Siyebai/baiye-trading-system.git
cd baiye-trading-system

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置 API（config.py 第18-19行，纸交易不需要真实资金）
#    BINANCE_API_KEY = "your_key"
#    BINANCE_SECRET  = "your_secret"

# 4. 确认运行模式为 paper
#    config.py: RUN_MODE = "paper"

# 5. 启动引擎
python3 main_v73.py

# 6. 或使用 Guardian 守护（推荐，自动重启）
bash guardian_scripts/start_baiye_v73.sh &
```

### 查看实时日志
```bash
tail -f logs/baiye_v73.log
```

---

## ⚙️ 核心配置说明（config.py）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `RUN_MODE` | `"paper"` | 运行模式：`paper`/`shadow`/`live` |
| `INITIAL_EQUITY` | `150.0` | 初始资金（USDT） |
| `RISK_PCT` | `0.02` | 单笔风险 2% |
| `FEE` | `0.0002` | Maker手续费率（0.02%） |
| `SIGNAL_MIN_SCORE` | `2.5` | 信号最低评分（0-10） |
| `KELLY_FRACTION` | `0.25` | Kelly系数（保守25%） |
| `WR_GUARD_MIN_WR` | `0.42` | WRGuard触发阈值（WR<42%降仓） |
| `DAILY_LOSS_PCT` | `0.06` | 日熔断（亏损6%暂停） |
| `TRAILING_STOP_ENABLED` | `True` | 追踪止损开关 |
| `TRAILING_STOP_THRESH` | `0.6` | 浮盈达0.6ATR启动追踪 |
| `POLL_SECS` | `30` | 扫描间隔（秒） |

---

## 📊 品种参数表

| 品种 | 连涨根(sc) | 连跌根(lc) | 幅度(ccp) | ADX门槛 | TP倍数 | SL倍数 | 允许做多 |
|------|-----------|-----------|----------|---------|--------|--------|---------|
| BTCUSDT | 4 | 5 | 0.20% | 20 | 1.8× | 1.4× | ✅ |
| ETHUSDT | 5 | 4 | 0.15% | 18 | 2.0× | 1.5× | ✅ |
| SOLUSDT | 5 | 4 | 0.15% | 30 | 2.2× | 1.6× | ✅ |
| BNBUSDT | 5 | 6 | 0.15% | 15 | 2.0× | 1.5× | ❌ |
| LINKUSDT | 7 | 4 | 0.25% | 25 | 2.5× | 1.5× | ✅ |
| SUIUSDT | 7 | 6 | 0.08% | 25 | 2.0× | 1.5× | ✅ |
| POLUSDT | 5 | 4 | 0.15% | 25 | 2.0× | 1.5× | ❌ |
| DOTUSDT | 5 | 4 | 0.15% | 20 | 2.2× | 1.5× | ✅ |

---

## 📈 策略逻辑

### 核心信号（均值回归）
- **SHORT信号**：连涨 ≥ sc 根 + 累涨 ≥ ccp + close **< EMA200**（趋势对齐）
- **LONG信号**：连跌 ≥ lc 根 + 累跌 ≥ ccp + close **> EMA200**

### 多周期评分系统（0-10分）
```
ADX强度      0~3分   (ADX≥40: 3分, ≥30: 2分, ≥20: 1分)
60m趋势对齐  0~1.5分 (1h EMA200方向一致)
5m共振信号   0~1.5分 (5m同方向信号加分，反向扣0.5分)
RR比         0~2分   (RR≥2.0: 2分, ≥1.5: 1分)
RSI极值      0~1分   (SHORT时RSI≥65, LONG时RSI≤35)
动态TP       0~1分   (ADX≥35时TP×1.5)
```
最低开仓评分：**≥ 2.5分**

### 风控层
1. **Kelly仓位**：基于历史WR动态调整风险金额
2. **WRGuard**：近30笔WR < 42% → 降仓50%；WR < 25% → 暂停开仓
3. **追踪止损**：浮盈 ≥ 0.6ATR → 移至保本位
4. **相关性控制**：BTC/ETH/SOL/DOT 同方向最多2仓
5. **日熔断**：当日亏损 ≥ 6% → 暂停至次日
6. **资金费率过滤**：|费率| ≥ 0.1% 或距结算 < 30min → 跳过

---

## 📂 验证数据说明

### data/ 目录 — 真实历史数据
- **数据来源**：Binance 永续合约 API（`/fapi/v1/klines`）
- **覆盖时间**：2025-11-14 ~ 2026-05-13（约180天）
- **格式**：CSV，列名 `ts,open,high,low,close,vol`
- **周期**：15m（17280行/品种）、5m（51840行/品种）

### research/ 目录 — 回测结果
| 文件 | 说明 |
|------|------|
| `180d_backtest_results.json` | 180天全量回测结果 |
| `fusion_final_report.json` | 融合策略最终报告 |
| `walkforward_results.json` | Walk-Forward验证（防过拟合） |
| `fee_corrected_results.json` | 手续费修正后结果（Maker 0.02%） |
| `param_refit_v2.json` | 参数重新拟合结果 |

---

## 🤖 智能体使用指南

详见 [AGENT_GUIDE.md](./AGENT_GUIDE.md)

### 快速任务分配
| 任务 | 目标文件 | 说明 |
|------|---------|------|
| Bug审计 | `main_v73.py` | 检查边界条件、数据类型、异常处理 |
| 性能分析 | `main_v73.py` + `config.py` | 并发优化、内存管理 |
| 回测验证 | `engine/backtest_engine_v3.py` + `data/` | 用真实数据验证策略 |
| 参数优化 | `config.py` `SYM_CFG` | 网格搜索最优参数 |
| 稳定性测试 | `main_v73.py` | 长时间运行压力测试 |

---

## 📋 版本历史

| 版本 | 要点 |
|------|------|
| v7.3 | 并发K线拉取、TF扫描修复、EMA200方向过滤、DOTUSDT、Guardian |
| v7.2 | Kelly仓位、WRGuard、追踪止损、多周期评分、相关性控制 |
| v7.1 | Wilder指标修复、原子写入、日熔断、冷却期 |
| v7.0 | 8品种、Maker费率、动态TP |

---

## ⚠️ 免责声明

本系统仅用于研究和纸交易验证，不构成投资建议。实盘交易存在资金损失风险，使用前请充分理解策略逻辑。
