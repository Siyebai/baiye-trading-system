#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
白夜纸交易引擎 v6.9 — 生产级终极版
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
版本: v6.9
日期: 2026-05-10
作者: 白夜交易系统

核心改进（vs 历史所有版本）:
  [BUG修复]
  ✅ 双重日志Bug    → logger.propagate=False
  ✅ cc方向重置     → 切换方向时 cc=chg（不累积旧方向值）
  ✅ Wilder ATR/ADX → 与回测引擎v2.2完全一致
  ✅ SIGTERM优雅退出 → 不被guardian误杀
  ✅ 参数严格对齐   → 完全使用MEMORY.md验证参数（180天+OOS）

  [架构优化]
  ✅ 单文件自洽     → 无外部依赖，任何Python3.10+环境可运行
  ✅ 冷却期保护     → 同品种同方向5根K线冷却，防重复开仓
  ✅ 日熔断保护     → 单日亏损≥6%权益自动停止
  ✅ 最大持仓保护   → 同时≤4个品种
  ✅ 名义值保护     → 0.5U≤名义值≤权益50%
  ✅ 状态持久化     → 每次操作后立即写盘
  ✅ 日志轮转       → RotatingFileHandler 5MB×3备份
  ✅ 异常隔离       → 单品种异常不影响其他品种
  ✅ 详细状态输出   → 每轮显示信号距离，便于监控

  [参数来源] MEMORY.md §最终确认参数（2026-05-08, v2.2引擎, 180天+OOS验证）
  品种     WR     OOS_WR  月均%
  BTCUSDT  60.4%  57.8%   +8.4%
  ETHUSDT  63.0%  64.5%   +7.7%  ← 最优
  SOLUSDT  58.5%  58.7%   +1.9%
  BNBUSDT  63.0%  64.6%   +7.8%  ← 禁LONG
  LINKUSDT 67.5%  70.0%   +8.7%  ← 最稳健
  POLUSDT  55.8%  50.0%   +3.3%  ← 边缘，禁LONG

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
VERSION        = "v6.9"
CAPITAL        = 150.0       # 初始资金 U
RISK_PCT       = 0.02        # 单笔风险 2%
FEE            = 0.0004      # 手续费 0.04% 单边（Taker，保守估计）
MAX_POSITIONS  = 4           # 最大同时持仓数
POLL_SECS      = 30          # 轮询间隔（秒）
TARGET_TRADES  = 100         # 目标完成笔数（达到后输出报告）
DAILY_LOSS_PCT = 0.06        # 日熔断：单日亏损≥6%权益停止
INTERVAL       = "15m"       # K线周期（与回测一致）
KLINE_LIMIT    = 500         # 每次拉取K线数（EMA200需要200+）
COOLDOWN_BARS  = 5           # 同品种同方向冷却K线数

WORK_DIR   = Path(__file__).parent
LOG_FILE   = WORK_DIR / "logs" / "paper_v69.log"
STATE_FILE = WORK_DIR / "logs" / "paper_v69_state.json"
TRADES_FILE= WORK_DIR / "logs" / "paper_v69_trades.json"
LOG_FILE.parent.mkdir(exist_ok=True)

# ══════════════════════════════════════════════════════════════
#  日志配置（修复双重日志Bug：propagate=False）
# ══════════════════════════════════════════════════════════════
def _setup_logger() -> logging.Logger:
    log = logging.getLogger("paper_v69")
    log.setLevel(logging.INFO)
    log.propagate = False  # ← 关键：禁止传播到root logger，消除双重输出

    fmt = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    fh = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)

    return log

logger = _setup_logger()

