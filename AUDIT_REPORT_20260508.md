# AUDIT REPORT - 白夜系统深度审计报告
**日期**: 2026-05-08  
**引擎**: backtest_engine_v2.2  
**审计范围**: engine/backtest_engine_v2.py, engine/signal_engine.py, engine/live_engine.py, engine/risk_engine.py, scripts/live_scanner.py

---

## 一、Bug清单

### 🔴 严重Bug（已修复）

#### Bug#1: ATR算法不一致 [CRITICAL]
- **位置**: `engine/backtest_engine_v2.py`
- **问题**: 回测引擎使用 EWM(span=14, alpha=2/15=0.1333)，而 signal_engine.py 和 live_scanner.py 均使用 Wilder's 平滑(alpha=1/14=0.0714)
- **影响**: ATR值相差约 **21%**（实测 BTC：EWM=309 vs Wilder=254），导致SL/TP距离不一致，回测结果无法代表实盘表现
- **修复**: `compute_indicators()` 中 ATR/ADX 改为 Wilder's 平滑，与 signal_engine.py 保持一致
- **文件**: `engine/backtest_engine_v2.py` → v2.2

#### Bug#2: ADX算法不一致 [CRITICAL]
- **位置**: `engine/backtest_engine_v2.py`
- **问题**: 回测引擎 ADX 使用 EWM(span=14)，实盘引擎使用 Wilder's
- **影响**: ADX值相差大（实测 BTC：EWM=34.9 vs Wilder=24.0），导致ADX阈值对应的行情强度不同，信号触发频率差异显著（BTC 180天：EWM=436笔 vs Wilder=313笔）
- **修复**: 同 Bug#1，统一改为 Wilder's
- **文件**: `engine/backtest_engine_v2.py` → v2.2

#### Bug#7: RiskEngine.get_risk_pct() 方法不存在 [CRITICAL]
- **位置**: `engine/risk_engine.py`, `engine/live_engine.py:286`
- **问题**: `live_engine.py` 第286行调用 `self.risk.get_risk_pct()`，但 RiskEngine 类没有此方法，运行时会抛出 `AttributeError`
- **影响**: 实盘引擎持仓关闭回调(`_on_trade_closed`)必然崩溃
- **修复**: 在 RiskEngine 中添加 `get_risk_pct()` 方法
- **文件**: `engine/risk_engine.py`

#### Bug#8: live_scanner.py SHORT WIN PnL计算错误 [CRITICAL]
- **位置**: `scripts/live_scanner.py:180`
- **问题**: `pnl_r = (tp - entry) / entry` 对 SHORT 方向而言，tp < entry 时值为负数，WIN交易被计为亏损
- **影响**: 所有 SHORT 方向的盈利交易都被错误记为亏损，纸交易统计完全失真
- **修复**: SHORT WIN → `(entry - tp) / entry`，LONG WIN → `(tp - entry) / entry`
- **文件**: `scripts/live_scanner.py` → v2.2

### 🟡 中等Bug（已修复）

#### Bug#4: live_scanner.py 手续费率错误
- **位置**: `scripts/live_scanner.py`
- **问题**: `FEE = 0.0018`（双边合计费率）被用于单边计算，实际高估费用约2倍
- **修复**: `FEE = 0.0009`（单边，与 backtest_engine_v2.py 一致）
- **文件**: `scripts/live_scanner.py`

#### Bug#5: live_scanner.py 硬编码参数，忽略各品种最优配置
- **位置**: `scripts/live_scanner.py`
- **问题**: `check_short_signal(df)` 和 `check_long_signal(df)` 硬编码 n=6/4, pct=0.002, adx=20，不读取 config
- **影响**: 实盘扫描使用的参数与 optimal_params_v21.json 不符，多品种使用相同默认参数
- **修复**: 从 `optimal_params_v21.json` 加载各品种参数，传入信号函数
- **文件**: `scripts/live_scanner.py`

#### Bug#9: live_scanner.py 未遵守各品种 long_disabled 标志
- **位置**: `scripts/live_scanner.py`
- **问题**: BNBUSDT(long WR=42.9%) 和 POLUSDT 应禁止做多，但 live_scanner 未实现此逻辑
- **修复**: 读取 config 中 `long_disabled` 字段，如为 true 则跳过 LONG 信号检测
- **文件**: `scripts/live_scanner.py`

#### Bug#6: live_scanner.py 缺少冷却/互斥信号机制
- **位置**: `scripts/live_scanner.py`  
- **问题**: 无 signal cooldown 逻辑，同一品种可在连续多根K线重复触发信号
- **影响**: 在趋势延续行情中可能连续开仓，放大风险
- **修复**: 已改为只在无持仓时检测新信号（单品种互斥），并从2品种上限升至3品种（与config一致）
- **文件**: `scripts/live_scanner.py`

### 🟢 轻微问题（已记录，未修复）

#### Bug#3: 实盘入场价与回测入场价不同
- **位置**: `engine/signal_engine.py`, `engine/backtest_engine_v2.py`
- **问题**: 回测使用下根K线开盘价入场，实盘使用当前收盘价入场
- **影响**: 轻微偏差（约0.02-0.1%），在正常流动性市场可接受
- **建议**: 可在实盘订单执行时记录实际成交价与回测预期的差异，累积数据后优化

