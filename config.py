"""
白夜交易系统 v7.2 — 配置层
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
融合来源:
  v7.1  — 6品种验证参数、Wilder指标、动态TP、日熔断
  config.py(v7.2草稿) — 相关性控制、WRGuard、Kelly、追踪止损
  v9.3  — 多周期框架、信号评分、shadow模式、状态CRC校验
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

# ═══════════ 版本 ═══════════
VERSION = "7.2"

# ═══════════ 交易所 ═══════════
EXCHANGE          = "binance"
BINANCE_API_KEY   = "zv6mpAUG7avCTk9IUztR8Ysegyj3AgIPDEnZt31ycA4600msoQlwiU358jMse3w1"
BINANCE_SECRET    = "JgtCa5lfjqf51Gj4XeOmGJWDwcITNBFm51eXXDyAXeg2FNZQ5hi9hLDcrx0EkG2Y"
BINANCE_TESTNET   = False
BINANCE_BASE_URL  = ("https://testnet.binancefuture.com"
                     if BINANCE_TESTNET else "https://fapi.binance.com")

# ═══════════ 运行模式 ═══════════
# paper: 无真实下单 | shadow: 实盘镜像（只读）| live: 实盘（需主人确认）
RUN_MODE = "paper"

# ═══════════ 资金 ═══════════
INITIAL_EQUITY = 150.0    # 初始资金 U
LEVERAGE       = 1        # paper不用杠杆；live再调

# ═══════════ 手续费 ═══════════
FEE_MAKER = 0.0002        # Maker限价单 0.02% 单边
FEE_TAKER = 0.0004        # Taker市价单 0.04% 单边（保守回测用）
FEE       = FEE_MAKER     # 默认用Maker

# ═══════════ 风控 ═══════════
DAILY_LOSS_PCT       = 0.06   # 日熔断：单日亏损≥6%权益停止
MAX_OPEN_POSITIONS   = 7      # 最大同时持仓（7品种各1）
MAX_HOLD_BARS        = 30     # 最多持仓30根K线，超时强制平仓
COOLDOWN_BARS        = 5      # 同品种同方向冷却K线数
MIN_NOTIONAL         = 5.0    # 最小名义值 U
MIN_RR_RATIO         = 1.5    # 最低盈亏比（开仓过滤）

# ═══════════ 多周期配置（v7.2核心新增）═══════════
# 短线布局：3m快速、5m标准、15m核心、60m趋势确认
TIMEFRAMES = ["3m", "5m", "15m", "1h"]
TF_WEIGHTS = {
    "3m":  0.10,   # 超短线，权重低（噪音多）
    "5m":  0.25,   # 短线主力
    "15m": 0.45,   # 核心周期（经验证最稳）
    "1h":  0.20,   # 趋势过滤（Binance期货用1h非60m）
}
TF_PRIMARY    = "15m"      # 主周期（信号生成）
TF_CONFIRM    = "1h"       # 趋势确认周期
TF_FAST       = "5m"       # 快速信号辅助
KLINE_LIMIT   = 500        # K线数量（EMA200需200+）
POLL_SECS     = 30         # 轮询间隔（秒）

# ═══════════ 信号评分（v9.3融合）═══════════
SIGNAL_MIN_SCORE  = 3.0    # 最低信号综合评分（0-10）
# 评分项权重（在主引擎中计算）:
#   ADX强度(0-3) + 多周期共振(0-3) + RR比(0-2) + 趋势对齐(0-2)

# ═══════════ 品种参数（v7.1 MEMORY验证最优值）═══════════
# sc=连涨根数触发SHORT | lc=连跌根数触发LONG
# ccp=累涨跌幅阈值 | adx_th=ADX最低门槛
# tp_s=止盈ATR倍数 | sl_atr=止损ATR倍数 | long_disabled=禁多
SYMBOL_CONFIGS = {
    "BTCUSDT": dict(sc=4, lc=5, ccp=0.002,  adx_th=22, tp_s=0.8, sl_atr=1.0, long_disabled=False),
    "ETHUSDT": dict(sc=5, lc=4, ccp=0.0015, adx_th=20, tp_s=0.8, sl_atr=1.0, long_disabled=False),
    "SOLUSDT": dict(sc=5, lc=4, ccp=0.0015, adx_th=30, tp_s=0.8, sl_atr=1.0, long_disabled=False),
    "BNBUSDT": dict(sc=5, lc=6, ccp=0.0015, adx_th=15, tp_s=0.8, sl_atr=1.0, long_disabled=True),
    "LINKUSDT":dict(sc=7, lc=4, ccp=0.0025, adx_th=15, tp_s=1.2, sl_atr=1.0, long_disabled=False),
    "SUIUSDT": dict(sc=7, lc=6, ccp=0.0008, adx_th=30, tp_s=0.8, sl_atr=1.0, long_disabled=False),
    "POLUSDT": dict(sc=5, lc=4, ccp=0.0015, adx_th=25, tp_s=0.8, sl_atr=1.0, long_disabled=True),
    # 候选扩展（OOS待验证）: AVAXUSDT, ARBUSDT, OPUSDT, NEARUSDT, INJUSDT
}
SYMBOLS = list(SYMBOL_CONFIGS.keys())

# ═══════════ 动态TP（v7.0/v7.1继承）═══════════
DYNAMIC_TP_ADX_TH = 35     # ADX>35 时启用动态TP
DYNAMIC_TP_MULT   = 1.5    # 强趋势TP扩大倍数

# ═══════════ 追踪止损（v7.2新增）═══════════
TRAILING_STOP_ENABLED = True
TRAILING_STOP_THRESH  = 0.5   # 浮盈≥0.5×ATR时激活追踪
TRAILING_STOP_DIST    = 0.4   # 追踪止损距离：0.4×ATR

# ═══════════ 相关性控制（v7.2融合）═══════════
HIGH_CORR_GROUP    = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
MAX_CORR_SAME_SIDE = 2        # 高相关品种同侧最多2个

# ═══════════ WinRate Guard（v7.2融合）═══════════
WR_GUARD_WINDOW   = 30        # 观察最近N笔
WR_GUARD_MIN_WR   = 0.42      # 低于42%触发守卫（提高RR要求）
WR_GUARD_MIN_RR   = 2.0       # 守卫模式RR≥2.0
WR_GUARD_BOOST_WR = 0.60      # 恢复到60%后解除守卫

# ═══════════ Kelly 仓位（v7.2融合）═══════════
KELLY_ENABLED     = True
KELLY_FRACTION    = 0.25      # 25% Kelly（保守）
KELLY_MIN_TRADES  = 20        # ≥20笔才启用Kelly
KELLY_MAX_RISK    = 0.04      # Kelly上限4%
RISK_PCT          = 0.02      # Kelly未启用时默认风险2%

# ═══════════ 文件路径 ═══════════
from pathlib import Path
_BASE = Path(__file__).parent
LOG_FILE    = str(_BASE / "logs" / "baiye_v72.log")
STATE_FILE  = str(_BASE / "data" / "state_v72.json")
TRADE_LOG   = str(_BASE / "data" / "trades_v72.jsonl")
PID_FILE    = str(_BASE / "data" / "baiye_v72.pid")

# ═══════════ 校验 ═══════════
def validate() -> bool:
    if RUN_MODE not in ("paper", "shadow", "live"):
        raise ValueError(f"RUN_MODE非法: {RUN_MODE}")
    if RUN_MODE == "live" and not BINANCE_API_KEY:
        raise ValueError("实盘模式需配置API Key")
    if not SYMBOL_CONFIGS:
        raise ValueError("SYMBOL_CONFIGS为空")
    for tf, w in TF_WEIGHTS.items():
        if tf not in TIMEFRAMES:
            raise ValueError(f"TF_WEIGHTS包含未知周期: {tf}")
    return True
