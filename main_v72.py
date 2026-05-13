#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
白夜交易系统 v7.2 — 核心引擎
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
整合融合历史:
  v7.1  Wilder ATR/ADX | cc方向切换修复 | 双触发open价优先 | 动态TP
        原子写入 | 日熔断 | 冷却期 | 名义值保护 | 单品种异常隔离
  v7.2c Kelly仓位 | WinRate Guard | 追踪止损 | 相关性控制
        多时间周期(3m/5m/15m/60m) | 信号评分系统
  v9.3  EventBus解耦 | 状态CRC校验 | PerfMon滚动统计
        PositionManager | signal_score加权 | shadow模式
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
目标: 短线高频 | 3~60m多周期布局 | 胜率≥55% | 月均+8%以上
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import sys
import time
import warnings
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")

# ──────────────────────────────────────────────────────────────
#  导入配置层
# ──────────────────────────────────────────────────────────────
try:
    import config as cfg
except ImportError:
    raise RuntimeError("未找到 config.py，请确保与 main_v72.py 同目录")

cfg.validate()

VERSION = f"v{cfg.VERSION}"

# ──────────────────────────────────────────────────────────────
#  目录初始化
# ──────────────────────────────────────────────────────────────
for _p in (cfg.LOG_FILE, cfg.STATE_FILE, cfg.TRADE_LOG, cfg.PID_FILE):
    Path(_p).parent.mkdir(parents=True, exist_ok=True)

# ──────────────────────────────────────────────────────────────
#  日志（propagate=False 防双重日志）
# ──────────────────────────────────────────────────────────────
def _setup_logger() -> logging.Logger:
    lg = logging.getLogger("baiye_v72")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    if not lg.handlers:
        fmt = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        fh = RotatingFileHandler(cfg.LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        lg.addHandler(fh)
        lg.addHandler(sh)
    return lg

logger = _setup_logger()

# ──────────────────────────────────────────────────────────────
#  优雅退出
# ──────────────────────────────────────────────────────────────
_running = True

def _on_signal(sig, _frame):
    global _running
    logger.warning(f"收到信号 {sig}，当前轮次完成后优雅退出...")
    _running = False

signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT,  _on_signal)


# ══════════════════════════════════════════════════════════════
#  § 1  技术指标（Wilder's平滑 — 与回测引擎v2.2完全一致）
# ══════════════════════════════════════════════════════════════
def _wilder(arr: np.ndarray, n: int) -> np.ndarray:
    """Wilder's RMA: 初值=前n期简单均值，后: out[i]=out[i-1]*(n-1)/n + arr[i]/n"""
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
    """计算 ATR/ADX/EMA200/连涨连跌/累涨跌幅（含方向切换重置修复）"""
    df = df.copy()
    c, h, l = df["close"].values, df["high"].values, df["low"].values
    n = len(c)

    # True Range
    tr = np.maximum(h - l, np.maximum(
        np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1))
    ))
    tr[0] = h[0] - l[0]

    # ATR (Wilder, 14)
    df["atr"] = _wilder(tr, 14)

    # EMA200
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()

    # ADX (Wilder, 14)
    up  = np.diff(h, prepend=h[0])
    dn  = np.diff(l, prepend=l[0]) * -1
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr14 = _wilder(tr, 14)
    safe  = np.where(atr14 > 0, atr14, np.nan)
    pdi   = 100 * _wilder(pdm, 14) / safe
    ndi   = 100 * _wilder(ndm, 14) / safe
    denom = np.where((pdi + ndi) > 0, pdi + ndi, np.nan)
    dx    = 100 * np.abs(pdi - ndi) / denom
    df["adx"] = _wilder(dx, 14)
    df["pdi"] = pdi
    df["ndi"] = ndi

    # RSI(14) — 多周期共振过滤用
    delta = np.diff(c, prepend=c[0])
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)
    avg_g = _wilder(gain, 14)
    avg_l = _wilder(loss, 14)
    rs    = np.where(avg_l > 0, avg_g / avg_l, 100.0)
    df["rsi"] = 100 - 100 / (1 + rs)

    # 连涨(cu)/连跌(cd)/累涨跌幅(cc) — 方向切换重置修复（v6.9）
    cu = np.zeros(n, dtype=int)
    cd = np.zeros(n, dtype=int)
    cc = np.zeros(n, dtype=float)
    for i in range(1, n):
        chg = (c[i] - c[i - 1]) / c[i - 1]
        if c[i] > c[i - 1]:
            cu[i] = cu[i - 1] + 1
            cd[i] = 0
            cc[i] = chg if cd[i - 1] > 0 else cc[i - 1] + chg
        elif c[i] < c[i - 1]:
            cd[i] = cd[i - 1] + 1
            cu[i] = 0
            cc[i] = chg if cu[i - 1] > 0 else cc[i - 1] + chg
        else:
            cu[i] = cu[i - 1]; cd[i] = cd[i - 1]; cc[i] = cc[i - 1]
    df["cu"] = cu; df["cd"] = cd; df["cc"] = cc
    return df


