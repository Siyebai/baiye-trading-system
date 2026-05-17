# 白夜交易系统 v9.0 FINAL

加密货币量化交易系统 — 全自动纸交易引擎

## 架构

```
baiye-trading-system/
├── main_v90.py          # 单文件全集成引擎 (配置+策略+风控+Dashboard)
├── guardian_v90.py      # 不死守护进程 (自动重启+内存监控+API检测)
├── data/                # 历史K线CSV + 交易记录JSONL
└── logs/                # 运行日志
```

## 特性

- **Numba JIT 241x 加速** — 指标计算 0.03ms vs Python 8ms
- **双策略引擎** — 顺势动量 + 均值回归
- **动态Kelly仓位** — 胜率驱动 0.25x-0.45x 自适应
- **多级熔断** — SOFT(8%DD) / HARD(15%DD) 自动保护
- **弹性WRGuard** — 滚动胜率保护，低胜率自动降级
- **3段追踪止损** — 保本→锁定→动态 递进保护
- **5状态市场识别** — HIGH_VOL/TREND/RANGE/NOISE/BAD_LIQ
- **品种动态权重** — 滚动Sharpe驱动资金分配
- **实时Dashboard** — 终端内持仓/信号/权益/Sharpe 全景
- **钢铁守护** — 崩溃自启+内存监控+API心跳+日志轮转
- **离线CSV回退** — API不可用时自动切换本地180天数据
- **20品种覆盖** — 3级分类，全部参数独立配置

## 验证

| 验证方式 | 交易数 | WR |
|---------|--------|-----|
| offline_replay 180天 | 596笔 | 65.9% |
| fast_100trades | 100笔 | 56.0% |
| v8.4 Walk-Forward OOS | 663笔 | 70.7% PF=1.51 |

## 运行

```bash
# 直接启动
python main_v90.py

# 守护模式 (推荐)
python guardian_v90.py
```

## 策略参数

- **顺势策略**: cu≥2→LONG, cd≥2→SHORT, SL=3.0ATR, TP=1.5ATR
- **均值回归**: sc=3, lc=3, ADX≥15, SL=1.5-2.0ATR
- **风控**: Kelly(动态), WRGuard, CircuitBreaker, CorrFilter
- **资金**: 150U初始, Maker 0.02%, 每笔风险1.5%

## 许可

MIT License
