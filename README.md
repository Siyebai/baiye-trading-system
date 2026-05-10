# 白夜交易系统 v6.8 (Baiye Trading System)

> 🦞 MomReversal 动量反转策略 | 15m/1m双时间框架 | 纸交易验证中

---

## 📌 仓库地址
```
https://github.com/Siyebai/baiye-trading-system
```

## 🚀 快速开始

### 环境要求
- Python 3.10+
- 依赖：`pip install requests numpy pandas`
- Binance API（纸交易模式，不需要真实API Key）

### 启动纸交易（v6.8 稳定版）
```bash
cd killer-trading-system
python3 main_paper_trade.py
```

### 启动 v6.5（历史备份版）
```bash
python3 start_paper_v65.py
```

---

## 📁 目录结构
```
killer-trading-system/
├── main_paper_trade.py          # ★ 主入口：纸交易 v6.8（最新稳定版）
├── start_paper_v65.py           # 纸交易 v6.5（历史备份）
├── start_paper_v6[1-8].py       # 版本演进记录
│
├── engine/                      # 核心引擎
│   ├── backtest_engine_v2.py    # ★ 回测引擎 v2.2（主力，Wilder's ATR/ADX）
│   ├── backtest_engine_v3.py    # 回测引擎 v3（含OOS验证）
│   ├── live_engine.py           # 实盘引擎（Phase6备用）
│   ├── risk_engine.py           # 风险引擎（2%单笔风险，日熔断6%）
│   ├── signal_engine.py         # 信号引擎（MomReversal指标计算）
│   ├── order_executor.py        # 订单执行器
│   └── ws_feeder.py             # WebSocket实时数据馈送
│
├── config/                      # 策略配置
│   ├── strategy_v12_optimized.json  # ★ 主配置（6品种最优参数）
│   ├── v3_validated_params.json     # v3验证参数（含OOS）
│   ├── capital_allocation.yaml      # 资金分配（夏普加权）
│   └── CONFIG_GUIDE.md              # 配置说明
│
├── scripts/                     # 工具脚本
│   ├── live_scanner.py          # 实时信号扫描器
│   ├── deep_validate_v1.py      # 深度验证脚本
│   └── download_180d.py         # 历史数据下载
│
├── logs/                        # 日志（gitignore，本地保存）
├── data/                        # K线数据缓存
├── paper_trades_v65.json        # 纸交易记录（v6.5）
└── CHANGELOG.md                 # 版本更新日志
```

---

## 🎯 策略核心：MomReversal（动量反转）

### 信号逻辑
| 方向 | 触发条件 |
|------|---------|
| **SHORT** | 连涨 ≥ sc 根 + 累涨 ≥ ccp + ADX ≥ adx_th |
| **LONG**  | 连跌 ≥ lc 根 + 累跌 ≥ ccp + ADX ≥ adx_th （部分品种禁用）|

### 执行参数
- **TP** = 0.8 × ATR
- **SL** = 1.0 × ATR  
- **单笔风险** = 资金 × 2%
- **手续费** = 0.02% 单边（Maker限价，BNB抵扣后约0.009%）

### 验证品种（v2.2引擎，180天+OOS）
| 品种 | WR | 月均% | OOS_WR | 参数(sc/lc/ccp/adx) | 备注 |
|------|----|-------|--------|--------------------|----|
| BTCUSDT  | 60.4% | +8.4%  | 57.8% | 4/5/0.002/22  | ✅ |
| ETHUSDT  | 63.0% | +7.7%  | 64.5% | 5/4/0.0015/20 | ✅最优 |
| SOLUSDT  | 58.5% | +1.9%  | 58.7% | 5/4/0.0015/30 | ✅ |
| BNBUSDT  | 63.0% | +7.8%  | 64.6% | 5/6/0.0015/15 | ✅禁LONG |
| LINKUSDT | 67.5% | +8.7%  | 70.0% | 7/4/0.0025/15 | ✅最稳健 |
| POLUSDT  | 55.8% | +3.3%  | 50.0% | 5/4/0.0015/25 | ⚠️边缘 |

### 组合回测（150U本金，180天）
- 终值：265U（+77%）
- 月均：+12.9%
- 最大回撤：16.4%
- 盈利因子：1.24

---

## ⚙️ 配置文件说明

### `config/strategy_v12_optimized.json`
```json
{
  "BTCUSDT": {
    "sc": 4,          // 连涨N根触发SHORT
    "lc": 5,          // 连跌N根触发LONG
    "ccp": 0.002,     // 累计涨跌幅阈值（0.2%）
    "adx_th": 22,     // ADX最低阈值（趋势强度）
    "tp_s": 0.8,      // TP = tp_s × ATR
    "sl_atr": 1.0,    // SL = sl_atr × ATR
    "long_disabled": false  // 是否禁用LONG方向
  }
}
```

### `config/capital_allocation.yaml`
```yaml
# 夏普加权资金分配（总计150U）
LINKUSDT: 21.8%   # 32.7U  最稳定
BTCUSDT:  18.9%   # 28.3U  高弹性
ETHUSDT:  18.3%   # 27.4U
SOLUSDT:  16.6%   # 24.9U
BNBUSDT:  13.9%   # 20.9U
POLUSDT:  10.5%   # 15.7U
```

---

## 🔄 纸交易 v6.8 特性

**相比 v6.5 改进：**
- ✅ 修复 cc 方向切换未重置 Bug
- ✅ SIGTERM 优雅退出（不被守护进程误杀）
- ✅ 每轮全品种信号状态输出
- ✅ 1m K线实时数据（更及时）
- ✅ 统一 FEE=0.0004（Taker）

---

## 🛡️ 风险控制

| 层级 | 规则 |
|------|------|
| 单笔止损 | ≤ 2% 权益 |
| 日熔断 | 单日亏损 ≥ 6% 停止 |
| 最大持仓 | 同时 ≤ 4 个品种 |
| 冷却期 | 同方向同品种 5 根K线 |

---

## 📊 版本历史

| 版本 | 日期 | 关键改进 |
|------|------|---------|
| v6.8 | 2026-05-10 | 稳定守护版，SIGTERM优雅退出，1m实时数据 |
| v6.5 | 2026-05-10 | 7品种高信号量，Maker策略 |
| v2.2引擎 | 2026-05-08 | 修复ATR/ADX算法，消除回测vs实盘21%差异 |
| v2.0引擎 | 2026-05-06 | 修复5个关键Bug，开仓用下根open价 |

详见 [CHANGELOG.md](./CHANGELOG.md)

---

## ⚠️ 免责声明
本系统仅供学习研究，纸交易模式不涉及真实资金。实盘需主人确认。
