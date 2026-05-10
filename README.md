# 白夜交易系统 (Baiye Trading System)

[![Version](https://img.shields.io/badge/version-v6.4-blue)](https://github.com/Siyebai/baiye-trading-system)
[![Strategy](https://img.shields.io/badge/strategy-Momentum_Reversal-green)](https://github.com/Siyebai/baiye-trading-system)
[![Exchange](https://img.shields.io/badge/exchange-Binance_Futures-yellow)](https://www.binance.com)

---

## 📖 系统概述

**白夜交易系统 v6.4** — 基于 Binance 合约市场的15分钟动量反转策略量化交易系统。

- **策略逻辑**：检测连续同向K线（动量过度延伸）触发反转信号
- **信号过滤**：ADX趋势强度过滤 + ATR波动率自适应止盈止损
- **手续费模式**：Maker限价单（0.02%/单边），最大化保留利润
- **目标收益**：月均+2%~+5%（保守估计），最大回撤控制在25%以内

---

## 🏗️ 系统架构

```
baiye-trading-system/
│
├── engine/                          # 核心引擎层
│   ├── backtest_engine_v2.py        # 回测引擎 v2.2（Wilder's ATR/ADX）
│   ├── signal_engine.py             # 信号检测引擎
│   ├── risk_engine.py               # 风控引擎（日熔断/仓位管理）
│   ├── order_executor.py            # 订单执行器（限流重试）
│   ├── live_engine.py               # 实盘主引擎
│   └── white_night_v6_1.py          # v6.1 一体化引擎
│
├── config/                          # 配置文件
│   ├── strategy_v12_optimized.json  # 各品种最优参数
│   ├── optimal_params.yaml          # 网格搜索最优参数
│   └── capital_allocation.yaml     # 资金分配方案（夏普加权）
│
├── start_paper_v64.py               # ⭐ 纸交易主程序 v6.4（当前版本）
├── run_100trades_v40.py             # 100笔完整验证脚本 v4.0
├── backtest_real.py                 # 快速回测脚本
│
├── docs/                            # 文档
│   ├── BACKTEST_RESULTS.md          # 回测结果汇总
│   └── MONTHLY_STABILITY.md        # 月度稳定性分析
│
├── logs/                            # 日志（运行时生成）
│   ├── paper_v64.log                # 实时纸交易日志
│   └── backtest_v40_100trades.json  # 100笔验证数据
│
├── VALIDATION_100TRADES_V40.md      # ✅ 100笔完整闭环复盘报告
├── CHANGELOG.md                     # 版本更新记录
└── README.md                        # 本文件
```

---

## 📊 策略参数（v6.4 Maker版）

| 品种 | 连涨根数(SHORT) | 连跌根数(LONG) | ADX阈值 | TP倍数 | SL倍数 | 禁多 |
|------|---------------|--------------|---------|--------|--------|------|
| LINKUSDT | 7根 | 4根 | ≥15 | 0.8×ATR | 1.0×ATR | 否 |
| SOLUSDT  | 5根 | 4根 | ≥25 | 1.0×ATR | 1.0×ATR | 否 |
| BNBUSDT  | 5根 | 6根 | ≥15 | 1.2×ATR | 1.0×ATR | 是 |

**开仓条件（SHORT）**：
- 连续 ≥sc 根 K线收盘价递增
- 累计涨幅 ≥ ccp（防噪音）
- ADX ≥ adx_th（趋势强度确认）

**开仓条件（LONG）**：
- 连续 ≥lc 根 K线收盘价递减
- 累计跌幅 ≥ ccp
- ADX ≥ adx_th
- close > EMA200（牛市过滤）

---

## ✅ 100笔验证结果（v4.0 · Maker费率）

| 指标 | 值 | 状态 |
|------|-----|------|
| 总交易笔数 | **100笔** | ✅ |
| 综合胜率 | **61.0%** | ✅ (目标≥58%) |
| 盈利因子 | **1.196** | ✅ (>1.0) |
| 总净PnL | **+28.08U** | ✅ |
| 终值 | **178.08U** (起始150U) | ✅ |
| 平均盈利 | +2.808U/笔 | - |
| 平均亏损 | -3.672U/笔 | - |
| 手续费 | 0.02%单边 (Maker) | ✅ |

---

## 🔧 环境要求

```bash
Python >= 3.8
pandas, numpy, requests
```

---

## 🚀 快速启动

```bash
# 启动纸交易（v6.4 Maker版）
cd baiye-trading-system
python3 start_paper_v64.py

# 运行100笔完整验证
python3 run_100trades_v40.py

# 快速回测
python3 backtest_real.py
```

---

## ⚠️ 风控说明

- **单笔风险**: 账户权益的 2%（150U → 约3U/笔）
- **日亏损熔断**: 权益 ×6% = 约9U
- **最大持仓数**: 3个品种同时持仓
- **BNB禁止做多**（近期单边下跌趋势）

---

## 📈 版本历史

| 版本 | 日期 | 关键更新 |
|------|------|---------|
| v6.4 | 2026-05-10 | Maker限价单策略，FEE=0.02%，三品种组合，100笔验证通过 |
| v6.3 | 2026-05-10 | 降低ADX阈值，扩展8品种，加速信号积累 |
| v6.1 | 2026-05-09 | Wilder's ATR/ADX修正，多品种并行 |
| v2.2 | 2026-05-08 | 深度Bug修复（手续费计算/ATR算法） |
| v2.0 | 2026-05-06 | 多品种扩展（LINK/POL加入） |
| v1.0 | 2026-05-01 | 初始版本（BTC/ETH/SOL/BNB） |

---

## 📁 仓库地址

**GitHub**: https://github.com/Siyebai/baiye-trading-system

---

*白夜交易系统 · 构建者：思夜白 · 技术伙伴：李白 v1.0*
