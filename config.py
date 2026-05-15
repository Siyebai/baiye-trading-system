"""
白夜交易系统 v8.0 — 配置层（深度优化版）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
融合来源:
  v7.1  — 6品种验证参数、Wilder指标、动态TP、日熔断
  v7.2c — 相关性控制、WRGuard、Kelly、追踪止损、多周期
  v9.3  — SymCfg、6层评分参数、3段追踪、弹性WRGuard、资金费率
  v8.0  — 2026-05-15真实数据优化: 10品种最优参数、TP压低修复TIMEOUT
           optimize_params.py v2: 1920组/品种网格 + 实时Binance K线
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

VERSION = "8.0"

# ═══════════ 交易所 ═══════════
EXCHANGE         = "binance"
BINANCE_API_KEY  = "zv6mpAUG7avCTk9IUztR8Ysegyj3AgIPDEnZt31ycA4600msoQlwiU358jMse3w1"
BINANCE_SECRET   = "JgtCa5lfjqf51Gj4XeOmGJWDwcITNBFm51eXXDyAXeg2FNZQ5hi9hLDcrx0EkG2Y"
BINANCE_TESTNET  = False
BINANCE_BASE_URL = ("https://testnet.binancefuture.com"
                    if BINANCE_TESTNET else "https://fapi.binance.com")

# ═══════════ 运行模式 ═══════════
RUN_MODE = "paper"   # paper | shadow | live

# ═══════════ 资金 ═══════════
INITIAL_EQUITY = 150.0
LEVERAGE       = 1

# ═══════════ 手续费 ═══════════
FEE_MAKER = 0.0002
FEE_TAKER = 0.0004
FEE       = FEE_MAKER

# ═══════════ 风控 ═══════════
DAILY_LOSS_PCT     = 0.06
MAX_OPEN_POSITIONS = 7
MAX_HOLD_BARS      = 25   # v8.0: 30→25，减少TIMEOUT率
COOLDOWN_BARS      = 5
MIN_NOTIONAL       = 5.0
MIN_RR_RATIO       = 1.5

# ═══════════ 多周期 ═══════════
TIMEFRAMES  = ["3m", "5m", "15m", "1h"]
TF_PRIMARY  = "15m"
TF_CONFIRM  = "5m"
TF_FILTER   = "1h"
TF_FAST     = "3m"
KLINE_LIMIT = 500
POLL_SECS   = 30

# ═══════════ 指标参数（v9.3新增）═══════════
EMA_FAST    = 9
EMA_MID     = 21
EMA_SLOW    = 55
RSI_PERIOD  = 14
RSI_LONG_MIN  = 45;  RSI_LONG_MAX  = 68
RSI_SHORT_MIN = 32;  RSI_SHORT_MAX = 55
MACD_FAST   = 12;   MACD_SLOW  = 26;  MACD_SIG = 9
ATR_PERIOD  = 14
ADX_MIN     = 18.0
ATR_VOL_MIN = 0.0025   # ATR/price 最低波动率
ATR_VOL_MAX = 0.025    # ATR/price 最高波动率（过滤极端波动）

# ═══════════ 信号评分 ═══════════
SIGNAL_MIN_SCORE = 2.0   # v8.0: 2.5→2.0，信号频率优先，由WRGuard把关质量

# ═══════════ 动态TP ═══════════
DYNAMIC_TP_ADX_TH = 30   # v8.0: 35→30，更多交易触发DTP
DYNAMIC_TP_MULT   = 1.3  # v8.0: 1.5→1.3，适配低基准tp_mult

# ═══════════ 3段追踪止损（v9.3升级）═══════════
TRAIL_BREAKEVEN_ATR  = 0.5   # 浮盈≥0.5ATR → 移至保本
TRAIL_LOCK_ATR       = 1.0   # 浮盈≥1.0ATR → 锁定0.3ATR盈利
TRAIL_DYNAMIC_ATR    = 1.5   # 浮盈≥1.5ATR → 动态追踪
TRAIL_DYNAMIC_DIST   = 0.8   # 动态追踪距当前价 0.8ATR
# 追踪止损开关及阈值（main_v72.py引用）
TRAILING_STOP_ENABLED = True
TRAILING_STOP_THRESH  = 0.5  # v8.0: 0.6→0.5，更早锁盈
TRAILING_STOP_DIST    = 0.4  # v8.0: 0.5→0.4ATR，收紧追踪距离

# ═══════════ 相关性控制 ═══════════
HIGH_CORR_GROUP    = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "DOTUSDT"}
MAX_CORR_SAME_SIDE = 2

# ═══════════ 弹性WRGuard（v9.3升级）═══════════
WR_GUARD_WINDOW   = 30
WR_GUARD_MIN_WR   = 0.42
WR_GUARD_BOOST_WR = 0.62
WR_GUARD_MIN_RR   = 2.0
WR_GUARD_PAUSE_WR = 0.25   # WR<25% 完全暂停开仓

# ═══════════ Kelly ═══════════
KELLY_ENABLED    = True
KELLY_FRACTION   = 0.25
KELLY_MIN_TRADES = 20
KELLY_MAX_RISK   = 0.04
RISK_PCT         = 0.02

# ═══════════ 资金费率过滤（v9.3新增）═══════════
FUNDING_SKIP_RATE   = 0.001    # |费率|≥0.1% 跳过开仓
FUNDING_SKIP_WINDOW = 1800     # 距结算<30min 跳过开仓
FUNDING_UPDATE_SEC  = 900      # 每15min刷新费率

# ═══════════ SymCfg — 品种个性化（v9.3新增）═══════════
@dataclass(frozen=True)
class SymCfg:
    sc:          int   = 5      # 连涨根数触发SHORT
    lc:          int   = 4      # 连跌根数触发LONG
    ccp:         float = 0.0015 # 累涨跌幅阈值
    adx_th:      float = 18.0   # ADX最低门槛
    tp_mult:     float = 2.0    # 止盈ATR倍数
    sl_mult:     float = 1.5    # 止损ATR倍数
    allow_long:  bool  = True
    allow_short: bool  = True

# ─── v8.1 参数来源：Walk-Forward 663笔OOS验证 + 品种筛选（2026-05-15）───
# 验证方法: Binance实时1500根K线，IS=500 OOS=1000
# 筛选规则: OOS期PF>1.0方开仓，PF<1的品种在当前市场屚无效
#
# 有效品种 (PF>1经过OOS验证):
#   SUIUSDT  WR=86.4% PF=3.43 n=22  🔥
#   TONUSDT  WR=72.8% PF=1.51 n=92  🔥 tp=0.8升级
#   POLUSDT  WR=73.1% PF=1.62 n=26  🔥
#   SOLUSDT  WR=75.0% PF=1.38 n=16  ✅
#   DOGEUSDT WR=63.4% PF=1.34 n=71  ✅ tp=1.5/sl=1.8优化
#   DOTUSDT  WR=60.0% PF=1.16 n=20  ✅ tp=0.8/sl=1.0优化
#   BTCUSDT  WR=75.0% PF=1.20 n=12  ✅ adx提高35过滤越势行情
# 暂停品种 (OOS期PF<1，当前市场均值回归不适用):
#   ETH/XRP/LINK/HYPE → 尚在列表但设高门槛降低仓位质
SYM_CFG: Dict[str, SymCfg] = {
    # BTC: adx>=35过滤越势行情 WR=75% PF=1.20 n=12
    "BTCUSDT":  SymCfg(sc=3, lc=4, ccp=0.001,  adx_th=35, tp_mult=0.8, sl_mult=1.5),
    # ETH: OOS PF<1 全市场失效，高ADX门槛+低占仓待察
    "ETHUSDT":  SymCfg(sc=5, lc=3, ccp=0.0015, adx_th=35, tp_mult=0.8, sl_mult=1.5),
    # SOL: WR=75% PF=1.38 n=16 ✅
    "SOLUSDT":  SymCfg(sc=7, lc=5, ccp=0.001,  adx_th=15, tp_mult=0.8, sl_mult=1.5),
    # XRP: OOS PF<1，将adx提高30降低风险
    "XRPUSDT":  SymCfg(sc=3, lc=3, ccp=0.001,  adx_th=30, tp_mult=0.8, sl_mult=1.5),
    # DOGE: tp=1.5/sl=1.8优化 WR=63% PF=1.34 n=71 ✅
    "DOGEUSDT": SymCfg(sc=3, lc=3, ccp=0.001,  adx_th=15, tp_mult=1.5, sl_mult=1.8),
    # LINK: OOS PF<1，将adx提高35
    "LINKUSDT": SymCfg(sc=4, lc=3, ccp=0.003,  adx_th=35, tp_mult=0.8, sl_mult=1.5, allow_long=False),
    # DOT: tp=0.8/sl=1.0优化 WR=60% PF=1.16 n=20 ✅
    "DOTUSDT":  SymCfg(sc=4, lc=4, ccp=0.001,  adx_th=30, tp_mult=0.8, sl_mult=1.0),
    # SUI: 旗舰 WR=86% PF=3.43 🔥
    "SUIUSDT":  SymCfg(sc=6, lc=4, ccp=0.001,  adx_th=25, tp_mult=0.6, sl_mult=1.5),
    # TON: WR=73% PF=1.51 n=92 tp=0.8升级 🔥
    "TONUSDT":  SymCfg(sc=3, lc=3, ccp=0.001,  adx_th=15, tp_mult=0.8, sl_mult=1.5),
    # HYPE: OOS PF<1，高ADX门槛
    "HYPEUSDT": SymCfg(sc=3, lc=3, ccp=0.001,  adx_th=35, tp_mult=0.8, sl_mult=1.5),
    # POL: WR=73% PF=1.62 禁LONG 🔥
    "POLUSDT":  SymCfg(sc=3, lc=3, ccp=0.001,  adx_th=25, tp_mult=1.2, sl_mult=1.5, allow_long=False),
}
SYMBOLS = list(SYM_CFG.keys())
VERSION = "8.1"

# 兼容层：将SymCfg转换为dict格式（main_v72.py使用dict访问）
SYMBOL_CONFIGS = {
    sym: {
        "sc":            sc.sc,
        "lc":            sc.lc,
        "ccp":           sc.ccp,
        "adx_th":        sc.adx_th,
        "tp_s":          sc.tp_mult,  # tp_mult即tp_s
        "sl_atr":        sc.sl_mult,  # sl_mult即sl_atr
        "long_disabled": not sc.allow_long,
        "short_disabled":not sc.allow_short,
    }
    for sym, sc in SYM_CFG.items()
}

# ═══════════ 文件路径 ═══════════
_BASE    = Path(__file__).parent
LOG_FILE   = str(_BASE / "logs" / "baiye_v73.log")
STATE_FILE = str(_BASE / "data" / "state_v73.json")
TRADE_LOG  = str(_BASE / "data" / "trades_v73.jsonl")
PID_FILE   = str(_BASE / "data" / "baiye_v73.pid")

# ═══════════ 校验 ═══════════
def validate() -> bool:
    if RUN_MODE not in ("paper", "shadow", "live"):
        raise ValueError(f"RUN_MODE非法: {RUN_MODE}")
    if RUN_MODE == "live" and not BINANCE_API_KEY:
        raise ValueError("实盘模式需配置API Key")
    if not SYM_CFG:
        raise ValueError("SYM_CFG为空")
    return True