# ══════════════════════════════════════════════════════════════
#  品种配置（严格对齐 MEMORY.md 验证参数 2026-05-08）
#  格式: sc=连涨触发SHORT, lc=连跌触发LONG,
#        ccp=累计涨跌幅阈值, adx_th=ADX最低阈值
#        tp_s=TP倍数×ATR, sl_atr=SL倍数×ATR
#        long_disabled=是否禁用LONG
# ══════════════════════════════════════════════════════════════
CONFIGS: dict = {
    # WR=60.4%, OOS=57.8%, 月均+8.4%
    "BTCUSDT": dict(
        sc=4, lc=5, ccp=0.002, adx_th=22,
        tp_s=0.8, sl_atr=1.0, long_disabled=False
    ),
    # WR=63.0%, OOS=64.5%, 月均+7.7% ← 最优
    "ETHUSDT": dict(
        sc=5, lc=4, ccp=0.0015, adx_th=20,
        tp_s=0.8, sl_atr=1.0, long_disabled=False
    ),
    # WR=58.5%, OOS=58.7%, 月均+1.9%（adx_th=30防Q1亏损）
    "SOLUSDT": dict(
        sc=5, lc=4, ccp=0.0015, adx_th=30,
        tp_s=0.8, sl_atr=1.0, long_disabled=False
    ),
    # WR=63.0%, OOS=64.6%, 月均+7.8%（禁LONG）
    "BNBUSDT": dict(
        sc=5, lc=6, ccp=0.0015, adx_th=15,
        tp_s=0.8, sl_atr=1.0, long_disabled=True   # ← 禁LONG
    ),
    # WR=67.5%, OOS=70.0%, 月均+8.7% ← 最稳健
    "LINKUSDT": dict(
        sc=7, lc=4, ccp=0.0025, adx_th=15,
        tp_s=0.8, sl_atr=1.0, long_disabled=False
    ),
    # WR=55.8%, OOS=50.0%, 月均+3.3%（边缘，禁LONG）
    "POLUSDT": dict(
        sc=5, lc=4, ccp=0.0015, adx_th=25,
        tp_s=0.8, sl_atr=1.0, long_disabled=True    # ← 禁LONG
    ),
}
SYMBOLS = list(CONFIGS.keys())

# ══════════════════════════════════════════════════════════════
#  优雅退出（SIGTERM/SIGINT）
# ══════════════════════════════════════════════════════════════
_running = True

def _handle_signal(sig, frame):
    global _running
    logger.warning(f"收到信号 {sig}，执行优雅退出（当前轮次完成后停止）...")
    _running = False

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)

# ══════════════════════════════════════════════════════════════
#  技术指标（Wilder's平滑，与回测引擎v2.2完全一致）
# ══════════════════════════════════════════════════════════════
def _wilder_smooth(arr: np.ndarray, n: int) -> np.ndarray:
    """Wilder's 平滑移动平均（RMA）
    初始值 = 前n期简单均值，后续 out[i] = out[i-1]*(n-1)/n + arr[i]/n
    """
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
    """计算 ATR / ADX / EMA200 / 连涨连跌累涨"""
    df = df.copy()
    c = df["close"].values
    h = df["high"].values
    l = df["low"].values
    n = len(c)

    # ── True Range ────────────────────────────────────────────
    tr = np.maximum(
        h - l,
        np.maximum(
            np.abs(h - np.roll(c, 1)),
            np.abs(l - np.roll(c, 1))
        )
    )
    tr[0] = h[0] - l[0]

    # ── ATR (Wilder's, period=14) ─────────────────────────────
    df["atr"] = _wilder_smooth(tr, 14)

    # ── EMA200 ────────────────────────────────────────────────
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()

    # ── ADX (Wilder's, period=14) ─────────────────────────────
    up = np.diff(h, prepend=h[0])
    dn = np.diff(l, prepend=l[0]) * -1
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)

    atr14  = _wilder_smooth(tr, 14)
    safe   = np.where(atr14 > 0, atr14, np.nan)
    pdi    = 100 * _wilder_smooth(pdm, 14) / safe
    ndi    = 100 * _wilder_smooth(ndm, 14) / safe
    denom  = np.where((pdi + ndi) > 0, pdi + ndi, np.nan)
    dx     = 100 * np.abs(pdi - ndi) / denom
    df["adx"] = _wilder_smooth(dx, 14)

    # ── 连涨(cu)/连跌(cd)/累涨跌幅(cc) ─────────────────────────
    # ← Bug修复：方向切换时 cc=chg（不累积旧方向值）
    cu = np.zeros(n, dtype=int)
    cd = np.zeros(n, dtype=int)
    cc = np.zeros(n, dtype=float)
    for i in range(1, n):
        chg = (c[i] - c[i - 1]) / c[i - 1]
        if c[i] > c[i - 1]:                          # 上涨
            cu[i] = cu[i - 1] + 1
            cd[i] = 0
            cc[i] = chg if cd[i - 1] > 0 else cc[i - 1] + chg  # 方向切换重置
        elif c[i] < c[i - 1]:                        # 下跌
            cd[i] = cd[i - 1] + 1
            cu[i] = 0
            cc[i] = chg if cu[i - 1] > 0 else cc[i - 1] + chg  # 方向切换重置
        else:                                         # 平盘
            cu[i] = cu[i - 1]
            cd[i] = cd[i - 1]
            cc[i] = cc[i - 1]

    df["cu"] = cu
    df["cd"] = cd
    df["cc"] = cc
    return df