# ══════════════════════════════════════════════════════════════
#  § 2  数据拉取（多周期 + 重试退避）
# ══════════════════════════════════════════════════════════════
def fetch_klines(symbol: str, interval: str,
                 limit: int = cfg.KLINE_LIMIT, retries: int = 3) -> pd.DataFrame:
    url = f"{cfg.BINANCE_BASE_URL}/fapi/v1/klines"
    for attempt in range(retries):
        try:
            r = requests.get(url, params={"symbol": symbol, "interval": interval,
                                          "limit": limit}, timeout=10)
            r.raise_for_status()
            cols = ["ts","open","high","low","close","vol","ct","qv","tr","tbb","tbq","ign"]
            df = pd.DataFrame(r.json(), columns=cols)
            for col in ["open","high","low","close"]:
                df[col] = df[col].astype(float)
            return df
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
            else:
                raise RuntimeError(f"[{symbol}/{interval}] K线拉取失败: {e}")


def fetch_multi_tf(symbol: str) -> Dict[str, Optional[pd.DataFrame]]:
    """拉取该品种所有周期K线并计算指标，单品种异常不影响其他品种"""
    result: Dict[str, Optional[pd.DataFrame]] = {}
    for tf in cfg.TIMEFRAMES:
        try:
            result[tf] = compute_indicators(fetch_klines(symbol, tf))
        except Exception as e:
            logger.warning(f"[{symbol}/{tf}] 数据异常: {e}")
            result[tf] = None
    return result


# ══════════════════════════════════════════════════════════════
#  § 3  信号检测 + 多周期评分
# ══════════════════════════════════════════════════════════════
def _raw_signal(symbol: str, df: pd.DataFrame, sym_cfg: dict) -> Optional[dict]:
    """
    单周期原始信号检测（基于已收盘K线 df.iloc[-2]）
    返回 dict 或 None
    """
    if df is None or len(df) < 220:
        return None
    row = df.iloc[-2]   # 已收盘K线
    cur = df.iloc[-1]   # 当前K线（入场价用close模拟Limit）

    adx = float(row["adx"]) if not np.isnan(row["adx"]) else 0.0
    atr = float(row["atr"]) if not np.isnan(row["atr"]) else 0.0
    ema = float(row["ema200"]) if not np.isnan(row["ema200"]) else 0.0
    rsi = float(row["rsi"]) if not np.isnan(row["rsi"]) else 50.0

    if adx < sym_cfg["adx_th"] or atr <= 0:
        return None

    cu    = int(row["cu"])
    cd    = int(row["cd"])
    cc    = float(row["cc"])
    entry = float(cur["close"])

    # 动态TP
    tp_s = sym_cfg["tp_s"]
    if adx >= cfg.DYNAMIC_TP_ADX_TH:
        tp_s = tp_s * cfg.DYNAMIC_TP_MULT

    # SHORT: 连涨≥sc + 累涨≥ccp + RSI超买辅助(不强制过滤，仅评分)
    if cu >= sym_cfg["sc"] and cc >= sym_cfg["ccp"]:
        return {
            "side":       "short",
            "entry":      entry,
            "sl":         entry + sym_cfg["sl_atr"] * atr,
            "tp":         entry - tp_s * atr,
            "adx":        round(adx, 1),
            "atr":        round(atr, 8),
            "rsi":        round(rsi, 1),
            "tp_s":       round(tp_s, 2),
            "dynamic_tp": adx >= cfg.DYNAMIC_TP_ADX_TH,
            "cu": cu, "cd": cd, "cc": cc,
            "bar_ts": int(row["ts"]),
            "ema200": round(ema, 4),
        }

    # LONG: 连跌≥lc + 累跌≥ccp + close>EMA200
    if (not sym_cfg["long_disabled"]
            and cd >= sym_cfg["lc"]
            and cc <= -sym_cfg["ccp"]
            and entry > ema):
        return {
            "side":       "long",
            "entry":      entry,
            "sl":         entry - sym_cfg["sl_atr"] * atr,
            "tp":         entry + tp_s * atr,
            "adx":        round(adx, 1),
            "atr":        round(atr, 8),
            "rsi":        round(rsi, 1),
            "tp_s":       round(tp_s, 2),
            "dynamic_tp": adx >= cfg.DYNAMIC_TP_ADX_TH,
            "cu": cu, "cd": cd, "cc": cc,
            "bar_ts": int(row["ts"]),
            "ema200": round(ema, 4),
        }
    return None


