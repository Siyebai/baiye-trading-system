# 智能体使用指南 (Agent Guide)

> **仓库地址**：https://github.com/Siyebai/killer-trading-system
> **最新版本**：v6.1 | **更新日期**：2025-05-08
> **核心引擎**：`engine/white_night_v6_1.py`（自包含，单文件即可运行）

---

## 快速克隆

```bash
git clone https://github.com/Siyebai/killer-trading-system.git
cd killer-trading-system
pip install numpy pandas
```

---

## 核心文件清单

| 文件 | 用途 | 必要性 |
|------|------|--------|
| `engine/white_night_v6_1.py` | 白夜系统 v6.1 完整代码（自包含） | ⭐ 必须 |
| `engine/backtest_engine_v2.py` | 原始验证引擎（信号基准） | 可选（对比验证用） |
| `config/optimal_params_v6.json` | 多时间框架参数配置 | ⭐ 必须 |
| `config/optimal_params.json` | 15m 基准参数 | 可选 |
| `data/*.csv` / `data/*.json` | 真实K线数据 | ⭐ 必须 |

---

## 数据文件一览

### 15m 数据（6品种，推荐使用）

| 文件 | 品种 | K线数 | 格式 |
|------|------|-------|------|
| `BTCUSDT_15m_180d.csv` | BTC | 17280 | CSV: ts,open,high,low,close,volume |
| `ETHUSDT_15m_180d.csv` | ETH | 17280 | 同上 |
| `SOLUSDT_15m_180d.csv` | SOL | 17280 | 同上 |
| `BNBUSDT_15m_180d.csv` | BNB | 17280 | 同上 |
| `LINKUSDT_15m_180d.csv` | LINK | 17280 | 同上 |
| `POLUSDT_15m_180d.csv` | POL | 17280 | 同上 |

### 多时间框架数据

| 文件 | 品种 | 周期 | 格式 | 说明 |
|------|------|------|------|------|
| `BTCUSDT_3m_90d.json` | BTC | 3min | 数组 | [ts,o,h,l,c,v,...] |
| `ETHUSDT_3m_90d.json` | ETH | 3min | 数组 | 同上 |
| `SOLUSDT_3m_90d.json` | SOL | 3min | 数组 | 同上 |
| `BNBUSDT_3m_90d.json` | BNB | 3min | 数组 | 同上 |
| `BTCUSDT_5m_60d.json` | BTC | 5min | 数组 | 同上 |
| `ETHUSDT_5m.json` | ETH | 5min | 字典 | {ts,o,h,l,c,v,tbv,dt} |
| `SOLUSDT_5m.json` | SOL | 5min | 字典 | 同上 |
| `BNBUSDT_5m.json` | BNB | 5min | 字典 | 同上 |
| `BTCUSDT_1h_365d.json` | BTC | 1h | 数组 | [ts,o,h,l,c,v,...] |
| `ETHUSDT_1h.json` | ETH | 1h | 字典 | {ts,o,h,l,c,v,tbv,dt} |
| `SOLUSDT_1h.json` | SOL | 1h | 字典 | 同上 |
| `BNBUSDT_1h.json` | BNB | 1h | 字典 | 同上 |

> **注意**：LINKUSDT 和 POLUSDT 仅有 15m 数据

---

## 使用方式

### 方式一：运行完整系统（推荐）

```python
import sys
sys.path.insert(0, 'engine')
from white_night_v6_1 import WhiteNight

wn = WhiteNight()
wn.run_all()  # 运行全部6品种15m回测 + Walk-Forward验证
```

### 方式二：单品种精细回测

```python
import pandas as pd
from white_night_v6_1 import Indicators, SignalEngine, BacktestEngine, Params, calc_stats

# 1. 加载数据
df = pd.read_csv('data/BTCUSDT_15m_180d.csv')
df['ts'] = pd.to_datetime(df['ts'])
df = df.set_index('ts').sort_index()
df.columns = [c.lower() for c in df.columns]

# 2. 计算指标（ATR, ADX, EMA200, RSI, MACD, 连涨连跌等）
df = Indicators.compute(df)

# 3. 获取参数
p = Params.get('BTCUSDT', '15m')
# {'sc': 4, 'lc': 5, 'ccp': 0.002, 'adx_th': 22, 'tp_s': 0.6, 'tp_l': 0.5, 'sl_atr': 1.5}

# 4. 生成信号
sigs = SignalEngine.generate_core(
    df, sc=p['sc'], lc=p['lc'], ccp=p['ccp'], adx_th=p['adx_th'],
    long_disabled=p.get('long_disabled', False)
)

# 5. 回测
trades, equity = BacktestEngine.run(
    df, sigs,
    tp_s=p['tp_s'], tp_l=p['tp_l'], sl_atr=p['sl_atr'],
    adx_dynamic_tp=p.get('adx_dynamic_tp', False),
    capital=150.0, risk_pct=0.02
)

# 6. 统计
days = (df.index[-1] - df.index[0]).days
stats = calc_stats(trades, days=days)
print(f"WR={stats.wr:.1f}% PF={stats.pf:.2f} 月均={stats.monthly:.1f}% DD={stats.max_dd:.1f}%")
print(f"交易数={stats.trades} 多头胜率={stats.long_wins}/{stats.long_trades} 空头胜率={stats.short_wins}/{stats.short_trades}")
```