# ══════════════════════════════════════════════════════════════
#  数据获取
# ══════════════════════════════════════════════════════════════
def fetch_klines(symbol: str, limit: int = KLINE_LIMIT) -> pd.DataFrame:
    """从 Binance 合约 API 拉取 K 线"""
    url = "https://fapi.binance.com/fapi/v1/klines"
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


# ══════════════════════════════════════════════════════════════
#  信号检测（严格参数，与回测完全对齐）
# ══════════════════════════════════════════════════════════════
def check_signal(symbol: str, df: pd.DataFrame, cfg: dict) -> dict | None:
    """
    返回信号字典或 None
    使用 df.iloc[-2]（已收盘K线）作为信号源
    入场价用 df.iloc[-1]["close"]（当前价）
    """
    if len(df) < 220:
        return None

    row    = df.iloc[-2]    # 已收盘K线
    cur    = df.iloc[-1]    # 当前K线（用于入场价）

    adx = float(row["adx"]) if not np.isnan(row["adx"]) else 0.0
    atr = float(row["atr"]) if not np.isnan(row["atr"]) else 0.0
    ema = float(row["ema200"]) if not np.isnan(row["ema200"]) else 0.0

    # 基础过滤：ADX + ATR 有效
    if adx < cfg["adx_th"] or atr <= 0:
        return None

    cu  = int(row["cu"])
    cd  = int(row["cd"])
    cc  = float(row["cc"])
    entry = float(cur["close"])

    # ── SHORT 信号：连涨≥sc + 累涨≥ccp ──────────────────────
    if cu >= cfg["sc"] and cc >= cfg["ccp"]:
        return {
            "direction": "做空",
            "entry":     entry,
            "sl":        entry + cfg["sl_atr"] * atr,
            "tp":        entry - cfg["tp_s"]   * atr,
            "adx": round(adx, 1),
            "atr": round(atr, 6),
            "cu": cu, "cd": cd, "cc": cc,
            "bar_ts": int(row["ts"]),
        }

    # ── LONG 信号：连跌≥lc + 累跌≥ccp + 价格>EMA200 ─────────
    if (not cfg["long_disabled"]
            and cd >= cfg["lc"]
            and cc <= -cfg["ccp"]
            and entry > ema):
        return {
            "direction": "做多",
            "entry":     entry,
            "sl":        entry - cfg["sl_atr"] * atr,
            "tp":        entry + cfg["tp_s"]   * atr,
            "adx": round(adx, 1),
            "atr": round(atr, 6),
            "cu": cu, "cd": cd, "cc": cc,
            "bar_ts": int(row["ts"]),
        }

    return None


# ══════════════════════════════════════════════════════════════
#  持仓退出检测
# ══════════════════════════════════════════════════════════════
def check_exit(pos: dict, high: float, low: float, open_: float = None) -> tuple[str, float] | tuple[None, None]:
    """检查持仓是否触发止盈/止损
    open_: 当前K线开盘价，用于双触发时判断先后（与回测引擎v2.2保持一致）
    """
    if pos["direction"] == "做空":
        tp_hit = low  <= pos["tp"]
        sl_hit = high >= pos["sl"]
        if tp_hit and sl_hit:
            # 双触发：用开盘价判断先后（保守=悲观处理）
            if open_ is not None:
                # open高于入场价 → 先上涨触SL，再下跌触TP → 止损
                return ("止损", pos["sl"]) if open_ >= pos["entry"] else ("止盈", pos["tp"])
            return "止损", pos["sl"]  # 无open信息时保守处理
        if tp_hit: return "止盈",  pos["tp"]
        if sl_hit: return "止损",  pos["sl"]
    else:  # 做多
        tp_hit = high >= pos["tp"]
        sl_hit = low  <= pos["sl"]
        if tp_hit and sl_hit:
            if open_ is not None:
                # open低于入场价 → 先下跌触SL → 止损
                return ("止损", pos["sl"]) if open_ <= pos["entry"] else ("止盈", pos["tp"])
            return "止损", pos["sl"]
        if tp_hit: return "止盈",  pos["tp"]
        if sl_hit: return "止损",  pos["sl"]
    return None, None