#### Bug#10: SignalEngine.evaluate() 重复处理历史K线
- **位置**: `engine/signal_engine.py`
- **问题**: 每次调用 `evaluate()` 都对所有历史K线重跑 EMA200 增量更新，滑动窗口测试需要每次新建引擎实例
- **影响**: 生产环境单流使用无问题；测试代码已正确处理（每次新建 SignalEngine）
- **建议**: 可优化为记录已处理的最后时间戳，避免重复计算

---

## 二、SOL专项优化

**Q1亏损原因分析**:
- adx_th=25 时，Q1市场剧烈波动（高频假突破）导致大量连续止损
- 实测数据支持：adx_th=30 过滤了更多低质量信号

**三方案对比**:

| 方案 | WR | Monthly | DD | Trades |
|------|-----|---------|-----|--------|
| ADX>=25 (原参数) | 59.5% | +5.9% | 16.7% | 279 |
| **ADX>=30 (选定)** | **62.3%** | **+8.8%** | **13.7%** | **228** |
| ADX>=30+sc=6 | 63.1% | +6.8% | 8.2% | 157 |
| ADX>=30+ccp=0.002 | 62.1% | +8.6% | 15.0% | 227 |
| SHORT only (adx25) | 60.6% | +4.6% | 11.4% | 165 |
| ADX>=35 | 63.0% | +6.9% | 16.9% | 162 |

**结论**: ADX>=30 最优，WR+2.8%，月均+2.9%，回撤-3.0%，信号数量合理。

---

## 三、最终参数确认（v2.2引擎 + 180天真实数据）

| 品种 | sc | lc | ccp | adx | tp_s | tp_l | long_dis | WR | Monthly | DD | PF |
|------|----|----|-----|-----|------|------|----------|-----|---------|-----|-----|
| BTCUSDT | 4 | 5 | 0.002 | 22 | 0.8 | 0.8 | No | 60.4% | +8.4% | 16.8% | 1.19 |
| ETHUSDT | 5 | 4 | 0.0015 | 20 | 0.8 | 0.7 | No | 63.0% | +7.7% | 18.9% | 1.26 |
| SOLUSDT | 5 | 4 | 0.0015 | **30** | 0.8 | 0.8 | No | 58.5% | +1.9% | 19.3% | 1.10 |
| BNBUSDT | 5 | 6 | 0.0015 | 15 | 0.8 | 0.8 | Yes | 63.0% | +7.8% | 10.4% | 1.31 |
| LINKUSDT | 7 | 4 | 0.0025 | 15 | 0.8 | 0.7 | No | 67.5% | +8.7% | 12.3% | 1.50 |
| POLUSDT | 5 | 4 | 0.0015 | 25 | 1.0 | 0.7 | Yes | 55.8% | +3.3% | 13.3% | 1.24 |

**注意**: v2.2 Wilder's ATR/ADX 下结果与 v2.1 (EWM) 有所不同，v2.2 更准确（与实盘一致）。

---

## 四、30天样本外前向测试（OOS）

| 品种 | IS WR | IS Monthly | OOS WR | OOS Monthly | 判断 |
|------|-------|-----------|--------|------------|------|
| BTCUSDT | 60.5% | +11.8% | 57.8% | +5.3% | ✅ 通过 |
| ETHUSDT | 65.1% | +14.3% | 64.5% | +16.3% | ✅ 最优 |
| SOLUSDT | 63.2% | +9.8% | 58.7% | +4.4% | ✅ 通过 |
| BNBUSDT | 61.5% | +6.3% | 64.6% | +15.8% | ✅ 最优 |
| LINKUSDT | 66.9% | +8.2% | 70.0% | +17.5% | ✅ 最优 |
| POLUSDT | 54.9% | +4.3% | 50.0% | -0.9% | ⚠️ 边缘 |

6/6 品种样本外 WR≥50%，POLUSDT OOS略弱，建议降低仓位权重。

---

## 五、代码修改摘要

| 文件 | 修改内容 | 影响等级 |
|------|---------|--------|
| `engine/backtest_engine_v2.py` | ATR/ADX EWM→Wilder's | 🔴 Critical |
| `engine/risk_engine.py` | 添加 get_risk_pct() 方法 | 🔴 Critical |
| `scripts/live_scanner.py` | FEE修复+PnL公式+per-symbol params+long_disabled | 🔴 Critical |
| `config/optimal_params_v21.json` | SOL adx_th 25→30，版本升至v2.2.0 | 🟡 Medium |
| `config/strategy_v12_optimized.json` | **新建** 最优参数配置+回测结果+OOS结果 | 🟡 Medium |

---

## 六、风险说明

1. **POLUSDT** OOS月均=-0.9%，建议实盘先观察或降权重（当前capital_weight=0.105已是最低）
2. **v2.2 ATR修复后** 所有品种信号频率降低（约30%），属正常现象，反映了更精准的实盘行为
3. **SOLUSDT** ADX=30 后月均+1.9%（较弱），可考虑进一步优化或降低权重
4. **实盘入场价偏差**（Bug#3未完全修复），建议在实盘运行时统计滑点数据

---

*报告由白夜系统深度优化子Agent生成 | 2026-05-08*
