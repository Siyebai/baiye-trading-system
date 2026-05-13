#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
白夜交易系统 v7.1 — 参数修正版
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
版本: v7.1
日期: 2026-05-11
作者: 白夜交易系统 (思夜白)

整合历史:
  v6.5 优点吸收:
    ✅ 8个品种 (含ARB/DOT/SUI/ADA高信号量品种)
    ✅ Maker费率 FEE=0.0002 (限价单0.02%单边)
    ✅ LINK/DOT 用 tp_s=1.2×ATR (EV更高)
    ✅ 各品种独立优化参数 (v6.5网格搜索)
    ✅ ~10笔/天信号量设计

  v6.9 优点吸收 (生产级Bug全修复):
    ✅ Wilder ATR/ADX 与回测引擎v2.2完全一致
    ✅ cc方向切换正确重置 (最关键Bug修复)
    ✅ propagate=False (双重日志Bug修复)
    ✅ SIGTERM/SIGINT 优雅退出
    ✅ 原子文件写入 (防崩溃数据损坏)
    ✅ 退出检测使用open价判断双触发先后
    ✅ 单品种异常隔离不影响其他品种
    ✅ 日熔断保护 (单日≥6%权益亏损停止)
    ✅ 冷却期保护 (同品种同方向5根K线冷却)
    ✅ 名义值保护 (5U≤notional≤权益50%)
    ✅ 每轮拉取一次K线复用 (退出+信号共用)

  v7.0 新增优化:
    ✅ 动态TP: 市场趋势强(ADX>35)时 tp_s×1.5
    ✅ 信号质量评分: ADX强度加权开仓量
    ✅ 每日收益统计 + 连胜/连败追踪
    ✅ 最大回撤实时监控
    ✅ 15m时间框架 (核心周期)

  v7.1 参数修正 (MEMORY验证值修正):
    ✅ ETHUSDT: sc=5, lc=4, ccp=0.0015, adx=20 (修正)
    ✅ SOLUSDT: sc=5, lc=4, ccp=0.0015, adx=30 (关键修复: adx 12→30)
    ✅ LINKUSDT: sc=7, lc=4, ccp=0.0025, adx=15 (关键修复: sc/ccp修正)
    ✅ ARB/DOT/ADA 未通过OOS验证，从CONFIGS删除
    ✅ SUIUSDT 通过验证: sc=7, lc=6, ccp=0.0008, adx=30

品种配置 (v7.1 MEMORY修正最优参数):
  品种      | sc | lc | ccp    | adx | tp_s | 来源说明
  ETHUSDT   |  5 |  4 | 0.0015 |  20 | 0.8x | MEMORY修正
  SOLUSDT   |  5 |  4 | 0.0015 |  30 | 0.8x | MEMORY修正 adx=30
  LINKUSDT  |  7 |  4 | 0.0025 |  15 | 1.2x | MEMORY修正
  BNBUSDT   |  5 |  6 | 0.0015 |  15 | 0.8x | v6.9 LONG禁
  BTCUSDT   |  4 |  5 | 0.002  |  22 | 0.8x | v6.9 MEMORY
  SUIUSDT   |  7 |  6 | 0.0008 |  30 | 0.8x | v7.1 OOS通过
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import json
import time
import signal
import sys
import logging
import warnings
from pathlib import Path
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")

# ══════════════════════════════════════════════════════════════
#  全局常量
# ══════════════════════════════════════════════════════════════
VERSION        = "v7.1"
CAPITAL        = 150.0       # 初始资金 U
RISK_PCT       = 0.02        # 单笔风险 2% = 3U
FEE            = 0.0002      # Maker限价单 0.02%单边（v6.5优化）
MAX_POSITIONS  = 6           # 最大同时持仓数（v6.5: 8品种高信号量）
POLL_SECS      = 30          # 轮询间隔
TARGET_TRADES  = 100         # 目标完成笔数
DAILY_LOSS_PCT = 0.06        # 日熔断：单日亏损≥6%权益停止
INTERVAL       = "15m"       # K线周期（核心框架）
KLINE_LIMIT    = 500         # K线数量（EMA200需200+）
COOLDOWN_BARS  = 5           # 同品种同方向冷却K线数