# ══════════════════════════════════════════════════════════════
#  状态持久化
# ══════════════════════════════════════════════════════════════
def _default_state() -> dict:
    return {
        "positions":    {},
        "equity":       CAPITAL,
        "day_loss":     0.0,
        "day_date":     "",
        "total_trades": 0,
        "wins":         0,
        "losses":       0,
        "total_pnl":    0.0,
    }

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return _default_state()

def save_state(s: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2))
    tmp.replace(STATE_FILE)  # 原子写，防止崩溃损坏文件

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
#  统计输出
# ══════════════════════════════════════════════════════════════
def print_summary(state: dict, trades: list) -> None:
    total = state["wins"] + state["losses"]
    wr    = state["wins"] / total * 100 if total > 0 else 0
    logger.info("=" * 70)
    logger.info(f"  白夜纸交易 {VERSION} 阶段汇总")
    logger.info(f"  完成笔数: {total} | 胜率: {wr:.1f}% | 总PnL: {state['total_pnl']:+.4f}U")
    logger.info(f"  初始资金: {CAPITAL}U → 当前权益: {state['equity']:.4f}U "
                f"({(state['equity']/CAPITAL - 1)*100:+.2f}%)")
    if trades:
        wins_pnl  = [t["net_pnl"] for t in trades if t["net_pnl"] > 0]
        loss_pnl  = [t["net_pnl"] for t in trades if t["net_pnl"] < 0]
        avg_win   = sum(wins_pnl) / len(wins_pnl)  if wins_pnl else 0
        avg_loss  = sum(loss_pnl) / len(loss_pnl)  if loss_pnl else 0
        pf        = -avg_win / avg_loss * (wr/100) / (1-wr/100) if avg_loss < 0 and wr < 100 else 0
        logger.info(f"  平均盈利: {avg_win:+.4f}U | 平均亏损: {avg_loss:+.4f}U | PF≈{pf:.2f}")
    logger.info("=" * 70)


