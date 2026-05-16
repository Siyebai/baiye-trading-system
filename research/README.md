# research/ — 研究数据索引

本目录保存所有历史优化、验证、回测的结果数据。

## 文件说明

| 文件 | 版本 | 说明 |
|------|------|------|
| `optimize_v84.json`       | v8.4 | **最新** 8品种 Sharpe目标函数优化结果，IS=400 OOS=1100根，含WR/PF/Sharpe/MaxDD |
| `optimize_result_v2.json` | v8.0 | 11品种 1920组/品种 网格搜索结果，Binance实时1000根15m K线 |
| `optimize_extra.json` | v8.0 | 新增4品种(SUI/TON/HYPE/POL)最优参数 |
| `validate_100_v8.json` | v8.1 | 663笔 Walk-Forward OOS验证结果，15m 1500根，IS=500 OOS=1000 |
| `deep_test_v81.json` | v8.2 | 成交量过滤+RSI方向过滤 8品种对比实验结果 |
| `deep_test_v82.json` | v8.3 | 追踪止损门槛(0.3/0.4/0.5/0.6) + 动态TP实验结果 |
| `validate_realdata_result.json` | v7.3 | 大规模真实数据回测 (~672K行) |
| `100trades_validation_result.json` | v7.2 | 100笔完整闭环验证 |
| `stability_report_v21.json` | v7.2 | 策略稳定性测试 |
| `walkforward_results.json` | v7.1 | 早期Walk-Forward结果 |
| `180d_backtest_results.json` | v7.0 | 180天历史回测基准 |
| `short_signal_research.py` | v6.x | SHORT信号研究脚本 |

## 关键结论时间线

```
v7.1 → v7.2: Kelly+WRGuard+追踪止损 加入，WR从50%→58%
v7.2 → v7.3: 多周期并发，EMA200距离保护，资金费率过滤
v7.3 → v8.0: TIMEOUT率58%→0.2% (tp压低修复)
v8.0 → v8.1: 663笔OOS验证，7品种有效 WR=70.7% PF=1.51
v8.1 → v8.2: SUI成交量过滤PF3.43→5.05，TON RSI过滤稳定1.52
v8.2 → v8.3: TON追踪止损trl=0.4 PF=7.31, POL trl=0.6 PF=3.00
```