def compute_signal_score(symbol: str,
                          tf_data: Dict[str, Optional[pd.DataFrame]],
                          sym_cfg: dict,
                          primary_sig: dict) -> float:
    """
    多周期共振评分 (0~10):
      ADX强度     0~3分
      多周期共振  0~3分（60m趋势对齐+5m辅助信号）
      RR比        0~2分
      RSI极值     0~1分
      动态TP      0~1分
    """
    score = 0.0
    side  = primary_sig["side"]

    # ── ADX强度 (0~3)
    adx = primary_sig["adx"]
    if adx >= 40:   score += 3.0
    elif adx >= 30: score += 2.0
    elif adx >= 20: score += 1.0

    # ── 60m趋势对齐 (0~1.5)
    df60 = tf_data.get("1h")
    if df60 is not None and len(df60) >= 50:
        row60 = df60.iloc[-2]
        ema60  = float(row60["ema200"]) if not np.isnan(row60["ema200"]) else 0
        close60 = float(row60["close"])
        adx60   = float(row60["adx"]) if not np.isnan(row60["adx"]) else 0
        if side == "short" and close60 < ema60 and adx60 > 20:
            score += 1.5   # 60m空头趋势对齐
        elif side == "long" and close60 > ema60 and adx60 > 20:
            score += 1.5   # 60m多头趋势对齐
        elif adx60 > 15:
            score += 0.5   # 60m有趋势但方向未对齐，部分得分

    # ── 5m辅助信号共振 (0~1.5)
    df5 = tf_data.get("5m")
    if df5 is not None and len(df5) >= 50:
        sig5 = _raw_signal(symbol, df5, sym_cfg)
        if sig5 and sig5["side"] == side:
            score += 1.5   # 5m信号方向一致
        elif sig5:
            score -= 0.5   # 5m信号反向，扣分

    # ── RR比 (0~2)
    entry = primary_sig["entry"]
    tp    = primary_sig["tp"]
    sl    = primary_sig["sl"]
    sl_d  = abs(entry - sl)
    tp_d  = abs(tp - entry)
    rr    = tp_d / sl_d if sl_d > 0 else 0
    if rr >= 2.0:   score += 2.0
    elif rr >= 1.5: score += 1.0
    elif rr >= 1.2: score += 0.5

    # ── RSI极值加分 (0~1)
    rsi = primary_sig["rsi"]
    if side == "short" and rsi >= 65: score += 1.0
    elif side == "long" and rsi <= 35: score += 1.0
    elif side == "short" and rsi >= 55: score += 0.5
    elif side == "long" and rsi <= 45: score += 0.5

    # ── 动态TP加分 (0~1)
    if primary_sig["dynamic_tp"]:
        score += 1.0

    return round(min(score, 10.0), 2)


# ══════════════════════════════════════════════════════════════
#  § 4  持仓退出 + 追踪止损
# ══════════════════════════════════════════════════════════════
def check_exit(pos: dict, high: float, low: float,
               open_: float = None) -> Tuple[Optional[str], Optional[float]]:
    """
    检查是否触发 TP/SL/TIMEOUT/TRAILING
    open价用于双触发时判断先后（v6.9修复）
    """
    side = pos["side"]
    entry = pos["entry"]
    sl    = pos["sl"]
    tp    = pos["tp"]

    if side == "short":
        tp_hit = low  <= tp
        sl_hit = high >= sl
        if tp_hit and sl_hit:
            if open_ is not None:
                return ("SL", sl) if open_ >= entry else ("TP", tp)
            return "SL", sl
        if tp_hit: return "TP",  tp
        if sl_hit: return "SL",  sl
    else:
        tp_hit = high >= tp
        sl_hit = low  <= sl
        if tp_hit and sl_hit:
            if open_ is not None:
                return ("SL", sl) if open_ <= entry else ("TP", tp)
            return "SL", sl
        if tp_hit: return "TP",  tp
        if sl_hit: return "SL",  sl

    # 超时强制平仓
    if pos.get("bars_held", 0) >= cfg.MAX_HOLD_BARS:
        cur_price = (high + low) / 2
        return "TIMEOUT", cur_price

    return None, None


def update_trailing_stop(pos: dict, high: float, low: float) -> dict:
    """
    追踪止损：浮盈≥0.5×ATR时将SL移至保本方向
    每次更新只收紧不放松
    """
    if not cfg.TRAILING_STOP_ENABLED:
        return pos
    atr   = pos.get("atr", 0)
    entry = pos["entry"]
    side  = pos["side"]
    if atr <= 0:
        return pos

    # 计算当前浮盈
    if side == "short":
        float_pnl_atr = (entry - low) / atr   # 用low估算浮盈
        if float_pnl_atr >= cfg.TRAILING_STOP_THRESH:
            new_sl = low + cfg.TRAILING_STOP_DIST * atr
            # 只收紧（new_sl < old_sl for short）
            if new_sl < pos["sl"]:
                pos = dict(pos)
                pos["sl"] = round(new_sl, 8)
                pos["trailing_active"] = True
    else:
        float_pnl_atr = (high - entry) / atr  # 用high估算浮盈
        if float_pnl_atr >= cfg.TRAILING_STOP_THRESH:
            new_sl = high - cfg.TRAILING_STOP_DIST * atr
            # 只收紧（new_sl > old_sl for long）
            if new_sl > pos["sl"]:
                pos = dict(pos)
                pos["sl"] = round(new_sl, 8)
                pos["trailing_active"] = True
    return pos


