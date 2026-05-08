# 纸交易深度分析报告
**日期**: 2026-05-08 | **引擎**: paper_trading_v3 (v2.2参数)

## 7笔纸交易分析

| # | 品种 | 方向 | 结果 | PnL | 分析 |
|---|------|------|------|-----|------|
| 1 | BNBUSDT | SHORT | ✅WIN | +1.91U | ADX强，TP顺利触及 |
| 2 | POLUSDT | SHORT | ✅WIN | +2.82U | HIGH ADX=36，强趋势 |
| 3 | SOLUSDT | SHORT | ❌LOSS | -3.34U | SOL反弹逆势，旧adx25过低 |
| 4 | LINKUSDT | SHORT | ✅WIN | +2.28U | 正常执行 |
| 5 | SOLUSDT | SHORT | ❌LOSS | -3.26U | 连续SL，冷却机制触发 |
| 6 | SOLUSDT | SHORT | ✅WIN | +2.16U | 冷却后重入，趋势恢复 |
| 7 | BNBUSDT | SHORT | ✅WIN | +1.92U | BNB多次稳定获利 |

**总计**: WR=71.4%, PnL=+4.48U (+2.99%), 手续费=2.12U

## WIN特征
- ADX普遍>30，趋势强
- BNB/LINK/POL SHORT方向与市场空头一致
- 平均持仓约30-75分钟

## LOSS特征
- SOL连续2次SL：行情短期反弹，adx=25过低（已更新为30）
- 入场时市场ADX=25-28，处于临界值

## 修复行动
- ✅ SOL adx_th: 25 → 30（已更新并重启）
- ✅ 冷却机制正常工作（第5笔LOSS后有冷却期）
