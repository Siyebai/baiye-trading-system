# AGENT_GUIDE.md — 智能体测试指南

> 本文档供其他AI Agent测试白夜交易系统使用

## 仓库地址
```
https://github.com/Siyebai/baiye-trading-system
```

## 快速测试（无需API Key）

### 1. 克隆仓库
```bash
git clone https://github.com/Siyebai/baiye-trading-system.git
cd baiye-trading-system
pip install requests numpy pandas
```

### 2. 运行纸交易（主入口）
```bash
python3 main_paper_trade.py
# 等待第一个信号触发（通常1-30分钟内）
# Ctrl+C 优雅退出
# 查看结果：cat logs/paper_v68_trades.json
```

### 3. 运行回测验证
```bash
# 下载180天历史数据
python3 scripts/download_180d.py

# 运行回测（BTC，默认参数）
python3 -c "
from engine.backtest_engine_v2 import BacktestEngine
import json, pandas as pd

with open('data/BTCUSDT_15m_180d.json') as f:
    klines = json.load(f)

df = pd.DataFrame(klines, columns=['ts','open','high','low','close','vol','_','_','_','_','_','_'])
df[['open','high','low','close','vol']] = df[['open','high','low','close','vol']].astype(float)

engine = BacktestEngine(
    symbol='BTCUSDT',
    short_count=4, long_count=5,
    cum_chg_pct=0.002, adx_th=22,
    tp_scale=0.8, sl_scale=1.0,
    long_disabled=False
)
result = engine.run(df, capital=150.0)
print(f'WR={result[\"win_rate\"]*100:.1f}% 月均={result[\"monthly_return\"]*100:.1f}%')
"
```

### 4. 检查信号输出格式
纸交易每轮输出格式：
```
2026-05-10 09:00:00 [09:00:00] 完成=5/10 WR=60% PnL=+1.234U 权益=151.23U | 持仓=ETHUSDT(做空@2500.00)
2026-05-10 09:00:00   BTCUSDT: ADX=25.3 | SHORT=[cu3/4|cc0.15%/0.20%] LONG=[cd0/5|cc-0.00%/-0.20%]
```

## 核心文件路径
| 文件 | 说明 |
|------|------|
| `main_paper_trade.py` | ★ 主入口 |
| `engine/backtest_engine_v2.py` | 回测引擎（含ATR/ADX Wilder's算法）|
| `engine/risk_engine.py` | 风险控制 |
| `config/strategy_v12_optimized.json` | 6品种最优参数 |
| `config/v3_validated_params.json` | OOS验证参数 |
| `paper_trades_v65.json` | 历史纸交易记录 |

## 已知问题（已修复）
- ~~Bug#1: pgrep误检测Gateway PID~~ → sys_guardian v3.1已修复
- ~~Bug#2: cc方向切换未重置~~ → v6.8已修复
- ~~Bug#3: ATR/ADX非Wilder's平滑~~ → v2.2引擎已修复
- ~~Bug#4: 手续费700倍误差~~ → FEE=0.0009已修正

## 测试建议
1. 先运行回测验证策略参数合理性
2. 再启动纸交易，等待至少10笔完成
3. 对比纸交易WR与回测WR（差距应<10%）
4. 检查日志 `logs/paper_v68.log` 排查异常
