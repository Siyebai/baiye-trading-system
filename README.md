# 白夜交易系统 v6.1 (White Night Trading System)

> 基于动量反转策略的多品种加密货币量化交易系统，专为 Binance 合约设计
> **最新版本：v6.1** | **更新日期：2025-05-08**

---

## 系统概述

| 项目 | 内容 |
|------|------|
| **版本** | v6.1 (ATR 1.5× SL + TP Tightening) |
| **策略类型** | 均值回归 (Mean Reversion) |
| **核心逻辑** | 连涨连跌 + 累计变化 + EMA200 + ADX |
| **时间框架** | 3m / 5m / 15m / 1h |
| **合格品种** | 6个（BTC/LINK/POL/ETH/SOL/BNB） |
| **平均胜率** | 73.7% (15m, v6.1) |
| **Walk-Forward** | 全部通过，0/6 过拟合 |

---

## v6.1 关键突破

| 指标 | v6.0 (SL=1.0×) | v6.1 (SL=1.5×) | 提升 |
|------|-----------------|-----------------|------|
| 平均胜率 | 61.9% | **73.7%** | +11.8% |
| BTCUSDT WF降幅 | 6.3% | **1.6%** | 过拟合消除 |
| SOLUSDT WF降幅 | 7.7% | **3.9%** | 过拟合消除 |
| 过拟合品种数 | 2/6 | **0/6** | 全部通过 |

**核心优化**：
1. **ATR 1.5× 止损** — 宽止损容忍更多市场噪音，大幅提升胜率
2. **TP 缩紧** (tp_s=0.6, tp_l=0.5) — 快进快出，配合宽止损实现高胜率
3. **信号逻辑 100% 一致** — 与原始引擎 (backtest_engine_v2) 完全验证

---

## 策略逻辑

### 信号生成（与原始引擎 100% 一致）

```python
# SHORT（做空）
条件: consec_up[i] >= sc AND cum_chg[i] >= ccp AND ADX[i] >= adx_th

# LONG（做多）
条件: consec_down[i] >= lc AND cum_chg[i] <= -ccp AND close[i] > EMA200[i] AND ADX[i] >= adx_th
```

### 出场规则

```
SHORT: SL = entry + sl_atr × ATR,  TP = entry - tp_s × ATR
LONG:  SL = entry - sl_atr × ATR,  TP = entry + tp_l × ATR

同帧双触发: 用开盘价判断先后（先判断哪个更近）
入场价格: 信号根 i → 下根 i+1 的 open 价
手续费: 0.09% 单边 (FEE = 0.0009)
```

### 风控增强

| 机制 | 说明 |
|------|------|
| 连续SL冷却 | 2次连续止损 → 暂停 16 根K线 (约4小时) |
| 品种级禁多 | BNBUSDT long_disabled=True (多WR=42.9%拖累) |
| ADX动态TP | BNBUSDT: ADX≥30→TP×1.3, ADX≥40→TP×1.6 |
| Walk-Forward验证 | 70/30 训练/测试分割，过拟合阈值: WR降幅>10% |

---

## v6.1 回测结果 (15m, 180天, ATR 1.5×)

| 品种 | WR% | PF | 月均% | 最大DD | 交易数 | WF通过 |
|------|-----|-----|-------|--------|--------|--------|
| LINKUSDT | **80.8%** | 1.62 | 4.2% | 10.1% | 182 | ✅ |
| ETHUSDT | **78.9%** | 1.42 | 3.8% | 12.5% | 332 | ✅ |
| BTCUSDT | **73.5%** | 1.35 | 7.2% | 8.4% | 464 | ✅ |
| SOLUSDT | **70.2%** | 1.10 | 1.5% | 9.8% | 280 | ✅ |
| BNBUSDT | **67.3%** | 1.22 | 5.1% | 7.6% | 201 | ✅ |
| POLUSDT | **65.8%** | 1.06 | 0.5% | 14.3% | 271 | ✅ |
| **平均** | **73.7%** | **1.27** | **3.7%** | — | — | **6/6** |

### 多时间框架覆盖 (v6.1)

| 时间框架 | 品种 | WF验证 |
|----------|------|--------|
| 3m | BTC/ETH/SOL/BNB | ✅ |
| 5m | BTC/ETH/SOL/BNB | ✅ |
| 15m | BTC/LINK/POL/ETH/SOL/BNB | ✅ |
| 1h | BTC/ETH/SOL/BNB | ✅ |

