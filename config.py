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

# ─── 参数来源：validate_realdata.py 180天真实数据验证（2026-05-13）───
# 验证方法：backtest_engine_v3 + Maker手续费(0.02%) + Walk-Forward
# 各品种 WR / PF（Maker费后）:
#   BTCUSDT  sc=4 adx=22  WR=63.8% PF=1.27 月均+1.4%  PASS ✅
#   BNBUSDT  sc=5 adx=15  WR=70.0% PF=1.61 月均+0.6%  PASS ✅
#   LINKUSDT sc=7 adx=25  WR=58.3% PF=1.02 月均+0.03% PASS ✅（长期用LONG禁用）
#   POLUSDT  sc=5 adx=25  WR=65.2% PF=1.30 月均+1.5%  PASS ✅
#   SOLUSDT  sc=5 adx=30  WR=54.1% PF=0.98 月均-0.2%  WARN ⚠️（接近盈亏平衡）
#   ETHUSDT  暂停 — 180天各参数组合均无法盈利，待市场结构改善后重入
SYM_CFG: Dict[str, SymCfg] = {
    # BTC: sc=4连涨触发SHORT，ADX≥22确保趋势，tp=1.8ATR经引擎验证最优
    "BTCUSDT":  SymCfg(sc=4, lc=5, ccp=0.002,  adx_th=22, tp_mult=1.8, sl_mult=1.4),
    # ETH: 暂停 — 180天均值回归失效（单边下跌行情），保留配置但不在SYMBOLS中激活
    # "ETHUSDT":  SymCfg(sc=5, lc=4, ccp=0.0015, adx_th=18, tp_mult=2.0, sl_mult=1.5),
    # SOL: adx_th=30过滤低趋势横盘，减少假信号
    "SOLUSDT":  SymCfg(sc=5, lc=4, ccp=0.0015, adx_th=30, tp_mult=2.2, sl_mult=1.6),
    # BNB: 禁LONG（历史LONG负期望），SHORT WR=70% 最高
    "BNBUSDT":  SymCfg(sc=5, lc=6, ccp=0.0015, adx_th=15, tp_mult=2.0, sl_mult=1.5, allow_long=False),
    # LINK: sc=7严格过滤，禁LONG，tp=2.5但需Maker费才盈利
    "LINKUSDT": SymCfg(sc=7, lc=6, ccp=0.0015, adx_th=25, tp_mult=2.0, sl_mult=1.5, allow_long=False),
    # SUI: 高sc=7防假信号，波动小需ccp=0.0008
    "SUIUSDT":  SymCfg(sc=7, lc=6, ccp=0.0008, adx_th=25, tp_mult=2.0, sl_mult=1.5),
    # POL: 禁LONG，SHORT WR=65.2% 稳定
    "POLUSDT":  SymCfg(sc=5, lc=4, ccp=0.0015, adx_th=25, tp_mult=2.0, sl_mult=1.5, allow_long=False),
    # DOT: 标准参数，待更多数据验证
    "DOTUSDT":  SymCfg(sc=5, lc=4, ccp=0.0015, adx_th=20, tp_mult=2.2, sl_mult=1.5),
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
