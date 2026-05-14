"""
白夜交易系统 v7.3 — 配置层（深度优化版）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
融合来源:
  v7.1  — 6品种验证参数、Wilder指标、动态TP、日熔断
  v7.2c — 相关性控制、WRGuard、Kelly、追踪止损、多周期
  v9.3  — SymCfg、6层评分参数、3段追踪、弹性WRGuard、资金费率
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

VERSION = "7.3"

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
MAX_HOLD_BARS      = 30
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
SIGNAL_MIN_SCORE = 2.5   # 6层评分 满分5+2=7，开仓要求≥2.5（v7.3降低提升信号频率）

# ═══════════ 动态TP ═══════════
DYNAMIC_TP_ADX_TH = 35
DYNAMIC_TP_MULT   = 1.5

# ═══════════ 3段追踪止损（v9.3升级）═══════════
TRAIL_BREAKEVEN_ATR  = 0.5   # 浮盈≥0.5ATR → 移至保本
TRAIL_LOCK_ATR       = 1.0   # 浮盈≥1.0ATR → 锁定0.3ATR盈利
TRAIL_DYNAMIC_ATR    = 1.5   # 浮盈≥1.5ATR → 动态追踪
TRAIL_DYNAMIC_DIST   = 0.8   # 动态追踪距当前价 0.8ATR
# 追踪止损开关及阈值（main_v72.py引用）
TRAILING_STOP_ENABLED = True
TRAILING_STOP_THRESH  = 0.6  # 浮盈≥0.6ATR开启追踪
TRAILING_STOP_DIST    = 0.5  # 追踪距离 0.5ATR（保本+少量盈利）

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

# ─── 参数来源：optimize_params.py v2 真实数据网格优化（2026-05-14）───
# 数据源：Binance API 实时拉取 1000根 15m K线
# 优化方法：网格搜索（1920组/品种）+ 综合评分（WR×0.55+PF×0.3+n×0.15）
# 过滤规则：WR≥50%、n≥12、PF≥1.0 方可选入（BNB/POL PF<1，暂停开仓）
# 各品种最优参数（2026-05-14 实时验证）:
#   BTCUSDT  sc=3 adx=15  WR=82.4% PF=1.45 n=68  ✅
#   ETHUSDT  sc=5 adx=15  WR=85.7% PF=1.69 n=63  ✅ 重新启用
#   SOLUSDT  sc=6 adx=25  WR=92.3% PF=5.16 n=13  ✅
#   LINKUSDT sc=4 adx=15  WR=79.4% PF=1.86 n=34  ✅
#   DOTUSDT  sc=4 adx=30  WR=86.7% PF=2.80 n=15  ✅
#   SUIUSDT  sc=6 adx=25  WR=95.2% PF=11.32 n=21 ✅
#   BNBUSDT  sc=3 adx=20  WR=71.7% PF=0.66 n=53  ❌ PF<1 暂停
#   POLUSDT  sc=3 adx=15  WR=70.5% PF=0.78 n=44  ❌ PF<1 暂停
SYM_CFG: Dict[str, SymCfg] = {
    # BTC: sc=3连涨触发SHORT，ADX≥15，tp=0.6ATR（快速止盈锁利）
    "BTCUSDT":  SymCfg(sc=3, lc=4, ccp=0.001,  adx_th=15, tp_mult=0.6, sl_mult=1.5),
    # ETH: 重新启用，sc=5+adx=15，WR=85.7%
    "ETHUSDT":  SymCfg(sc=5, lc=3, ccp=0.0015, adx_th=15, tp_mult=0.6, sl_mult=1.5),
    # SOL: sc=6严格过滤，adx=25，WR=92.3% 最高之一
    "SOLUSDT":  SymCfg(sc=6, lc=5, ccp=0.001,  adx_th=25, tp_mult=0.8, sl_mult=1.5),
    # BNB: PF=0.66 亏钱，暂停（保留配置备用）
    # "BNBUSDT":  SymCfg(sc=3, lc=3, ccp=0.0015, adx_th=20, tp_mult=0.6, sl_mult=1.5, allow_long=False),
    # LINK: sc=4+ccp=0.003严格，禁LONG，WR=79.4% PF=1.86
    "LINKUSDT": SymCfg(sc=4, lc=3, ccp=0.003,  adx_th=15, tp_mult=0.8, sl_mult=1.5, allow_long=False),
    # POL: PF=0.78 亏钱，暂停（保留配置备用）
    # "POLUSDT":  SymCfg(sc=3, lc=3, ccp=0.001, adx_th=15, tp_mult=0.6, sl_mult=1.5, allow_long=False),
    # DOT: sc=4+adx=30严格，WR=86.7% PF=2.80
    "DOTUSDT":  SymCfg(sc=4, lc=4, ccp=0.001,  adx_th=30, tp_mult=0.6, sl_mult=1.0),
    # SUI: sc=6+adx=25，WR=95.2% PF=11.32 超强
    "SUIUSDT":  SymCfg(sc=6, lc=4, ccp=0.001,  adx_th=25, tp_mult=0.6, sl_mult=1.5),
}
DEFAULT_SYM_CFG = SymCfg()
SYMBOLS = list(SYM_CFG.keys())

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