> 注意: LINKUSDT/POLUSDT 仅有 15m 数据，无 3m/5m/1h 参数

---

## 目录结构

```
killer-trading-system/
├── engine/
│   ├── white_night_v6_1.py     # 🔑 白夜系统 v6.1（主文件，自包含）
│   ├── white_night_v6_0.py     # v6.0 原始版本
│   ├── backtest_engine_v2.py   # 原始验证引擎（信号逻辑基准）
│   ├── live_engine.py          # 实盘执行引擎
│   ├── signal_engine.py        # 信号生成模块
│   ├── risk_engine.py          # 风控模块
│   └── order_executor.py       # 下单执行
├── config/
│   ├── optimal_params_v6.json  # 🔑 v6.1 多时间框架参数（含3m/5m/1h）
│   ├── optimal_params.json     # 15m 基准参数
│   └── capital_allocation.json # 资金分配
├── data/
│   ├── *_15m_180d.csv          # 6品种 180天 15m 数据 (17280根/品种)
│   ├── *_3m_90d.json           # 4品种 90天 3m 数据
│   ├── *_5m*.json              # 4品种 5m 数据
│   └── *_1h*.json              # 4品种 1h 数据
├── docs/                       # 文档
├── research/                   # 研究报告
├── AGENT_GUIDE.md              # 🔑 智能体使用指南
├── README.md                   # 本文件
└── VERSION                     # 版本号
```

---

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/Siyebai/killer-trading-system.git
cd killer-trading-system
```

### 2. 安装依赖

```bash
pip install numpy pandas
```

### 3. 运行 v6.1 回测

```python
import sys
sys.path.insert(0, 'engine')
from white_night_v6_1 import WhiteNight

# 运行全部品种 15m 回测
wn = WhiteNight()
wn.run_all()
```

### 4. 单品种回测

```python
import pandas as pd
from white_night_v6_1 import Indicators, SignalEngine, BacktestEngine, Params, calc_stats

# 加载数据
df = pd.read_csv('data/BTCUSDT_15m_180d.csv')
df['ts'] = pd.to_datetime(df['ts'])
df = df.set_index('ts').sort_index()
df.columns = [c.lower() for c in df.columns]

# 计算指标
df = Indicators.compute(df)

# 获取 v6.1 参数
p = Params.get('BTCUSDT', '15m')
# p = {'sc': 4, 'lc': 5, 'ccp': 0.002, 'adx_th': 22, 'tp_s': 0.6, 'tp_l': 0.5, 'sl_atr': 1.5}

# 生成信号
sigs = SignalEngine.generate_core(df, sc=p['sc'], lc=p['lc'], ccp=p['ccp'], adx_th=p['adx_th'])

# 回测
trades, equity = BacktestEngine.run(df, sigs, tp_s=p['tp_s'], tp_l=p['tp_l'], sl_atr=p['sl_atr'])
days = (df.index[-1] - df.index[0]).days
stats = calc_stats(trades, days=days)
print(f"WR={stats.wr:.1f}% PF={stats.pf:.2f} 月均={stats.monthly:.1f}%")
```

### 5. Walk-Forward 验证

```python
from white_night_v6_1 import WalkForward

wf = WalkForward.validate(df, 'BTCUSDT', '15m')
print(f"训练WR={wf['train']['wr']:.1f}% 测试WR={wf['test']['wr']:.1f}% 降幅={wf['wr_drop']:.1f}%")
print(f"过拟合={'是' if wf['overfit'] else '否'}")
```

### 6. 使用原始引擎（对比验证）

```python
from backtest_engine_v2 import compute_indicators, generate_signals, backtest_v2, calc_stats_v2

df = pd.read_csv('data/BTCUSDT_15m_180d.csv')
df['ts'] = pd.to_datetime(df['ts'])
df = df.set_index('ts').sort_index()
df.columns = [c.lower() for c in df.columns]
df = compute_indicators(df)