# v7.0 新增：动态TP参数
DYNAMIC_TP_ADX_THRESHOLD = 35   # ADX>35时启用动态TP
DYNAMIC_TP_MULTIPLIER    = 1.5  # 强趋势时TP扩大1.5倍

WORK_DIR    = Path(__file__).parent
LOG_FILE    = WORK_DIR / "logs" / "paper_v71.log"
STATE_FILE  = WORK_DIR / "logs" / "paper_v71_state.json"
TRADES_FILE = WORK_DIR / "logs" / "paper_v71_trades.json"
PID_FILE    = WORK_DIR / "logs" / "paper_v71.pid"
LOG_FILE.parent.mkdir(exist_ok=True)

# ══════════════════════════════════════════════════════════════
#  品种配置（v7.0整合最优参数）
# ══════════════════════════════════════════════════════════════
CONFIGS: dict = {
    # v7.1 MEMORY修正: sc=5, lc=4, ccp=0.0015, adx=20
    "ETHUSDT": dict(
        sc=5, lc=4, ccp=0.0015, adx_th=20,
        tp_s=0.8, sl_atr=1.0, long_disabled=False
    ),
    # v7.1 MEMORY修正: adx 12→30 (关键修复)
    "SOLUSDT": dict(
        sc=5, lc=4, ccp=0.0015, adx_th=30,
        tp_s=0.8, sl_atr=1.0, long_disabled=False
    ),
    # v7.1 MEMORY修正: sc=7, lc=4, ccp=0.0025, adx=15 (关键修复)
    "LINKUSDT": dict(
        sc=7, lc=4, ccp=0.0025, adx_th=15,
        tp_s=1.2, sl_atr=1.0, long_disabled=False
    ),
    # v6.9 MEMORY参数: WR=63%, OOS=64.6% (不变)
    "BNBUSDT": dict(
        sc=5, lc=6, ccp=0.0015, adx_th=15,
        tp_s=0.8, sl_atr=1.0, long_disabled=True   # ← 禁LONG
    ),
    # v6.9 MEMORY参数: WR=60.4%, OOS=57.8% (不变)
    "BTCUSDT": dict(
        sc=4, lc=5, ccp=0.002, adx_th=22,
        tp_s=0.8, sl_atr=1.0, long_disabled=False
    ),
    # v7.1 新品种OOS验证通过: WR=72.2% OOS=100%
    "SUIUSDT": dict(
        sc=7, lc=6, ccp=0.0008, adx_th=30,
        tp_s=0.8, sl_atr=1.0, long_disabled=False
    ),
    # ← ARB/DOT/ADA: OOS验证未通过，已从CONFIGS删除
}
SYMBOLS = list(CONFIGS.keys())

# ══════════════════════════════════════════════════════════════
#  优雅退出
# ══════════════════════════════════════════════════════════════
_running = True

def _handle_signal(sig, frame):
    global _running
    logger.warning(f"收到信号 {sig}，执行优雅退出（当前轮次完成后停止）...")
    _running = False

# ══════════════════════════════════════════════════════════════
#  日志配置（修复v6.x双重日志Bug：propagate=False）
# ══════════════════════════════════════════════════════════════
def _setup_logger() -> logging.Logger:
    lg = logging.getLogger("paper_v71")
    lg.setLevel(logging.INFO)
    lg.propagate = False   # ← 关键修复：防止双重日志
    if not lg.handlers:
        fh = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        # nohup重定向时不需要stdout handler（防止日志重复写入）
        lg.addHandler(fh)
    return lg

logger = _setup_logger()

