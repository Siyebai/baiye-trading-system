# 📦 数据仓库使用指南（供其他 Agent）

> **仓库地址**：https://github.com/Siyebai/killer-trading-system  
> **版本**：v3.2.0 | **更新**：2026-05-07  
> **已验证**：6品种 × 180天真实K线数据，胜率58-68%，全部PF>1.0

---

## 快速克隆

```bash
git clone https://github.com/Siyebai/killer-trading-system.git
cd killer-trading-system
```

---

## 数据文件一览（data/ 目录）

### 主要回测数据（★推荐使用）

| 文件名 | 品种 | 周期 | 范围 | 格式 | 大小 |
|--------|------|------|------|------|------|
| `BTCUSDT_15m_180d.csv` | BTC | 15min | 180天 | CSV | 1.0MB |
| `ETHUSDT_15m_180d.csv` | ETH | 15min | 180天 | CSV | 1.1MB |
| `SOLUSDT_15m_180d.csv` | SOL | 15min | 180天 | CSV | 0.9MB |
| `BNBUSDT_15m_180d.csv` | BNB | 15min | 180天 | CSV | 1.0MB |
| `LINKUSDT_15m_180d.csv` | LINK | 15min | 180天 | CSV | 0.9MB |
| `POLUSDT_15m_180d.csv` | POL | 15min | 180天 | CSV | 1.0MB |

**CSV格式**：`ts,open,high,low,close,volume` — 每品种17280根K线

### 扩展数据

| 文件名 | 品种 | 周期 | 格式 | 大小 |
|--------|------|------|------|------|
| `BTCUSDT_5m_60d.json` | BTC | 5min | JSON | 4.8MB |
| `BTCUSDT_3m_90d.json` | BTC | 3min | JSON | 6.0MB |
| `BTCUSDT_1h_365d.json` | BTC | 1h | JSON | 2.1MB |
| `BNBUSDT_3m_90d.json` | BNB | 3min | JSON | 5.8MB |
| `ETHUSDT_3m_90d.json` | ETH | 3min | JSON | 6.0MB |
| `SOLUSDT_3m_90d.json` | SOL | 3min | JSON | 6.0MB |

**JSON格式**：Binance Kline数组 `[ts, open, high, low, close, volume, ...]`

---

## 最优参数（config/optimal_params.json）

### 已验证结果

| 品种 | 胜率 | PF | 月均收益 | 最大回撤 | Walk-Forward外推 |
|------|------|-----|---------|---------|-----------------|
| **LINKUSDT** | **66.3%** | 1.40 | 8.2% | 12.3% | 67.5% ✅ |
| **ETHUSDT** | **64.7%** | 1.40 | 14.1% | 21.2% | 66.7% ✅ |
| **BTCUSDT** | **64.7%** | 1.57 | 18.5% | 11.4% | 62.0% ✅ |
| **BNBUSDT** | 60.4% | 1.38 | 13.1% | 14.2% | 57.2% ✅ |
| **SOLUSDT** | 60.0% | 1.17 | 6.8% | 16.7% | 60.1% ✅ |
| **POLUSDT** | 58.7% | 1.23 | 8.1% | 21.5% | 59.8% ✅ |

### 每品种核心参数

| 品种 | sc | lc | ccp | adx_th | tp_s | tp_l | sl_atr |
|------|----|----|-----|--------|------|------|--------|
| BTCUSDT | 4 | 5 | 0.002 | 22 | 0.8 | 1.0 | 1.0 |
| LINKUSDT | 7 | 4 | 0.0025 | 15 | 0.8 | 0.7 | 1.0 |
| POLUSDT | 5 | 4 | 0.0015 | 25 | 1.0 | 0.7 | 1.0 |
| ETHUSDT | 5 | 4 | 0.0015 | 20 | 0.8 | 0.7 | 1.0 |
| SOLUSDT | 5 | 4 | 0.0015 | 25 | 0.8 | 0.8 | 1.0 |
| BNBUSDT | 5 | 6 | 0.0015 | 15 | 0.8 | 0.8 | 1.0 |

**参数说明**：
- `sc`：连涨K线数阈值（做空信号：连涨≥sc根 + 累计涨幅≥ccp）
- `lc`：连跌K线数阈值（做多信号：连跌≥lc根 + 累计跌幅≤-ccp + 价格>EMA200）
- `ccp`：累计变化百分比阈值
- `adx_th`：ADX趋势强度阈值
- `tp_s`/`tp_l`：短线/长线止盈乘数
- `sl_atr`：ATR止损倍数

---

## Agent 使用示例

### Python 直接加载

```python
import pandas as pd
import json

# 方法1：从GitHub Raw加载（无需克隆）
base = "https://raw.githubusercontent.com/Siyebai/killer-trading-system/main/data/"
btc = pd.read_csv(base + "BTCUSDT_15m_180d.csv")

# 方法2：克隆后本地读取
btc = pd.read_csv("data/BTCUSDT_15m_180d.csv")

# 方法3：加载JSON数据
with open("data/BTCUSDT_5m_60d.json") as f:
    btc_json = json.load(f)
```

### 使用原始回测引擎

```python
import sys
sys.path.insert(0, "engine")
from backtest_engine_v2 import compute_indicators, generate_signals, backtest_v2

# 加载数据
df = pd.read_csv("data/BTCUSDT_15m_180d.csv")
df['ts'] = pd.to_datetime(df['ts'])
df = df.set_index('ts').sort_index()
df.columns = [c.lower() for c in df.columns]

# 计算指标
df = compute_indicators(df)

# 生成信号
sigs = generate_signals(df, sc=4, lc=5, ccp=0.002, adx_th=22)

# 回测
trades = backtest_v2(df, sigs, tp_s=0.8, tp_l=1.0, capital=150.0)
```

### 从零开始自定义策略

```python
# 最简数据加载
df = pd.read_csv("data/BTCUSDT_15m_180d.csv", index_col='ts', parse_dates=True)
print(f"数据范围: {df.index[0]} ~ {df.index[-1]}")
print(f"K线数量: {len(df)}")
```

---

## 引擎文件

| 文件 | 说明 |
|------|------|
| `engine/backtest_engine_v2.py` | 主回测引擎（验证版） |
| `engine/signal_engine.py` | 信号引擎 |
| `engine/risk_engine.py` | 风控引擎 |
| `engine/live_engine.py` | 实盘引擎 |
| `engine/ws_feeder.py` | WebSocket数据源 |

## 配置文件

| 文件 | 说明 |
|------|------|
| `config/optimal_params.json` | 6品种最优参数 |
| `config/capital_allocation.json` | 资金分配方案 |
| `config/system_params.yaml` | 系统参数 |

## 风控参数

| 参数 | 值 | 说明 |
|------|-----|------|
| 单笔风险 | 2% | capital=150USDT, 风险3USDT/笔 |
| 日最大亏损 | 6% | 超过则暂停 |
| 最大回撤停止 | 20% | 超过则停止 |
| 最大并发持仓 | 3 | 同时最多3个仓位 |
| 最低胜率阈值 | 58% | 低于此值品种剔除 |
| 连续止损冷却 | 2次SL→16根K线 | 约4小时 |

---

⚠️ **免责声明**：以上数据和分析仅供研究参考，不构成任何投资建议。投资有风险，决策需谨慎。