### 方式三：加载 JSON 数据（多时间框架）

```python
from white_night_v6_1 import DataLoader

# DataLoader 自动处理两种 JSON 格式
df = DataLoader.load('data/BTCUSDT_3m_90d.json', 'BTCUSDT')
# 或
df = DataLoader.load('data/ETHUSDT_5m.json', 'ETHUSDT')  # 缩写字段自动映射
```

### 方式四：Walk-Forward 验证

```python
from white_night_v6_1 import WalkForward

wf = WalkForward.validate(df, 'BTCUSDT', '15m', train_ratio=0.7)
print(f"训练WR={wf['train']['wr']:.1f}% 测试WR={wf['test']['wr']:.1f}% 降幅={wf['wr_drop']:.1f}%")
print(f"过拟合={'是' if wf['overfit'] else '否'}")
```

### 方式五：从 GitHub Raw 加载（无需克隆）

```python
import pandas as pd

base = "https://raw.githubusercontent.com/Siyebai/killer-trading-system/main/data/"
btc = pd.read_csv(base + "BTCUSDT_15m_180d.csv")
```

---

## v6.1 参数速查

### 15m 参数

| 品种 | sc | lc | ccp | adx_th | tp_s | tp_l | sl_atr | 特殊 |
|------|----|----|-----|--------|------|------|--------|------|
| BTCUSDT | 4 | 5 | 0.002 | 22 | 0.6 | 0.5 | 1.5 | — |
| LINKUSDT | 7 | 4 | 0.0025 | 15 | 0.6 | 0.5 | 1.5 | — |
| POLUSDT | 5 | 4 | 0.0015 | 25 | 0.8 | 0.7 | 1.5 | — |
| ETHUSDT | 5 | 4 | 0.0015 | 20 | 0.6 | 0.5 | 1.5 | — |
| SOLUSDT | 5 | 4 | 0.0015 | 25 | 0.8 | 0.7 | 1.5 | — |
| BNBUSDT | 5 | 6 | 0.0015 | 15 | 0.8 | 0.7 | 1.5 | 禁多+ADX动态TP |

### 3m 参数

| 品种 | sc | lc | ccp | adx_th | tp_s | tp_l | sl_atr | 特殊 |
|------|----|----|-----|--------|------|------|--------|------|
| BTCUSDT | 8 | 3 | 0.002 | 12 | 0.8 | 0.7 | 1.5 | — |
| ETHUSDT | 4 | 3 | 0.0008 | 12 | 1.0 | 1.0 | 1.5 | — |
| SOLUSDT | 4 | 4 | 0.0015 | 22 | 0.8 | 0.7 | 1.5 | — |
| BNBUSDT | 5 | 3 | 0.0008 | 22 | 1.0 | 0.8 | 1.5 | 禁多 |

### 5m 参数

| 品种 | sc | lc | ccp | adx_th | tp_s | tp_l | sl_atr | 特殊 |
|------|----|----|-----|--------|------|------|--------|------|
| BTCUSDT | 4 | 4 | 0.0015 | 18 | 0.8 | 0.7 | 1.5 | — |
| ETHUSDT | 4 | 6 | 0.001 | 22 | 0.8 | 0.7 | 1.5 | — |
| SOLUSDT | 4 | 5 | 0.0008 | 15 | 1.0 | 1.0 | 1.5 | — |
| BNBUSDT | 7 | 3 | 0.0008 | 25 | 0.8 | 0.7 | 1.5 | 禁多 |

### 1h 参数

| 品种 | sc | lc | ccp | adx_th | tp_s | tp_l | sl_atr | 特殊 |
|------|----|----|-----|--------|------|------|--------|------|
| BTCUSDT | 6 | 5 | 0.004 | 15 | 0.8 | 0.7 | 1.5 | — |
| ETHUSDT | 3 | 5 | 0.004 | 18 | 0.8 | 0.7 | 1.5 | — |
| SOLUSDT | 3 | 3 | 0.005 | 25 | 1.0 | 0.8 | 1.5 | — |
| BNBUSDT | 3 | 2 | 0.003 | 15 | 0.8 | 0.7 | 1.5 | 禁多 |