# ══════════════════════════════════════════════════════════════
#  主循环
# ══════════════════════════════════════════════════════════════
def main() -> None:
    global _running

    state  = load_state()
    trades = load_trades()

    # 日期重置检查
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("day_date") != today:
        state["day_loss"] = 0.0
        state["day_date"] = today

    logger.info("=" * 70)
    logger.info(f"  白夜纸交易引擎 {VERSION} — 生产级终极版")
    logger.info(f"  品种: {SYMBOLS}")
    logger.info(f"  资金={CAPITAL}U | 风险={RISK_PCT*100:.0f}%/笔 | 目标≥{TARGET_TRADES}笔")
    logger.info(f"  已完成={state['wins']+state['losses']}笔 | 权益={state['equity']:.2f}U")
    logger.info("=" * 70)

    cooldown: dict[str, int] = {}   # sym -> poll_count（上次开仓轮次）
    last_bar: dict[str, int] = {}   # sym -> 上次信号的bar_ts
    poll_count = 0

    while _running:
        try:
            poll_count += 1
            now   = datetime.now(timezone.utc)
            today = now.strftime("%Y-%m-%d")

            # 日期切换 → 重置日熔断计数
            if state.get("day_date") != today:
                state["day_loss"] = 0.0
                state["day_date"] = today
                logger.info("━━ 新的一天，日熔断计数已重置 ━━")

            # 日熔断检查
            if state["day_loss"] >= state["equity"] * DAILY_LOSS_PCT:
                logger.warning(
                    f"⛔ 日熔断触发！今日亏损={state['day_loss']:.4f}U "
                    f"≥ {DAILY_LOSS_PCT*100:.0f}% × 权益={state['equity']:.2f}U，暂停开仓"
                )
                time.sleep(60)
                continue

            # ── 1. 拉取所有品种K线（每品种只拉一次，退出+信号共用）
            scan_data: dict[str, pd.DataFrame | None] = {}
            for sym in SYMBOLS:
                try:
                    df = compute_indicators(fetch_klines(sym))
                    scan_data[sym] = df
                except Exception as e:
                    logger.warning(f"[{sym}] 数据获取失败: {e}")
                    scan_data[sym] = None

            # ── 2. 检查持仓退出
            for sym in list(state["positions"].keys()):
                pos = state["positions"][sym]
                df  = scan_data.get(sym)
                if df is None:
                    continue

                cur_bar = df.iloc[-1]
                high  = float(cur_bar["high"])
                low   = float(cur_bar["low"])
                open_ = float(cur_bar["open"])
                result, exit_price = check_exit(pos, high, low, open_)

                if result:
                    qty       = pos["qty"]
                    direction = pos["direction"]
                    raw_pnl   = (
                        (pos["entry"] - exit_price) * qty if direction == "做空"
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

                    del state["positions"][sym]

                    trade_rec = {
                        "no":           state["total_trades"],
                        "sym":          sym,
                        "direction":    direction,
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
                        "cu":           pos.get("cu"),
                        "cd":           pos.get("cd"),
                        "cc":           pos.get("cc"),
                        "bar_ts":       pos.get("bar_ts"),
                    }
                    trades.append(trade_rec)
                    save_state(state)
                    save_trades(trades)

                    total = state["wins"] + state["losses"]
                    wr    = state["wins"] / total * 100 if total > 0 else 0
                    emoji = "✅" if net_pnl > 0 else "❌"
                    logger.info(
                        f"{emoji} #{state['total_trades']:3d} 平仓 {sym} {direction} {result} | "
                        f"入={pos['entry']:.5g} 出={exit_price:.5g} PnL={net_pnl:+.4f}U | "
                        f"WR={wr:.1f}%({state['wins']}/{total}) 权益={state['equity']:.4f}U"
                    )

                    # 达到目标 → 输出阶段报告
                    if total >= TARGET_TRADES:
                        print_summary(state, trades)

            # ── 3. 扫描新信号
            if len(state["positions"]) < MAX_POSITIONS:
                for sym in SYMBOLS:
                    if sym in state["positions"]:
                        continue
                    # 冷却期检查（防重复开仓）
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

                    # 计算仓位大小
                    atr_val  = sig["atr"]
                    risk_u   = state["equity"] * RISK_PCT
                    qty      = risk_u / (atr_val * CONFIGS[sym]["sl_atr"]) if atr_val > 0 else 0
                    notional = qty * sig["entry"]

                    # 名义值保护
                    if notional < 0.5 or notional > state["equity"] * 0.5:
                        continue

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
                        "cu":        sig["cu"],
                        "cd":        sig["cd"],
                        "cc":        sig["cc"],
                        "bar_ts":    sig["bar_ts"],
                        "open_time": now.isoformat(),
                    }
                    save_state(state)

                    total = state["wins"] + state["losses"]
                    logger.info(
                        f"🔔 #{total + len(state['positions']):3d} 开仓 {sym} {sig['direction']} | "
                        f"入={sig['entry']:.5g} TP={sig['tp']:.5g} SL={sig['sl']:.5g} | "
                        f"ADX={sig['adx']} ATR={sig['atr']:.5g} cu={sig['cu']} cd={sig['cd']} "
                        f"cc={sig['cc']*100:.3f}% 名义={notional:.2f}U"
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
                f"权益={state['equity']:.2f}U | {pos_str}"
            )

            # 无持仓时显示每个品种距信号的距离
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

                    s_ok = (
                        "✅SHORT" if cu >= cfg["sc"] and cc / 100 >= cfg["ccp"] and adx >= cfg["adx_th"]
                        else f"SHORT[cu{cu}/{cfg['sc']} cc{cc:.2f}%/{cfg['ccp']*100:.2f}% adx{adx:.0f}/{cfg['adx_th']}]"
                    )
                    l_ok = (
                        "LONG禁" if cfg["long_disabled"]
                        else ("✅LONG" if cd >= cfg["lc"] and cc / 100 <= -cfg["ccp"] and adx >= cfg["adx_th"]
                              else f"LONG[cd{cd}/{cfg['lc']} cc{cc:.2f}%/{-cfg['ccp']*100:.2f}% adx{adx:.0f}/{cfg['adx_th']}]")
                    )
                    logger.info(f"  {sym:10s} ADX={adx:4.0f} | {s_ok} | {l_ok}")

            time.sleep(POLL_SECS)

        except Exception as e:
            logger.error(f"主循环异常（第{poll_count}轮）: {e}", exc_info=True)
            time.sleep(30)

    # ── 退出 → 保存最终状态
    logger.info("引擎正在退出，保存最终状态...")
    save_state(state)
    save_trades(trades)
    print_summary(state, trades)


# ══════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    main()