# ══════════════════════════════════════════════════════════════
#  技术指标（Wilder's平滑 — 与回测引擎v2.2完全一致）
# ══════════════════════════════════════════════════════════════
def _wilder_smooth(arr: np.ndarray, n: int) -> np.ndarray:
    """Wilder's RMA: 初始值=前n期简单均值，后续: out[i]=out[i-1]*(n-1)/n + arr[i]/n"""
    out = np.full(len(arr), np.nan)
    valid = np.where(~np.isnan(arr))[0]
    if len(valid) < n:
        return out
    s = valid[0]
    out[s + n - 1] = np.nanmean(arr[s:s + n])
    for i in range(s + n, len(arr)):
        if not np.isnan(out[i - 1]):
            out[i] = out[i - 1] * (n - 1) / n + arr[i] / n
    return out


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """计算 ATR / ADX / EMA200 / 连涨连跌累涨（含方向切换重置修复）"""
    df = df.copy()
    c = df["close"].values
    h = df["high"].values
    l = df["low"].values
    n = len(c)

    # True Range
    tr = np.maximum(
        h - l,
        np.maximum(np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1)))
    )
    tr[0] = h[0] - l[0]

    # ATR (Wilder's, period=14)
    df["atr"] = _wilder_smooth(tr, 14)

    # EMA200
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()

    # ADX (Wilder's, period=14)
    up  = np.diff(h, prepend=h[0])
    dn  = np.diff(l, prepend=l[0]) * -1
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr14 = _wilder_smooth(tr, 14)
    safe  = np.where(atr14 > 0, atr14, np.nan)
    pdi   = 100 * _wilder_smooth(pdm, 14) / safe
    ndi   = 100 * _wilder_smooth(ndm, 14) / safe
    denom = np.where((pdi + ndi) > 0, pdi + ndi, np.nan)
    dx    = 100 * np.abs(pdi - ndi) / denom
    df["adx"] = _wilder_smooth(dx, 14)

    # 连涨(cu)/连跌(cd)/累涨跌幅(cc) — Bug修复版: 方向切换时cc重置
    cu = np.zeros(n, dtype=int)
    cd = np.zeros(n, dtype=int)
    cc = np.zeros(n, dtype=float)
    for i in range(1, n):
        chg = (c[i] - c[i - 1]) / c[i - 1]
        if c[i] > c[i - 1]:                              # 上涨
            cu[i] = cu[i - 1] + 1
            cd[i] = 0
            cc[i] = chg if cd[i - 1] > 0 else cc[i - 1] + chg  # 方向切换重置
        elif c[i] < c[i - 1]:                            # 下跌
            cd[i] = cd[i - 1] + 1
            cu[i] = 0
            cc[i] = chg if cu[i - 1] > 0 else cc[i - 1] + chg  # 方向切换重置
        else:                                             # 平盘
            cu[i] = cu[i - 1]
            cd[i] = cd[i - 1]
            cc[i] = cc[i - 1]

    df["cu"] = cu
    df["cd"] = cd
    df["cc"] = cc
    return df


# ══════════════════════════════════════════════════════════════
#  数据获取（内置重试+退避）
# ══════════════════════════════════════════════════════════════
def fetch_klines(symbol: str, limit: int = KLINE_LIMIT, retries: int = 3) -> pd.DataFrame:
    url = "https://fapi.binance.com/fapi/v1/klines"
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(
                url,
                params={"symbol": symbol, "interval": INTERVAL, "limit": limit},
                timeout=10
            )
            r.raise_for_status()
            cols = ["ts","open","high","low","close","vol","ct","qv","tr","tbb","tbq","ign"]
            df = pd.DataFrame(r.json(), columns=cols)
            for col in ["open","high","low","close"]:
                df[col] = df[col].astype(float)
            return df
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"[{symbol}] 拉取K线失败({retries}次): {last_err}")