# ══════════════════════════════════════════════════════════════
#  § 5  Kelly 仓位计算
# ══════════════════════════════════════════════════════════════
class KellySizer:
    def __init__(self):
        self._wins: deque[int]   = deque(maxlen=cfg.WR_GUARD_WINDOW)
        self._pnls: deque[float] = deque(maxlen=cfg.WR_GUARD_WINDOW)

    def record(self, pnl: float):
        self._wins.append(1 if pnl > 0 else 0)
        self._pnls.append(pnl)

    def risk_amount(self, equity: float, sl_pct: float) -> float:
        """返回本笔应投入风险金额(U)"""
        if not cfg.KELLY_ENABLED or len(self._wins) < cfg.KELLY_MIN_TRADES:
            return equity * cfg.RISK_PCT

        wr    = sum(self._wins) / len(self._wins)
        wins  = [p for p in self._pnls if p > 0]
        loses = [p for p in self._pnls if p <= 0]
        if not wins or not loses:
            return equity * cfg.RISK_PCT

        avg_w = sum(wins) / len(wins)
        avg_l = abs(sum(loses) / len(loses))
        if avg_l < 1e-9:
            return equity * cfg.RISK_PCT

        kelly = wr - (1 - wr) / (avg_w / avg_l)
        risk  = max(0.005, min(cfg.KELLY_MAX_RISK, kelly * cfg.KELLY_FRACTION))
        return equity * risk / max(sl_pct, 0.001)

    @property
    def wr(self) -> float:
        return sum(self._wins) / max(len(self._wins), 1)


# ══════════════════════════════════════════════════════════════
#  § 6  WinRate Guard
# ══════════════════════════════════════════════════════════════
class WRGuard:
    def __init__(self):
        self._recent: deque[bool] = deque(maxlen=cfg.WR_GUARD_WINDOW)
        self._active = False

    def record(self, is_win: bool):
        self._recent.append(is_win)
        n  = len(self._recent)
        wr = sum(self._recent) / n if n > 0 else 1.0
        if not self._active and wr < cfg.WR_GUARD_MIN_WR and n >= 10:
            self._active = True
            logger.warning(f"⚠️  WRGuard激活: 近{n}笔胜率={wr:.0%} < {cfg.WR_GUARD_MIN_WR:.0%}，提升RR要求→{cfg.WR_GUARD_MIN_RR}")
        elif self._active and wr >= cfg.WR_GUARD_BOOST_WR:
            self._active = False
            logger.info(f"✅  WRGuard解除: 近{n}笔胜率={wr:.0%} ≥ {cfg.WR_GUARD_BOOST_WR:.0%}")

    @property
    def active(self) -> bool:
        return self._active

    @property
    def min_rr(self) -> float:
        return cfg.WR_GUARD_MIN_RR if self._active else cfg.MIN_RR_RATIO


# ══════════════════════════════════════════════════════════════
#  § 7  相关性控制
# ══════════════════════════════════════════════════════════════
class CorrFilter:
    """高相关品种同侧持仓不超过 MAX_CORR_SAME_SIDE"""
    def __init__(self, positions: dict):
        self._pos = positions

    def allow(self, symbol: str, side: str) -> bool:
        if symbol not in cfg.HIGH_CORR_GROUP:
            return True
        same_side_count = sum(
            1 for s, p in self._pos.items()
            if s in cfg.HIGH_CORR_GROUP and p["side"] == side
        )
        if same_side_count >= cfg.MAX_CORR_SAME_SIDE:
            logger.debug(f"[CorrFilter] {symbol} {side} 被拦截: 高相关组同侧已有{same_side_count}仓")
            return False
        return True


# ══════════════════════════════════════════════════════════════
#  § 8  状态持久化（CRC校验防损坏，原子写）
# ══════════════════════════════════════════════════════════════
def _default_state() -> dict:
    return {
        "positions":    {},
        "equity":       cfg.INITIAL_EQUITY,
        "peak_equity":  cfg.INITIAL_EQUITY,
        "max_drawdown": 0.0,
        "day_loss":     0.0,
        "day_date":     "",
        "total_trades": 0,
        "wins":         0,
        "losses":       0,
        "total_pnl":    0.0,
        "streak":       0,
        "daily_stats":  {},
    }


def _crc(data: dict) -> str:
    raw = {k: v for k, v in data.items() if k != "_crc"}
    return hashlib.md5(json.dumps(raw, sort_keys=True).encode()).hexdigest()