---

## 信号逻辑详解

```python
# 核心信号（与 backtest_engine_v2 100% 一致）
# 遍历每根K线 i (从第200根开始):

if adx[i] < adx_th:
    continue  # ADX 不足，跳过

# 做空信号
if consec_up[i] >= sc and cum_chg[i] >= ccp:
    signal = -1  # SHORT

# 做多信号
elif consec_down[i] >= lc and cum_chg[i] <= -ccp and close[i] > ema200[i]:
    signal = 1  # LONG
```

**关键概念**：
- `consec_up`：连续收盘价上涨的K线数量（涨则+1，跌/平则重置为0）
- `consec_down`：连续收盘价下跌的K线数量（跌则+1，涨/平则重置为0）
- `cum_chg`：当前连续涨/跌期间的累计涨跌幅（换方向时重置）
- 策略本质是**均值回归**：连涨后做空，连跌后做多（在EMA200上方）

---

## 回测引擎关键细节

| 项目 | 说明 |
|------|------|
| 入场价格 | 信号根 i → 下根 i+1 的 open 价 |
| 止损 | SL = entry ± sl_atr × ATR (1.5×) |
| 止盈 | TP = entry ∓ tp_s/tp_l × ATR |
| 同帧双触发 | 用开盘价判断先后（距离开盘价更近的先触发） |
| 手续费 | 0.09% 单边 (FEE = 0.0009) |
| 冷却期 | 同方向信号间隔 ≥ 5 根K线 |
| 资金管理 | 固定风险 2% / 笔 |

---

## 已验证的研究结论（请勿重试）

| 实验 | 结论 | 原因 |
|------|------|------|
| MACD方向过滤 | ❌ 杀掉95%信号 | 均值回归做空时MACD通常看多，逻辑矛盾 |
| RSI极端过滤 | ❌ 无效果 | 信号本身已处于极端位置 |
| 成交量确认 | ❌ 减少70%信号无提升 | 量价不相关于均值回归信号质量 |
| DI方向过滤 | ❌ 杀掉75-81%信号 | DI与均值回归方向矛盾 |
| ATR 1.0×止损 | ⚠️ 胜率低但月收益高 | 更窄止损→更多止损出场→WR低但赢时赚更多 |
| ATR 1.5×止损 | ✅ 胜率73.7% | 宽止损容忍噪音，过拟合消除 |

---

## 风控参数

| 参数 | 值 | 说明 |
|------|-----|------|
| 单笔风险 | 2% | capital=150USDT, 风险3USDT/笔 |
| 日最大亏损 | 6% | 超过则暂停 |
| 最大回撤停止 | 20% | 超过则停止 |
| 最大并发持仓 | 3 | 同时最多3个仓位 |
| 最低胜率阈值 | 55% | 低于此值品种剔除 |
| 连续止损冷却 | 2次SL→16根K线 | 约4小时 |
| 实盘确认 | 必须 | 系统不得自动执行 |

---

## 常见问题

**Q: 数据格式不匹配？**
- CSV 文件使用 `ts,open,high,low,close,volume` 格式
- JSON 文件有两种格式，DataLoader 已自动处理
- 确保 DataFrame 列名全小写，且 `ts` 列为 datetime 类型

**Q: 如何切换时间框架？**
```python
# 加载不同时间框架数据
df_3m = DataLoader.load('data/BTCUSDT_3m_90d.json', 'BTCUSDT')
df_5m = DataLoader.load('data/BTCUSDT_5m_60d.json', 'BTCUSDT')

# 使用对应参数
p_3m = Params.get('BTCUSDT', '3m')
p_5m = Params.get('BTCUSDT', '5m')
```

**Q: 如何验证信号一致性？**
```python
from backtest_engine_v2 import compute_indicators, generate_signals
from white_night_v6_1 import Indicators, SignalEngine

df_v2 = compute_indicators(df_orig)
sigs_v2 = generate_signals(df_v2, sc=4, lc=5, ccp=0.002, adx_th=22)

df_v6 = Indicators.compute(df_orig)
sigs_v6 = SignalEngine.generate_core(df_v6, sc=4, lc=5, ccp=0.002, adx_th=22)

# 应该100%匹配
assert (sigs_v2 == sigs_v6).all(), "信号不一致！"
```

---

⚠️ **免责声明**：以上数据和分析仅供研究参考，不构成任何投资建议。投资有风险，决策需谨慎。