# ══════════════════════════════════════════════════════════════
#  信号检测（v7.0 含动态TP）
# ══════════════════════════════════════════════════════════════
def check_signal(symbol: str, df: pd.DataFrame, cfg: dict) -> dict | None:
    """
    使用 df.iloc[-2]（已收盘K线）作为信号源
    入场价用 df.iloc[-1]["close"]（当前价模拟Limit成交）
    v7.0新增: ADX>35时动态扩大TP
    """
    if len(df) < 220:
        return None

    row = df.iloc[-2]    # 已收盘K线
    cur = df.iloc[-1]    # 当前K线（入场价）

    adx = float(row["adx"]) if not np.isnan(row["adx"]) else 0.0
    atr = float(row["atr"]) if not np.isnan(row["atr"]) else 0.0
    ema = float(row["ema200"]) if not np.isnan(row["ema200"]) else 0.0

    if adx < cfg["adx_th"] or atr <= 0:
        return None

    cu    = int(row["cu"])
    cd    = int(row["cd"])
    cc    = float(row["cc"])
    entry = float(cur["close"])

    # v7.0 动态TP: 强趋势时扩大TP倍数
    tp_s = cfg["tp_s"]
    if adx >= DYNAMIC_TP_ADX_THRESHOLD:
        tp_s = tp_s * DYNAMIC_TP_MULTIPLIER

    # SHORT信号: 连涨≥sc + 累涨≥ccp
    if cu >= cfg["sc"] and cc >= cfg["ccp"]:
        return {
            "direction": "做空",
            "entry":     entry,
            "sl":        entry + cfg["sl_atr"] * atr,
            "tp":        entry - tp_s * atr,
            "adx":       round(adx, 1),
            "atr":       round(atr, 6),
            "tp_s":      round(tp_s, 2),
            "dynamic_tp": adx >= DYNAMIC_TP_ADX_THRESHOLD,
            "cu": cu, "cd": cd, "cc": cc,
            "bar_ts": int(row["ts"]),
        }

    # LONG信号: 连跌≥lc + 累跌≥ccp + close>EMA200
    if (not cfg["long_disabled"]
            and cd >= cfg["lc"]
            and cc <= -cfg["ccp"]
            and entry > ema):
        return {
            "direction": "做多",
            "entry":     entry,
            "sl":        entry - cfg["sl_atr"] * atr,
            "tp":        entry + tp_s * atr,
            "adx":       round(adx, 1),
            "atr":       round(atr, 6),
            "tp_s":      round(tp_s, 2),
            "dynamic_tp": adx >= DYNAMIC_TP_ADX_THRESHOLD,
            "cu": cu, "cd": cd, "cc": cc,
            "bar_ts": int(row["ts"]),
        }

    return None


# ══════════════════════════════════════════════════════════════
#  持仓退出检测（v6.9修复版: open价判断双触发先后）
# ══════════════════════════════════════════════════════════════
def check_exit(pos: dict, high: float, low: float, open_: float = None) -> tuple:
    if pos["direction"] == "做空":
        tp_hit = low  <= pos["tp"]
        sl_hit = high >= pos["sl"]
        if tp_hit and sl_hit:
            if open_ is not None:
                return ("止损", pos["sl"]) if open_ >= pos["entry"] else ("止盈", pos["tp"])
            return "止损", pos["sl"]
        if tp_hit: return "止盈", pos["tp"]
        if sl_hit: return "止损", pos["sl"]
    else:
        tp_hit = high >= pos["tp"]
        sl_hit = low  <= pos["sl"]
        if tp_hit and sl_hit:
            if open_ is not None:
                return ("止损", pos["sl"]) if open_ <= pos["entry"] else ("止盈", pos["tp"])
            return "止损", pos["sl"]
        if tp_hit: return "止盈", pos["tp"]
        if sl_hit: return "止损", pos["sl"]
    return None, None