sigs = generate_signals(df, sc=4, lc=5, ccp=0.002, adx_th=22)
trades = backtest_v2(df, sigs, tp_s=0.6, tp_l=0.5, capital=150.0)
# 信号与 v6.1 完全一致（100% 匹配验证通过）
```

---

## v6.1 参数详解

### 15m 参数

| 品种 | sc | lc | ccp | adx_th | tp_s | tp_l | sl_atr | 特殊 |
|------|----|----|-----|--------|------|------|--------|------|
| BTCUSDT | 4 | 5 | 0.002 | 22 | 0.6 | 0.5 | 1.5 | — |
| LINKUSDT | 7 | 4 | 0.0025 | 15 | 0.6 | 0.5 | 1.5 | — |
| POLUSDT | 5 | 4 | 0.0015 | 25 | 0.8 | 0.7 | 1.5 | — |
| ETHUSDT | 5 | 4 | 0.0015 | 20 | 0.6 | 0.5 | 1.5 | — |
| SOLUSDT | 5 | 4 | 0.0015 | 25 | 0.8 | 0.7 | 1.5 | — |
| BNBUSDT | 5 | 6 | 0.0015 | 15 | 0.8 | 0.7 | 1.5 | 禁多+ADX动态TP |

### 3m / 5m / 1h 参数

详见 `config/optimal_params_v6.json` 的 `v6_validated_params` 部分，或查看 `engine/white_night_v6_1.py` 中的 `Params` 类。

**参数规律**：
- 更短周期 → 更大 sc，更小 ccp（更严格入场）
- 更长周期 → 更小 sc/lc，更大 ccp（更宽松入场）
- 所有时间框架统一 sl_atr = 1.5

---

## 数据文件说明

### 15m CSV 格式（6品种）

```
ts,open,high,low,close,volume
2024-11-09 00:00:00,76300.0,76350.5,76250.2,76310.8,1250.3
```

- 每品种约 17280 根K线
- 时间跨度约 180 天

### 3m/5m/1h JSON 格式（2种）

**格式A** — Binance 数组格式:
```json
[[1704067200000, 42000.0, 42100.0, 41950.0, 42050.0, 1234.5, ...], ...]
```

**格式B** — 字典缩写格式:
```json
[{"ts": 1704067200000, "o": 42000.0, "h": 42100.0, "l": 41950.0, "c": 42050.0, "v": 1234.5, "dt": "2024-01-01 00:00", "tbv": 617.2}, ...]
```

> v6.1 的 DataLoader 已自动处理两种 JSON 格式

---

## 已验证的研究结论

| 实验项目 | 结论 |
|----------|------|
| MACD方向过滤 | ❌ 杀掉95%信号，均值回归与趋势跟踪逻辑矛盾 |
| RSI极端过滤 | ❌ 无效果，信号本身已在极端位置 |
| 成交量确认 | ❌ 减少70%信号，无胜率提升 |
| DI方向过滤 | ❌ 杀掉75-81%信号，胜率反而下降 |
| ATR 1.5×止损 | ✅ 胜率提升12%，消除过拟合 |
| TP缩紧 | ✅ 快进快出，配合宽止损效果最佳 |
| 连续SL冷却 | ✅ 避免连续止损后冲动交易 |
| 品种级禁多 | ✅ BNBUSDT禁多后胜率+2.3% |

---

## 风控规则

| 规则 | 阈值 | 动作 |
|------|------|------|
| 单日亏损 | ≥6% | 当日停止所有交易 |
| 总回撤 | ≥20% | 系统暂停，等待手动恢复 |
| 最低胜率 | <55% | 品种剔除 |
| 最大并发持仓 | 3个 | 新信号排队等待 |
| 连续止损 | 2次 | 暂停16根K线(约4小时) |
| 实盘操作 | — | 必须人工确认 |

---

## 版本历史

| 版本 | 日期 | 核心变更 |
|------|------|---------|
| **v6.1** | 2025-05-08 | ATR 1.5×止损 + TP缩紧 → WR 73.7%，0/6过拟合 |
| v6.0 | 2025-05-08 | 信号逻辑回归验证策略 + 多时间框架 + Walk-Forward |
| v5.7 | 2025-05-07 | EMA+DI+RSI组合信号（已验证失败，WR=24.5%） |
| v2.0 | 2025-05-06 | 连涨连跌核心策略验证，6品种180天回测 |

---

## 贡献指南

1. Fork 本仓库
2. 创建特性分支 (`git checkout -b feature/xxx`)
3. 提交变更 (`git commit -m 'feat: xxx'`)
4. 推送分支 (`git push origin feature/xxx`)
5. 创建 Pull Request

---

## 许可证

MIT License

---

## 免责声明

⚠️ **本系统仅供研究学习，不构成任何投资建议。加密货币交易风险极高，可能导致全部本金损失。历史回测不代表未来表现。实盘操作前务必充分理解风险并做好资金管理。**

---

*白夜交易系统 v6.1 | 构建者：思夜白*