def load_state() -> dict:
    p = Path(cfg.STATE_FILE)
    if p.exists():
        try:
            raw = json.loads(p.read_text())
            stored_crc = raw.pop("_crc", "")
            if stored_crc and stored_crc != _crc(raw):
                logger.warning("⚠️  state CRC不匹配，使用默认状态")
            else:
                s = _default_state()
                s.update(raw)
                return s
        except Exception as e:
            logger.warning(f"⚠️  状态加载失败({e})，使用默认状态")
    return _default_state()


def save_state(s: dict) -> None:
    data = dict(s)
    data["_crc"] = _crc(data)
    tmp = Path(cfg.STATE_FILE).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(Path(cfg.STATE_FILE))


# ══════════════════════════════════════════════════════════════
#  § 9  交易记录（JSONL流式追加）
# ══════════════════════════════════════════════════════════════
def append_trade(rec: dict) -> None:
    with open(cfg.TRADE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_trades() -> list:
    p = Path(cfg.TRADE_LOG)
    if not p.exists():
        return []
    trades = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                trades.append(json.loads(line))
            except Exception:
                pass
    return trades


# ══════════════════════════════════════════════════════════════
#  § 10 统计辅助
# ══════════════════════════════════════════════════════════════
def update_stats(state: dict, net_pnl: float) -> None:
    """更新连胜连败 / 最大回撤 / 每日统计"""
    state["streak"] = (max(state["streak"], 0) + 1) if net_pnl > 0 \
                      else (min(state["streak"], 0) - 1)

    if state["equity"] > state["peak_equity"]:
        state["peak_equity"] = state["equity"]
    dd = (state["peak_equity"] - state["equity"]) / state["peak_equity"] * 100
    if dd > state["max_drawdown"]:
        state["max_drawdown"] = dd

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today not in state["daily_stats"]:
        state["daily_stats"][today] = {"pnl": 0.0, "trades": 0, "wins": 0}
    state["daily_stats"][today]["pnl"]    += net_pnl
    state["daily_stats"][today]["trades"] += 1
    if net_pnl > 0:
        state["daily_stats"][today]["wins"] += 1


def print_summary(state: dict, trades: list) -> None:
    total = state["wins"] + state["losses"]
    wr    = state["wins"] / total * 100 if total > 0 else 0.0
    logger.info("═" * 72)
    logger.info(f"  白夜交易系统 {VERSION} — 阶段汇总")
    logger.info(f"  完成: {total}笔 | 胜率: {wr:.1f}% | 总PnL: {state['total_pnl']:+.4f}U")
    logger.info(f"  权益: {cfg.INITIAL_EQUITY}U → {state['equity']:.4f}U "
                f"({(state['equity']/cfg.INITIAL_EQUITY-1)*100:+.2f}%)")
    logger.info(f"  最大回撤: {state['max_drawdown']:.1f}% | 连胜/连败: {state['streak']:+d}")
    if trades:
        w_pnl = [t["net_pnl"] for t in trades if t["net_pnl"] > 0]
        l_pnl = [t["net_pnl"] for t in trades if t["net_pnl"] < 0]
        avg_w = sum(w_pnl) / len(w_pnl) if w_pnl else 0
        avg_l = sum(l_pnl) / len(l_pnl) if l_pnl else 0
        pf = (avg_w * len(w_pnl)) / (abs(avg_l) * len(l_pnl)) \
             if l_pnl and avg_l != 0 else 0
        logger.info(f"  平均盈: {avg_w:+.4f}U | 平均亏: {avg_l:+.4f}U | PF={pf:.2f}")
        # 多周期分布
        by_tf: Dict[str, list] = {}
        for t in trades:
            tf = t.get("tf", "15m")
            by_tf.setdefault(tf, []).append(t["net_pnl"])
        for tf, pnls in sorted(by_tf.items()):
            tw = sum(1 for p in pnls if p > 0)
            logger.info(f"    [{tf}] {len(pnls)}笔 WR={tw/len(pnls)*100:.0f}% PnL={sum(pnls):+.3f}U")
    if state.get("daily_stats"):
        logger.info("  ── 每日收益 ──")
        for date, ds in sorted(state["daily_stats"].items()):
            dwr = ds["wins"] / ds["trades"] * 100 if ds["trades"] > 0 else 0
            logger.info(f"    {date}: PnL={ds['pnl']:+.3f}U WR={dwr:.0f}% "
                        f"({ds['wins']}/{ds['trades']})")
    logger.info("═" * 72)


# ══════════════════════════════════════════════════════════════
#  § 11 主循环
# ══════════════════════════════════════════════════════════════
def main() -> None:
    global _running

    # 写PID
    Path(cfg.PID_FILE).write_text(str(os.getpid()))

    state  = load_state()
    trades = load_trades()

    # 初始化辅助器
    kelly   = KellySizer()
    wr_guard = WRGuard()

    # 用历史trades预热Kelly/WRGuard
    for t in trades[-cfg.WR_GUARD_WINDOW:]:
        kelly.record(t["net_pnl"])
        wr_guard.record(t["net_pnl"] > 0)

    # 重置当日熔断
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("day_date") != today:
        state["day_loss"] = 0.0
        state["day_date"] = today

    logger.info("═" * 72)
    logger.info(f"  白夜交易系统 {VERSION} 启动  模式={cfg.RUN_MODE}")
    logger.info(f"  品种({len(cfg.SYMBOLS)}): {cfg.SYMBOLS}")
    logger.info(f"  时间周期: {cfg.TIMEFRAMES} | 主周期={cfg.TF_PRIMARY}")
    logger.info(f"  资金={cfg.INITIAL_EQUITY}U RISK={cfg.RISK_PCT*100:.0f}% FEE={cfg.FEE*10000:.1f}bps")
    logger.info(f"  Kelly={'ON' if cfg.KELLY_ENABLED else 'OFF'} "
                f"WRGuard={cfg.WR_GUARD_MIN_WR:.0%} "
                f"TrailingStop={'ON' if cfg.TRAILING_STOP_ENABLED else 'OFF'}")
    logger.info(f"  动态TP: ADX≥{cfg.DYNAMIC_TP_ADX_TH}→TP×{cfg.DYNAMIC_TP_MULT}")
    logger.info(f"  已完成={state['wins']+state['losses']}笔 "
                f"权益={state['equity']:.2f}U 回撤={state['max_drawdown']:.1f}%")
    logger.info("═" * 72)

    cooldown: Dict[str, int] = {}   # sym → 上次开仓poll_count
    last_bar: Dict[str, int] = {}   # sym → 上次信号bar_ts
    poll_count = 0

    while _running:
        try:
            poll_count += 1
            now   = datetime.now(timezone.utc)
            today = now.strftime("%Y-%m-%d")

            # 日期切换
            if state.get("day_date") != today:
                state["day_loss"] = 0.0
                state["day_date"] = today
                logger.info("━━ 新的一天，日熔断计数已重置 ━━")

            # 日熔断
            if state["day_loss"] >= state["equity"] * cfg.DAILY_LOSS_PCT:
                logger.warning(
                    f"⛔ 日熔断! 今日亏损={state['day_loss']:.4f}U"
                    f" ≥ {cfg.DAILY_LOSS_PCT*100:.0f}%×{state['equity']:.2f}U，等待60s"
                )
                time.sleep(60)
                continue

            # ── 1. 拉取所有品种多周期K线
            all_tf_data: Dict[str, Dict[str, Optional[pd.DataFrame]]] = {}
            for sym in cfg.SYMBOLS:
                all_tf_data[sym] = fetch_multi_tf(sym)

            # ── 2. 检查持仓退出 + 追踪止损
            for sym in list(state["positions"].keys()):
                pos  = state["positions"][sym]
                tf   = pos.get("tf", cfg.TF_PRIMARY)
                df   = all_tf_data.get(sym, {}).get(tf)
                if df is None:
                    continue

                cur   = df.iloc[-1]
                high  = float(cur["high"])
                low   = float(cur["low"])
                open_ = float(cur["open"])

                pos["bars_held"] = pos.get("bars_held", 0) + 1

                # 追踪止损更新
                pos = update_trailing_stop(pos, high, low)
                state["positions"][sym] = pos

                result, exit_price = check_exit(pos, high, low, open_)
                if result is None:
                    continue

                # 结算
                qty      = pos["qty"]
                raw_pnl  = ((pos["entry"] - exit_price) * qty if pos["side"] == "short"
                            else (exit_price - pos["entry"]) * qty)
                fee_cost = (pos["entry"] + exit_price) * qty * cfg.FEE
                net_pnl  = raw_pnl - fee_cost

                state["equity"]      += net_pnl
                state["total_pnl"]    = state.get("total_pnl", 0.0) + net_pnl
                state["total_trades"] = state.get("total_trades", 0) + 1

                if net_pnl > 0:
                    state["wins"] = state.get("wins", 0) + 1
                else:
                    state["losses"]    = state.get("losses", 0) + 1
                    state["day_loss"] += abs(net_pnl)

                update_stats(state, net_pnl)
                kelly.record(net_pnl)
                wr_guard.record(net_pnl > 0)

                del state["positions"][sym]

                rec = {
                    "no":           state["total_trades"],
                    "sym":          sym,
                    "tf":           tf,
                    "side":         pos["side"],
                    "entry":        round(pos["entry"], 8),
                    "exit":         round(exit_price, 8),
                    "qty":          round(qty, 6),
                    "notional":     round(pos.get("notional", 0), 4),
                    "result":       result,
                    "net_pnl":      round(net_pnl, 6),
                    "fee":          round(fee_cost, 6),
                    "bars_held":    pos.get("bars_held", 0),
                    "score":        pos.get("score", 0),
                    "adx":          pos.get("adx"),
                    "atr":          pos.get("atr"),
                    "tp_s":         pos.get("tp_s"),
                    "dynamic_tp":   pos.get("dynamic_tp", False),
                    "trailing":     pos.get("trailing_active", False),
                    "cu":  pos.get("cu"), "cd": pos.get("cd"), "cc": pos.get("cc"),
                    "open_time":    pos["open_time"],
                    "close_time":   now.isoformat(),
                    "equity_after": round(state["equity"], 6),
                }
                trades.append(rec)
                append_trade(rec)
                save_state(state)

                total = state["wins"] + state["losses"]
                wr    = state["wins"] / total * 100 if total > 0 else 0
                emoji = "✅" if net_pnl > 0 else "❌"
                trail = "🔒TRL" if rec["trailing"] else ""
                dtp   = "🚀DTP" if rec["dynamic_tp"] else ""
                streak_s = f"连{'胜' if state['streak']>0 else '败'}{abs(state['streak'])}"
                logger.info(
                    f"{emoji} #{state['total_trades']:3d} [{tf}] {sym} {pos['side'].upper()} "
                    f"{result}{trail}{dtp} | "
                    f"入={pos['entry']:.5g} 出={exit_price:.5g} PnL={net_pnl:+.5f}U | "
                    f"WR={wr:.1f}%({state['wins']}/{total}) "
                    f"净值={state['equity']:.4f}U {streak_s}"
                )

            # ── 3. 扫描新信号（多周期 + 评分过滤）
            if len(state["positions"]) < cfg.MAX_OPEN_POSITIONS:
                corr = CorrFilter(state["positions"])

                for sym in cfg.SYMBOLS:
                    if sym in state["positions"]:
                        continue
                    if len(state["positions"]) >= cfg.MAX_OPEN_POSITIONS:
                        break

                    tf_data = all_tf_data.get(sym, {})
                    sym_cfg = cfg.SYMBOL_CONFIGS[sym]

                    # 优先主周期15m，再检查5m和3m
                    best_sig  = None
                    best_tf   = None
                    best_score = -1.0

                    for tf in [cfg.TF_PRIMARY, cfg.TF_FAST, "3m"]:
                        # 冷却期：同品种同tf
                        ck = f"{sym}_{tf}"
                        if poll_count - cooldown.get(ck, 0) < cfg.COOLDOWN_BARS:
                            continue

                        df = tf_data.get(tf)
                        sig = _raw_signal(sym, df, sym_cfg)
                        if sig is None:
                            continue

                        # 同根K线去重
                        lk = f"{sym}_{tf}"
                        if last_bar.get(lk) == sig["bar_ts"]:
                            continue

                        # 计算多周期评分
                        score = compute_signal_score(sym, tf_data, sym_cfg, sig)

                        # WRGuard模式下要求更高RR
                        entry = sig["entry"]
                        sl    = sig["sl"]
                        tp    = sig["tp"]
                        rr    = abs(tp - entry) / max(abs(entry - sl), 1e-9)
                        if rr < wr_guard.min_rr:
                            logger.debug(f"[{sym}/{tf}] RR={rr:.2f} < WRGuard要求{wr_guard.min_rr:.1f}，跳过")
                            continue

                        if score > best_score:
                            best_score = score
                            best_sig   = sig
                            best_tf    = tf

                    if best_sig is None:
                        continue

                    # 最低评分过滤
                    if best_score < cfg.SIGNAL_MIN_SCORE:
                        continue

                    # 相关性过滤
                    if not corr.allow(sym, best_sig["side"]):
                        continue

                    # Kelly仓位计算
                    sl_pct = abs(best_sig["entry"] - best_sig["sl"]) / best_sig["entry"]
                    risk_u = kelly.risk_amount(state["equity"], sl_pct)
                    sl_d   = abs(best_sig["entry"] - best_sig["sl"])
                    if sl_d <= 0:
                        continue
                    qty      = risk_u / sl_d
                    notional = qty * best_sig["entry"]

                    # 名义值保护
                    if notional < cfg.MIN_NOTIONAL:
                        continue
                    if notional > state["equity"] * 0.5:
                        qty      = state["equity"] * 0.5 / best_sig["entry"]
                        notional = qty * best_sig["entry"]

                    # 记录开仓
                    ck = f"{sym}_{best_tf}"
                    lk = f"{sym}_{best_tf}"
                    last_bar[lk]  = best_sig["bar_ts"]
                    cooldown[ck]  = poll_count

                    state["positions"][sym] = {
                        "side":         best_sig["side"],
                        "tf":           best_tf,
                        "entry":        best_sig["entry"],
                        "sl":           best_sig["sl"],
                        "tp":           best_sig["tp"],
                        "qty":          qty,
                        "notional":     round(notional, 4),
                        "score":        best_score,
                        "adx":          best_sig["adx"],
                        "atr":          best_sig["atr"],
                        "rsi":          best_sig["rsi"],
                        "tp_s":         best_sig["tp_s"],
                        "dynamic_tp":   best_sig["dynamic_tp"],
                        "cu":           best_sig["cu"],
                        "cd":           best_sig["cd"],
                        "cc":           best_sig["cc"],
                        "bar_ts":       best_sig["bar_ts"],
                        "bars_held":    0,
                        "trailing_active": False,
                        "open_time":    now.isoformat(),
                    }
                    corr = CorrFilter(state["positions"])  # 更新相关性检查器
                    save_state(state)

                    total = state["wins"] + state["losses"]
                    dtp_s = f" 🚀DTP×{best_sig['tp_s']}" if best_sig["dynamic_tp"] else ""
                    wr_s  = " ⚠️WRG" if wr_guard.active else ""
                    logger.info(
                        f"🔔 #{total+len(state['positions']):3d} "
                        f"[{best_tf}] {sym} {best_sig['side'].upper()}"
                        f"{dtp_s}{wr_s} 评分={best_score:.1f} | "
                        f"入={best_sig['entry']:.5g} "
                        f"TP={best_sig['tp']:.5g} SL={best_sig['sl']:.5g} | "
                        f"ADX={best_sig['adx']} RSI={best_sig['rsi']} "
                        f"notional={notional:.1f}U"
                    )

            # ── 4. 每轮状态摘要
            total = state["wins"] + state["losses"]
            wr    = state["wins"] / total * 100 if total > 0 else 0.0
            pos_str = (
                " | ".join(
                    f"{s}({v['side'].upper()}/{v.get('tf','?')}@{v['entry']:.4g}"
                    f" sc={v.get('score',0):.1f})"
                    for s, v in state["positions"].items()
                ) or "无持仓"
            )
            logger.info(
                f"[{now.strftime('%H:%M')} #{poll_count}] "
                f"完成={total} WR={wr:.0f}% PnL={state['total_pnl']:+.3f}U "
                f"净值={state['equity']:.2f}U 回撤={state['max_drawdown']:.1f}% | {pos_str}"
            )

            # 无持仓时显示各品种信号距离（主周期15m）
            if not state["positions"]:
                for sym in cfg.SYMBOLS:
                    df = all_tf_data.get(sym, {}).get(cfg.TF_PRIMARY)
                    if df is None or len(df) < 5:
                        continue
                    row = df.iloc[-2]
                    adx = float(row["adx"]) if not np.isnan(row["adx"]) else 0.0
                    cu  = int(row["cu"]); cd = int(row["cd"])
                    cc  = float(row["cc"]) * 100
                    sc  = cfg.SYMBOL_CONFIGS[sym]
                    short_ok = cu >= sc["sc"] and cc/100 >= sc["ccp"] and adx >= sc["adx_th"]
                    long_ok  = (not sc["long_disabled"] and cd >= sc["lc"]
                                and cc/100 <= -sc["ccp"] and adx >= sc["adx_th"])
                    s_str = "✅SHORT" if short_ok else \
                        f"SHORT[cu{cu}/{sc['sc']} cc{cc:.2f}%/{sc['ccp']*100:.2f}% adx{adx:.0f}/{sc['adx_th']}]"
                    l_str = "LONG禁" if sc["long_disabled"] else (
                        "✅LONG" if long_ok else
                        f"LONG[cd{cd}/{sc['lc']} cc{cc:.2f}%/{-sc['ccp']*100:.2f}% adx{adx:.0f}/{sc['adx_th']}]"
                    )
                    logger.info(f"  {sym:10s} ADX={adx:4.0f} | {s_str} | {l_str}")

            # 每20轮输出性能摘要
            if poll_count % 20 == 0 and total > 0:
                elapsed_h = poll_count * cfg.POLL_SECS / 3600
                rate = total / elapsed_h if elapsed_h > 0 else 0
                logger.info(
                    f"╪ 性能摘要 #{poll_count} ╪ "
                    f"运行{elapsed_h:.1f}h | {rate:.1f}笔/h | "
                    f"Kelly WR={kelly.wr:.0%} | WRGuard={'激活' if wr_guard.active else '正常'}"
                )

            time.sleep(cfg.POLL_SECS)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"主循环异常(轮次{poll_count}): {e}", exc_info=True)
            time.sleep(30)

    # ── 收尾
    logger.info("引擎退出，保存最终状态...")
    save_state(state)
    print_summary(state, trades)
    try:
        Path(cfg.PID_FILE).unlink(missing_ok=True)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    main()