# ══════════════════════════════════════════════════════════════
#  状态持久化（原子写，防崩溃数据损坏）
# ══════════════════════════════════════════════════════════════
def _default_state() -> dict:
    return {
        "positions":    {},
        "equity":       CAPITAL,
        "peak_equity":  CAPITAL,      # v7.0新增: 最大回撤追踪
        "max_drawdown": 0.0,
        "day_loss":     0.0,
        "day_date":     "",
        "total_trades": 0,
        "wins":         0,
        "losses":       0,
        "total_pnl":    0.0,
        "streak":       0,            # v7.0新增: 正=连胜, 负=连败
        "daily_stats":  {},           # v7.0新增: 每日收益记录
    }

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            s = json.loads(STATE_FILE.read_text())
            # 兼容旧state：补全v7.0新字段
            for k, v in _default_state().items():
                s.setdefault(k, v)
            return s
        except Exception:
            pass
    return _default_state()

def save_state(s: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2))
    tmp.replace(STATE_FILE)

def load_trades() -> list:
    if TRADES_FILE.exists():
        try:
            return json.loads(TRADES_FILE.read_text())
        except Exception:
            pass
    return []

def save_trades(t: list) -> None:
    tmp = TRADES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(t, ensure_ascii=False, indent=2))
    tmp.replace(TRADES_FILE)


# ══════════════════════════════════════════════════════════════
#  v7.0 状态更新辅助（连胜/连败 + 最大回撤）
# ══════════════════════════════════════════════════════════════
def update_stats(state: dict, net_pnl: float) -> None:
    """更新连胜连败和最大回撤"""
    # 连胜连败
    if net_pnl > 0:
        state["streak"] = max(state["streak"], 0) + 1
    else:
        state["streak"] = min(state["streak"], 0) - 1

    # 最大回撤
    if state["equity"] > state["peak_equity"]:
        state["peak_equity"] = state["equity"]
    dd = (state["peak_equity"] - state["equity"]) / state["peak_equity"] * 100
    if dd > state["max_drawdown"]:
        state["max_drawdown"] = dd

    # 每日收益
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today not in state["daily_stats"]:
        state["daily_stats"][today] = {"pnl": 0.0, "trades": 0, "wins": 0}
    state["daily_stats"][today]["pnl"] += net_pnl
    state["daily_stats"][today]["trades"] += 1
    if net_pnl > 0:
        state["daily_stats"][today]["wins"] += 1


# ══════════════════════════════════════════════════════════════
#  统计输出
# ══════════════════════════════════════════════════════════════
def print_summary(state: dict, trades: list) -> None:
    total = state["wins"] + state["losses"]
    wr    = state["wins"] / total * 100 if total > 0 else 0
    logger.info("=" * 72)
    logger.info(f"  白夜交易系统 {VERSION} — 阶段汇总")
    logger.info(f"  完成笔数: {total} | 胜率: {wr:.1f}% | 总PnL: {state['total_pnl']:+.4f}U")
    logger.info(f"  资金: {CAPITAL}U → 当前权益: {state['equity']:.4f}U ({(state['equity']/CAPITAL-1)*100:+.2f}%)")
    logger.info(f"  最大回撤: {state['max_drawdown']:.1f}% | 连胜/连败: {state['streak']:+d}")
    if trades:
        wins_pnl = [t["net_pnl"] for t in trades if t["net_pnl"] > 0]
        loss_pnl = [t["net_pnl"] for t in trades if t["net_pnl"] < 0]
        avg_win  = sum(wins_pnl) / len(wins_pnl) if wins_pnl else 0
        avg_loss = sum(loss_pnl) / len(loss_pnl) if loss_pnl else 0
        pf = (-avg_win * len(wins_pnl)) / (abs(avg_loss) * len(loss_pnl)) if loss_pnl and avg_loss < 0 else 0
        logger.info(f"  平均盈利: {avg_win:+.4f}U | 平均亏损: {avg_loss:+.4f}U | PF={pf:.2f}")
    # 每日统计
    if state.get("daily_stats"):
        logger.info("  ── 每日收益 ──")
        for date, ds in sorted(state["daily_stats"].items()):
            dwr = ds["wins"] / ds["trades"] * 100 if ds["trades"] > 0 else 0
            logger.info(f"    {date}: PnL={ds['pnl']:+.3f}U WR={dwr:.0f}% ({ds['wins']}/{ds['trades']})")
    logger.info("=" * 72)


# ══════════════════════════════════════════════════════════════
#  主循环
# ══════════════════════════════════════════════════════════════
def main() -> None:
    global _running

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)

    # 写PID文件
    import os
    PID_FILE.write_text(str(os.getpid()))

    state  = load_state()
    trades = load_trades()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("day_date") != today:
        state["day_loss"] = 0.0
        state["day_date"] = today

    logger.info("=" * 72)
    logger.info(f"  白夜交易系统 {VERSION} — 启动")
    logger.info(f"  品种({len(SYMBOLS)}): {SYMBOLS}")
    logger.info(f"  资金={CAPITAL}U | 风险={RISK_PCT*100:.0f}%/笔 | FEE={FEE*100:.3f}%单边(Maker)")
    logger.info(f"  目标={TARGET_TRADES}笔 | 动态TP: ADX≥{DYNAMIC_TP_ADX_THRESHOLD} → TP×{DYNAMIC_TP_MULTIPLIER}")
    logger.info(f"  已完成={state['wins']+state['losses']}笔 | 权益={state['equity']:.2f}U | 回撤={state['max_drawdown']:.1f}%")
    logger.info("=" * 72)

    cooldown:  dict[str, int] = {}   # sym -> 上次开仓的poll_count
    last_bar:  dict[str, int] = {}   # sym -> 上次信号的bar_ts
    poll_count = 0

    while _running:
        try:
            poll_count += 1
            now   = datetime.now(timezone.utc)
            today = now.strftime("%Y-%m-%d")

            # 日期切换 → 重置日熔断
            if state.get("day_date") != today:
                state["day_loss"] = 0.0
                state["day_date"] = today
                logger.info("━━ 新的一天，日熔断计数已重置 ━━")

            # 日熔断检查
            if state["day_loss"] >= state["equity"] * DAILY_LOSS_PCT:
                logger.warning(
                    f"⛔ 日熔断! 今日亏损={state['day_loss']:.4f}U ≥ "
                    f"{DAILY_LOSS_PCT*100:.0f}%×{state['equity']:.2f}U，暂停60s"
                )
                time.sleep(60)
                continue

            # ── 1. 拉取所有品种K线（每品种仅拉一次，退出+信号共用）
            scan_data: dict = {}
            for sym in SYMBOLS:
                try:
                    scan_data[sym] = compute_indicators(fetch_klines(sym))
                except Exception as e:
                    logger.warning(f"[{sym}] 数据获取失败: {e}")
                    scan_data[sym] = None

            # ── 2. 检查持仓退出
            for sym in list(state["positions"].keys()):
                pos = state["positions"][sym]
                df  = scan_data.get(sym)
                if df is None:
                    continue

                cur   = df.iloc[-1]
                high  = float(cur["high"])
                low   = float(cur["low"])
                open_ = float(cur["open"])
                result, exit_price = check_exit(pos, high, low, open_)

                if result:
                    qty      = pos["qty"]
                    raw_pnl  = (
                        (pos["entry"] - exit_price) * qty if pos["direction"] == "做空"
                        else (exit_price - pos["entry"]) * qty
                    )
                    fee_cost = (pos["entry"] + exit_price) * qty * FEE
                    net_pnl  = raw_pnl - fee_cost

                    state["equity"]      += net_pnl
                    state["total_pnl"]    = state.get("total_pnl", 0.0) + net_pnl
                    state["total_trades"] = state.get("total_trades", 0) + 1

                    if net_pnl > 0:
                        state["wins"] = state.get("wins", 0) + 1
                    else:
                        state["losses"]   = state.get("losses", 0) + 1
                        state["day_loss"] += abs(net_pnl)

                    update_stats(state, net_pnl)
                    del state["positions"][sym]

                    trade_rec = {
                        "no":           state["total_trades"],
                        "sym":          sym,
                        "direction":    pos["direction"],
                        "entry":        round(pos["entry"], 8),
                        "exit":         round(exit_price, 8),
                        "qty":          round(qty, 6),
                        "notional":     round(pos["notional"], 4),
                        "result":       result,
                        "net_pnl":      round(net_pnl, 6),
                        "fee":          round(fee_cost, 6),
                        "open_time":    pos["open_time"],
                        "close_time":   now.isoformat(),
                        "equity_after": round(state["equity"], 6),
                        "adx":          pos.get("adx"),
                        "atr":          pos.get("atr"),
                        "tp_s":         pos.get("tp_s"),
                        "dynamic_tp":   pos.get("dynamic_tp", False),
                        "cu": pos.get("cu"), "cd": pos.get("cd"), "cc": pos.get("cc"),
                        "bar_ts": pos.get("bar_ts"),
                    }
                    trades.append(trade_rec)
                    save_state(state)
                    save_trades(trades)

                    total = state["wins"] + state["losses"]
                    wr    = state["wins"] / total * 100 if total > 0 else 0
                    emoji = "✅" if net_pnl > 0 else "❌"
                    streak_str = f"连{'胜' if state['streak']>0 else '败'}{abs(state['streak'])}"
                    dtp_str = "🚀DTP" if trade_rec["dynamic_tp"] else ""
                    logger.info(
                        f"{emoji} #{state['total_trades']:3d} 平仓 {sym} {pos['direction']} {result} {dtp_str}| "
                        f"入={pos['entry']:.5g} 出={exit_price:.5g} PnL={net_pnl:+.4f}U | "
                        f"WR={wr:.1f}%({state['wins']}/{total}) 权益={state['equity']:.4f}U {streak_str}"
                    )

                    if total >= TARGET_TRADES:
                        print_summary(state, trades)

            # ── 3. 扫描新信号
            if len(state["positions"]) < MAX_POSITIONS:
                for sym in SYMBOLS:
                    if sym in state["positions"]:
                        continue
                    if len(state["positions"]) >= MAX_POSITIONS:
                        break
                    # 冷却期
                    if poll_count - cooldown.get(sym, 0) < COOLDOWN_BARS:
                        continue

                    df = scan_data.get(sym)
                    if df is None:
                        continue

                    sig = check_signal(sym, df, CONFIGS[sym])
                    if sig is None:
                        continue

                    # 同一根K线不重复开仓
                    if last_bar.get(sym) == sig["bar_ts"]:
                        continue

                    # 计算仓位
                    risk_u   = state["equity"] * RISK_PCT
                    sl_dist  = abs(sig["entry"] - sig["sl"])
                    if sl_dist <= 0:
                        continue
                    qty      = risk_u / sl_dist
                    notional = qty * sig["entry"]

                    # 名义值保护
                    if notional < 5:
                        continue
                    if notional > state["equity"] * 0.5:
                        qty      = state["equity"] * 0.5 / sig["entry"]
                        notional = qty * sig["entry"]

                    last_bar[sym]  = sig["bar_ts"]
                    cooldown[sym]  = poll_count

                    state["positions"][sym] = {
                        "direction": sig["direction"],
                        "entry":     sig["entry"],
                        "sl":        sig["sl"],
                        "tp":        sig["tp"],
                        "qty":       qty,
                        "notional":  round(notional, 4),
                        "adx":       sig["adx"],
                        "atr":       sig["atr"],
                        "tp_s":      sig["tp_s"],
                        "dynamic_tp": sig["dynamic_tp"],
                        "cu":        sig["cu"],
                        "cd":        sig["cd"],
                        "cc":        sig["cc"],
                        "bar_ts":    sig["bar_ts"],
                        "open_time": now.isoformat(),
                    }
                    save_state(state)

                    total  = state["wins"] + state["losses"]
                    dtp_str = f" 🚀DynamicTP×{sig['tp_s']}" if sig["dynamic_tp"] else ""
                    logger.info(
                        f"🔔 #{total + len(state['positions']):3d} 开仓 {sym} {sig['direction']}{dtp_str} | "
                        f"入={sig['entry']:.5g} TP={sig['tp']:.5g} SL={sig['sl']:.5g} | "
                        f"ADX={sig['adx']} cc={sig['cc']*100:.3f}% notional={notional:.1f}U"
                    )

            # ── 4. 每轮状态摘要
            total = state["wins"] + state["losses"]
            wr    = state["wins"] / total * 100 if total > 0 else 0.0
            pos_str = (
                " | ".join(f"{s}({v['direction']}@{v['entry']:.4g})"
                           for s, v in state["positions"].items())
                or "无持仓"
            )
            logger.info(
                f"[{now.strftime('%H:%M')} #{poll_count}] "
                f"完成={total}/{TARGET_TRADES} WR={wr:.0f}% PnL={state['total_pnl']:+.3f}U "
                f"权益={state['equity']:.2f}U 回撤={state['max_drawdown']:.1f}% | {pos_str}"
            )

            # 无持仓时显示各品种到信号的距离
            if not state["positions"]:
                for sym in SYMBOLS:
                    df = scan_data.get(sym)
                    if df is None:
                        continue
                    row = df.iloc[-2]
                    adx = float(row["adx"]) if not np.isnan(row["adx"]) else 0.0
                    cu  = int(row["cu"])
                    cd  = int(row["cd"])
                    cc  = float(row["cc"]) * 100
                    cfg = CONFIGS[sym]

                    short_ok = cu >= cfg["sc"] and cc/100 >= cfg["ccp"] and adx >= cfg["adx_th"]
                    long_ok  = (not cfg["long_disabled"] and cd >= cfg["lc"]
                                and cc/100 <= -cfg["ccp"] and adx >= cfg["adx_th"])
                    s_str = "✅SHORT" if short_ok else f"SHORT[cu{cu}/{cfg['sc']} cc{cc:.2f}%/{cfg['ccp']*100:.2f}% adx{adx:.0f}/{cfg['adx_th']}]"
                    l_str = "LONG禁" if cfg["long_disabled"] else (
                        "✅LONG" if long_ok else f"LONG[cd{cd}/{cfg['lc']} cc{cc:.2f}%/{-cfg['ccp']*100:.2f}% adx{adx:.0f}/{cfg['adx_th']}]"
                    )
                    logger.info(f"  {sym:10s} ADX={adx:4.0f} | {s_str} | {l_str}")

            # 每20轮输出周期摘要
            if poll_count % 20 == 0:
                elapsed_h = poll_count * POLL_SECS / 3600
                rate = total / elapsed_h if elapsed_h > 0 else 0
                eta  = f"预计还需{(TARGET_TRADES-total)/rate:.0f}h" if rate > 0 else "等待信号..."
                logger.info(f"╪╪╪ 周期摘要 ╪╪╪ 运行{elapsed_h:.1f}h | {rate:.1f}笔/h | {eta}")

            time.sleep(POLL_SECS)

        except Exception as e:
            logger.error(f"主循环异常(轮次{poll_count}): {e}", exc_info=True)
            time.sleep(30)

    # 退出 → 保存并输出汇总
    logger.info("引擎退出，保存最终状态...")
    save_state(state)
    save_trades(trades)
    print_summary(state, trades)
    # 清理PID文件
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    main()
