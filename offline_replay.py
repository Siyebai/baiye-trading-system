#!/usr/bin/env python3
"""
白夜交易系统 v9.0 — 离线回放引擎
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
用途：在无网络环境（GFW阻断Binance API）下，基于本地CSV历史数据回放策略，
      生成完整的交易记录和统计报告，验证策略参数真实表现。

数据源：data/*_USDT_15m_180d.csv（8个品种，180天，15分钟K线）
策略：纯均值回归 (MR)，双信号 quick2 + mr_signal
风控：Kelly仓位 / WRGuard / 熔断 / 3段追踪止损 / 相关性过滤

输出：
  1. data/trades_v90.jsonl — 完整交易记录
  2. data/state_v90.json — 回放结束状态
  3. 控制台统计报告

运行：python offline_replay.py [--quick] [--fast]
  --quick: 仅生成快速摘要（不写trades文件）
  --fast:  仅处理SOL+BTC（快速调试）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations
import argparse, json, logging, os, sys, time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

try:
    from numba import njit
    HAS_NUMBA = True
except ImportError:
    HAS_NUMBA = False

# ═══════════════════════ 配置 (与 main_v90.py 同步) ═══════════════════════
@dataclass(frozen=True)
class _S:
    sc: int = 4; lc: int = 3; ccp: float = 0.001
    adx_th: float = 20; tp: float = 0.8; sl: float = 1.5
    long: bool = True; short: bool = True
    vf: bool = False; rf: bool = False; vt: float = 1.2

SYM = {
    "TONUSDT": _S(sc=3, lc=3, adx_th=20, tp=2.0, sl=1.5),
    "SUIUSDT": _S(sc=3, lc=3, adx_th=20, tp=2.0, sl=2.0),
    "POLUSDT": _S(sc=3, lc=3, adx_th=20, tp=2.0, sl=2.0),
    "DOTUSDT": _S(sc=3, lc=3, adx_th=20, tp=2.0, sl=1.5),
    "BTCUSDT": _S(sc=3, lc=3, adx_th=20, tp=2.0, sl=2.0),
    "SOLUSDT": _S(sc=3, lc=3, adx_th=20, tp=2.0, sl=2.0),
    "DOGEUSDT": _S(sc=3, lc=3, adx_th=20, tp=2.0, sl=1.5),
    "XRPUSDT": _S(sc=3, lc=3, adx_th=20, tp=2.0, sl=2.0),
    "BNBUSDT": _S(sc=3, lc=3, adx_th=20, tp=2.0, sl=2.0),
    "LINKUSDT": _S(sc=3, lc=3, adx_th=20, tp=2.0, sl=1.5),
    "ETHUSDT": _S(sc=0, lc=0, adx_th=20, tp=2.0, sl=2.0),
    "ADAUSDT": _S(sc=3, lc=3, adx_th=20, tp=2.0, sl=2.0),
    "AVAXUSDT": _S(sc=3, lc=3, adx_th=20, tp=2.0, sl=2.0),
    "NEARUSDT": _S(sc=3, lc=3, adx_th=20, tp=2.0, sl=2.0),
    "UNIUSDT": _S(sc=3, lc=3, adx_th=20, tp=2.0, sl=2.0),
    "AAVEUSDT": _S(sc=3, lc=3, adx_th=20, tp=2.0, sl=2.0),
    "OPUSDT": _S(sc=3, lc=3, adx_th=20, tp=2.0, sl=2.0),
    "ARBUSDT": _S(sc=3, lc=3, adx_th=20, tp=2.0, sl=2.0),
    "TIAUSDT": _S(sc=3, lc=3, adx_th=20, tp=2.0, sl=2.0),
    "WIFUSDT": _S(sc=3, lc=3, adx_th=20, tp=2.0, sl=2.0),
}

SC = {s: {"sc": c.sc, "lc": c.lc, "ccp": c.ccp, "adx_th": c.adx_th,
          "tp_s": c.tp, "sl_atr": c.sl,
          "long_disabled": False, "short_disabled": False,
          "vol_filter": False, "rsi_filter": False, "vol_th": 1.2}
      for s, c in SYM.items()}

# 全局参数
INIT_EQ = 150.0
FEE = 0.0002
DAILY_LOSS = 0.08
MAX_POS = 8
MAX_HOLD = 40
COOLDOWN = 1
MIN_NOTIONAL = 5.0
MIN_RR = 0.5
TF_P = "15m"
SIG_MIN = 1.0
DTP_TH = 30
DTP_M = 1.3

# 追踪止损
TRAIL = True
T_BE = 1.0; T_LK = 1.5; T_DY = 2.0; T_DD = 1.0  # 上调：需1ATR才保本，避免首bar即触发

# Kelly配置
KELLY = True; K_FRAC = 0.25; K_MIN = 8; K_MAX = 0.04; RISK = 0.015

# WRGuard
WRG_W = 30; WRG_MIN = 0.25; WRG_B = 0.55; WRG_RR = 0.8; WRG_P = 0.10
WRG_WARMUP = 20  # 前20笔不触发WRGuard暂停，避免早期连亏误杀

# 相关性
CORR_G = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "DOTUSDT", "XRPUSDT",
          "DOGEUSDT", "LINKUSDT", "ADAUSDT", "AVAXUSDT", "NEARUSDT"}
CORR_MAX = 3

_B = Path(__file__).parent
DATA_DIR = _B / "data"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("replay")

# ═══════════════════════ Numba 加速指标 ═══════════════════════
if HAS_NUMBA:
    @njit(nogil=True, cache=True)
    def _wnb(arr, n):
        L = len(arr); out = np.full(L, np.nan); s = -1
        for i in range(L):
            if not np.isnan(arr[i]): s = i; break
        if s < 0 or L - s < n: return out
        sm, cnt = 0.0, 0
        for i in range(s, s + n):
            if not np.isnan(arr[i]): sm += arr[i]; cnt += 1
        out[s + n - 1] = sm / cnt if cnt > 0 else np.nan
        for i in range(s + n, L):
            if not np.isnan(out[i - 1]): out[i] = out[i - 1] * (n - 1.0) / n + arr[i] / n
        return out

    @njit(nogil=True, cache=True)
    def _inb(h, l, c, v):
        L = len(c); tr = np.zeros(L); tr[0] = h[0] - l[0]
        for i in range(1, L):
            a1 = h[i] - l[i]; a2 = abs(h[i] - c[i - 1]); a3 = abs(l[i] - c[i - 1])
            mx = a2 if a2 > a3 else a3; tr[i] = a1 if a1 > mx else mx
        atr = _wnb(tr, 14)
        ema = np.zeros(L); ema[0] = c[0]
        for i in range(1, L): ema[i] = ema[i - 1] + (2.0 / 201.0) * (c[i] - ema[i - 1])
        up = np.zeros(L); dn = np.zeros(L); pdm = np.zeros(L); ndm = np.zeros(L)
        for i in range(1, L): up[i] = h[i] - h[i - 1]; dn[i] = -(l[i] - l[i - 1])
        for i in range(L):
            if up[i] > dn[i] and up[i] > 0: pdm[i] = up[i]
            if dn[i] > up[i] and dn[i] > 0: ndm[i] = dn[i]
        a14 = _wnb(tr, 14); pw = _wnb(pdm, 14); nw = _wnb(ndm, 14)
        pdi = np.full(L, np.nan); ndi = np.full(L, np.nan)
        for i in range(L):
            if a14[i] > 0 and not np.isnan(a14[i]):
                pdi[i] = 100.0 * pw[i] / a14[i]; ndi[i] = 100.0 * nw[i] / a14[i]
        dx = np.full(L, np.nan)
        for i in range(L):
            d = pdi[i] + ndi[i]
            if d > 0 and not np.isnan(d): dx[i] = 100.0 * abs(pdi[i] - ndi[i]) / d
        adx = _wnb(dx, 14)
        gain = np.zeros(L); loss = np.zeros(L)
        for i in range(1, L):
            d = c[i] - c[i - 1]
            if d > 0: gain[i] = d
            elif d < 0: loss[i] = -d
        ag = _wnb(gain, 14); al = _wnb(loss, 14); rsi = np.full(L, 50.0)
        for i in range(L):
            if al[i] > 0 and not np.isnan(al[i]):
                rs = ag[i] / al[i]; rsi[i] = 100.0 - 100.0 / (1.0 + rs)
        vr = np.ones(L)
        if v is not None:
            for i in range(20, L):
                sm = 0.0
                for j in range(i - 20, i): sm += v[j]
                mv = sm / 20.0; vr[i] = v[i] / mv if mv > 1e-12 else 1.0
        cu = np.zeros(L, dtype=np.int64); cd = np.zeros(L, dtype=np.int64)
        cc = np.zeros(L)
        for i in range(1, L):
            chg = (c[i] - c[i - 1]) / c[i - 1]
            if chg > 0.001: cu[i] = cu[i - 1] + 1; cd[i] = 0
            elif chg < -0.001: cd[i] = cd[i - 1] + 1; cu[i] = 0
            else: cu[i] = cu[i - 1]; cd[i] = cd[i - 1]
        for i in range(1, L):
            if cu[i] > 0 and cu[i - 1] == 0:
                s = max(0, i - cu[i])
                cc[i] = sum((c[j] - c[j - 1]) / c[j - 1] for j in range(s + 1, i + 1))
            if cd[i] > 0 and cd[i - 1] == 0:
                s = max(0, i - cd[i])
                cc[i] = sum((c[j] - c[j - 1]) / c[j - 1] for j in range(s + 1, i + 1))
        return atr, ema, adx, rsi, vr, cu, cd, cc
else:
    # Python fallback (slower but functional)
    def _wnb(arr, n):
        L = len(arr); out = np.full(L, np.nan)
        s = -1
        for i in range(L):
            if not np.isnan(arr[i]): s = i; break
        if s < 0 or L - s < n: return out
        sm, cnt = 0.0, 0
        for i in range(s, s + n):
            if not np.isnan(arr[i]): sm += arr[i]; cnt += 1
        out[s + n - 1] = sm / cnt if cnt > 0 else np.nan
        for i in range(s + n, L):
            if not np.isnan(out[i - 1]): out[i] = out[i - 1] * (n - 1.0) / n + arr[i] / n
        return out

    def _inb(h, l, c, v):
        h, l, c = np.array(h), np.array(l), np.array(c)
        L = len(c); tr = np.zeros(L); tr[0] = h[0] - l[0]
        for i in range(1, L):
            a1 = h[i] - l[i]; a2 = abs(h[i] - c[i - 1]); a3 = abs(l[i] - c[i - 1])
            tr[i] = max(a1, a2, a3)
        atr = _wnb(tr, 14)
        ema = np.zeros(L); ema[0] = c[0]
        for i in range(1, L): ema[i] = ema[i - 1] + (2.0 / 201.0) * (c[i] - ema[i - 1])
        up = np.zeros(L); dn = np.zeros(L); pdm = np.zeros(L); ndm = np.zeros(L)
        for i in range(1, L): up[i] = h[i] - h[i - 1]; dn[i] = -(l[i] - l[i - 1])
        for i in range(L):
            if up[i] > dn[i] and up[i] > 0: pdm[i] = up[i]
            if dn[i] > up[i] and dn[i] > 0: ndm[i] = dn[i]
        a14 = _wnb(tr, 14); pw = _wnb(pdm, 14); nw = _wnb(ndm, 14)
        pdi = np.full(L, np.nan); ndi = np.full(L, np.nan)
        for i in range(L):
            if a14[i] > 0 and not np.isnan(a14[i]):
                pdi[i] = 100.0 * pw[i] / a14[i]; ndi[i] = 100.0 * nw[i] / a14[i]
        dx = np.full(L, np.nan)
        for i in range(L):
            d = pdi[i] + ndi[i]
            if d > 0 and not np.isnan(d): dx[i] = 100.0 * abs(pdi[i] - ndi[i]) / d
        adx = _wnb(dx, 14)
        gain = np.zeros(L); loss = np.zeros(L)
        for i in range(1, L):
            d = c[i] - c[i - 1]
            if d > 0: gain[i] = d
            elif d < 0: loss[i] = -d
        ag = _wnb(gain, 14); al = _wnb(loss, 14); rsi = np.full(L, 50.0)
        for i in range(L):
            if al[i] > 0 and not np.isnan(al[i]): rs = ag[i] / al[i]; rsi[i] = 100.0 - 100.0 / (1.0 + rs)
        vr = np.ones(L)
        if v is not None:
            for i in range(20, L):
                sm = float(np.sum(v[i - 20:i]))
                mv = sm / 20.0; vr[i] = v[i] / mv if mv > 1e-12 else 1.0
        cu = np.zeros(L, dtype=np.int64); cd = np.zeros(L, dtype=np.int64)
        cc = np.zeros(L)
        for i in range(1, L):
            chg = (c[i] - c[i - 1]) / c[i - 1]
            if chg > 0.001: cu[i] = cu[i - 1] + 1; cd[i] = 0
            elif chg < -0.001: cd[i] = cd[i - 1] + 1; cu[i] = 0
            else: cu[i] = cu[i - 1]; cd[i] = cd[i - 1]
        for i in range(1, L):
            if cu[i] > 0 and cu[i - 1] == 0:
                s = max(0, i - cu[i])
                cc[i] = sum((c[j] - c[j - 1]) / c[j - 1] for j in range(s + 1, i + 1))
            if cd[i] > 0 and cd[i - 1] == 0:
                s = max(0, i - cd[i])
                cc[i] = sum((c[j] - c[j - 1]) / c[j - 1] for j in range(s + 1, i + 1))
        return atr, ema, adx, rsi, vr, cu, cd, cc


def compute_indicators(df):
    """计算全部技术指标，附加到DataFrame"""
    h = df["high"].values; l = df["low"].values
    c = df["close"].values; v = df["vol"].values
    atr, ema, adx, rsi, vr, cu, cd, cc = _inb(h, l, c, v)
    df = df.copy()
    df["atr"] = atr; df["ema200"] = ema; df["adx"] = adx
    df["rsi"] = rsi; df["vol_ratio"] = vr
    df["cu"] = cu; df["cd"] = cd; df["cc"] = cc
    return df


# ═══════════════════════ 策略信号 ═══════════════════════
def _real_cc(df, idx, count):
    """实时计算最近count根K线的累积变化率（修复cc预存bug）"""
    if idx < count or count <= 0: return 0.0
    closes = df["close"].values
    total = 0.0
    for j in range(idx - count + 1, idx + 1):
        if j > 0 and closes[j - 1] > 0:
            total += (closes[j] - closes[j - 1]) / closes[j - 1]
    return total

def mr_signal(sym, df, sc, idx):
    """纯均值回归策略 (与main_v90.py mr_signal完全一致)"""
    if idx < 220: return None  # 需要足够的历史数据
    row = df.iloc[idx - 1]  # 用前一根K线的指标
    cur = df.iloc[idx]       # 用当前K线开仓
    adx = float(row["adx"]) if not np.isnan(row["adx"]) else 0
    atr = float(row["atr"]) if not np.isnan(row["atr"]) else 0
    ema = float(row["ema200"]) if not np.isnan(row["ema200"]) else 0
    rsi = float(row["rsi"]) if not np.isnan(row["rsi"]) else 50
    # 均值回归=震荡市(低ADX)才交易，趋势市(高ADX)跳过
    if adx > sc["adx_th"] or atr <= 0: return None
    cu = int(row["cu"]); cd = int(row["cd"])
    entry = float(cur["close"])
    vr = float(row["vol_ratio"]) if not np.isnan(row["vol_ratio"]) else 1.0
    uvf = sc.get("vol_filter", False); urf = sc.get("rsi_filter", False)
    vth = sc.get("vol_th", 1.2)
    tp_s = sc["tp_s"]; dtp = False
    if adx >= DTP_TH: tp_s *= DTP_M; dtp = True
    sv_ok = (not uvf) or (vr >= vth); sr_ok = (not urf) or (rsi >= 55)

    # 实时计算cc（修复_inb中cc仅首bar赋值问题）
    short_cc = _real_cc(df, idx - 1, cu) if cu >= sc["sc"] else 0.0
    long_cc = _real_cc(df, idx - 1, cd) if cd >= sc["lc"] else 0.0
    
    if (cu >= sc["sc"] and short_cc >= sc["ccp"] and sv_ok and sr_ok
            and not sc.get("short_disabled", False)):
        return {"side": "short", "entry": entry,
                "sl": entry + sc["sl_atr"] * atr,
                "tp": entry - tp_s * atr,
                "adx": round(adx, 1), "atr": round(atr, 8),
                "rsi": round(rsi, 1), "tp_s": round(tp_s, 2),
                "dynamic_tp": dtp, "cu": cu, "cd": cd,
                "cc": round(short_cc, 6), "ema200": round(ema, 4),
                "vol_ratio": round(vr, 2), "strategy": "mr",
                "bar_idx": idx}

    lv_ok = (not uvf) or (vr >= vth); lr_ok = (not urf) or (rsi <= 45)
    if (not sc["long_disabled"] and cd >= sc["lc"]
            and long_cc <= -sc["ccp"] and entry > ema and lv_ok and lr_ok):
        return {"side": "long", "entry": entry,
                "sl": entry - sc["sl_atr"] * atr,
                "tp": entry + tp_s * atr,
                "adx": round(adx, 1), "atr": round(atr, 8),
                "rsi": round(rsi, 1), "tp_s": round(tp_s, 2),
                "dynamic_tp": dtp, "cu": cu, "cd": cd,
                "cc": round(long_cc, 6), "ema200": round(ema, 4),
                "vol_ratio": round(vr, 2), "strategy": "mr",
                "bar_idx": idx}
    return None


def compute_score(sig):
    """均值回归评分: 低ADX加分(震荡市适合均值回归)，高ADX不加分"""
    score = 0.0
    adx = sig["adx"]
    if 10 <= adx <= 20: score += 3.0     # 最佳震荡区间
    elif 0 < adx < 10: score += 1.0       # 太安静，但也可交易
    elif 20 < adx <= 25: score += 1.5     # 轻微趋势，勉强可做
    e = sig["entry"]; rr = abs(sig["tp"] - e) / max(abs(e - sig["sl"]), 1e-9)
    if rr >= 2.0: score += 2.0
    elif rr >= 1.5: score += 1.0
    if sig["dynamic_tp"]: score += 1.0
    return round(min(score, 10.0), 2)


# ═══════════════════════ 退出判定 ═══════════════════════
def check_exit(pos, bar_high, bar_low, bar_open):
    """检查是否触发 SL/TP/TIMEOUT"""
    s = pos["side"]; e = pos["entry"]; sl = pos["sl"]; tp = pos["tp"]
    if s == "short":
        if bar_low <= tp and bar_high >= sl:
            return ("SL", sl) if (bar_open >= e) else ("TP", tp)
        if bar_low <= tp: return "TP", tp
        if bar_high >= sl: return "SL", sl
    else:
        if bar_high >= tp and bar_low <= sl:
            return ("SL", sl) if (bar_open <= e) else ("TP", tp)
        if bar_high >= tp: return "TP", tp
        if bar_low <= sl: return "SL", sl
    if pos.get("bars_held", 0) >= MAX_HOLD:
        return "TIMEOUT", (bar_high + bar_low) / 2
    return None, None


def update_trail(pos, bar_high, bar_low):
    """3段追踪止损"""
    if not TRAIL: return pos
    atr = pos.get("atr", 0); e = pos["entry"]; s = pos["side"]
    if atr <= 0: return pos
    if s == "short":
        fpa = (e - bar_low) / atr
        if fpa >= T_BE:
            ns = e
            if fpa >= T_LK: ns = min(ns, bar_low + 0.3 * atr)
            if fpa >= T_DY: ns = min(ns, bar_low + T_DD * atr)
            if ns < pos["sl"]:
                pos = dict(pos); pos["sl"] = round(ns, 8); pos["trailing_active"] = True
    else:
        fpa = (bar_high - e) / atr
        if fpa >= T_BE:
            ns = e
            if fpa >= T_LK: ns = max(ns, bar_high - 0.3 * atr)
            if fpa >= T_DY: ns = max(ns, bar_high - T_DD * atr)
            if ns > pos["sl"]:
                pos = dict(pos); pos["sl"] = round(ns, 8); pos["trailing_active"] = True
    return pos


# ═══════════════════════ 风控 ═══════════════════════
class DK:
    """动态Kelly仓位管理"""
    def __init__(self):
        self._w = deque(maxlen=WRG_W)
        self._p = deque(maxlen=WRG_W)

    def record(self, pnl):
        self._w.append(1 if pnl > 0 else 0)
        self._p.append(pnl)

    def frac(self):
        if len(self._w) < K_MIN: return K_FRAC
        wr = sum(self._w) / len(self._w)
        if wr >= 0.80: return 0.45
        if wr >= 0.70: return 0.35
        if wr >= 0.60: return 0.30
        return K_FRAC

    def risk(self, eq, sp):
        if len(self._w) < K_MIN: return eq * RISK
        wr = sum(self._w) / len(self._w)
        w = [p for p in self._p if p > 0]; l = [p for p in self._p if p <= 0]
        if not w or not l: return eq * RISK
        aw = sum(w) / len(w); al = abs(sum(l) / len(l))
        if al < 1e-9: return eq * RISK
        k = wr - (1 - wr) / (aw / al)
        return eq * max(0.005, min(K_MAX, k * self.frac())) / max(sp, 0.001)


class WRG:
    """WRGuard"""
    def __init__(self):
        self._r = deque(maxlen=WRG_W)
        self._a = False; self._p = False

    def record(self, win):
        self._r.append(win)
        n = len(self._r)
        if n < WRG_WARMUP: return  # 热身期不触发暂停
        wr = sum(self._r) / n if n > 0 else 1
        if not self._p and wr < WRG_P:
            self._p = True
        elif self._p and wr >= WRG_MIN:
            self._p = False
        if not self._a and wr < WRG_MIN:
            self._a = True
        elif self._a and wr >= WRG_B:
            self._a = False

    @property
    def active(self): return self._a
    @property
    def paused(self): return self._p
    @property
    def min_rr(self): return WRG_RR if self._a else MIN_RR


class SWT:
    """品种权重"""
    def __init__(self, symbols):
        self._sp = {s: deque(maxlen=30) for s in symbols}

    def record(self, sym, pnl):
        if sym in self._sp: self._sp[sym].append(pnl)

    def get(self):
        scores = {}
        for s, pnls in self._sp.items():
            if len(pnls) < 5:
                scores[s] = 1.0 / len(self._sp)
            else:
                a = np.array(list(pnls)); mu = np.mean(a)
                std = np.std(a, ddof=1)
                scores[s] = max(0.02, mu / std if std > 0 else -1.0)
        t = sum(scores.values())
        return {s: v / t for s, v in scores.items()} if t > 0 else {s: 1.0 / len(self._sp) for s in self._sp}

    def order(self):
        return sorted(self._sp.keys(), key=lambda s: self.get().get(s, 0), reverse=True)


class CORR:
    """相关性过滤"""
    def __init__(self, pos):
        self._p = pos

    def allow(self, sym, side):
        if sym not in CORR_G: return True
        return sum(1 for s, p in self._p.items()
                   if s in CORR_G and p["side"] == side) < CORR_MAX


# ═══════════════════════ 回放引擎 ═══════════════════════
def load_data(symbols=None):
    """加载CSV数据 + 计算指标"""
    if symbols is None:
        csv_files = sorted(DATA_DIR.glob("*_USDT_15m_180d.csv"))
        symbols = [f.stem.replace("_15m_180d", "") for f in csv_files]
    else:
        csv_files = [DATA_DIR / f"{s}_15m_180d.csv" for s in symbols]

    data = {}
    for s, f in zip(symbols, csv_files):
        if not f.exists():
            logger.warning(f"跳过 {s}: 文件不存在 {f}")
            continue
        df = pd.read_csv(f)
        for c in ["open", "high", "low", "close", "vol"]:
            df[c] = df[c].astype(float)
        df = compute_indicators(df)
        data[s] = df
        nbars = len(df) - df["atr"].isna().sum()
        logger.info(f"  {s}: {len(df)} bars, {nbars} 有效bar")
    return data


def run_replay(data, syms_active, quick=False):
    """
    主回放循环：逐K线推进，模拟交易
    """
    # 找到所有数据的共同时间范围
    all_idxs = {s: 0 for s in syms_active}
    min_len = min(len(data[s]) for s in syms_active)

    # 状态初始化
    equity = INIT_EQ; peak_equity = INIT_EQ; max_dd = 0.0
    positions = {}; trades = []
    total_trades = 0; wins = 0; losses = 0
    kelly = DK(); wrg = WRG(); sw = SWT(syms_active)

    # 品种级别的冷却和最后信号时间
    cd = {}  # cooldown per symbol
    lb = {}  # last bar_ts per symbol (防重复信号)

    returns = []  # 用于计算Sharpe
    daily_pnl = {}  # 日度统计

    total_signals = 0
    skipped_score = 0; skipped_rr = 0; skipped_wrg = 0
    skipped_corr = 0; skipped_cd = 0; skipped_lb = 0

    start_time = time.time()
    last_progress = 0

    for bar_idx in range(220, min_len):  # 从220开始（暖启动）
        # 进度
        pct = int((bar_idx - 220) / (min_len - 220) * 100)
        if pct >= last_progress + 10:
            elapsed = time.time() - start_time
            speed = (bar_idx - 220) / max(elapsed, 1)
            logger.info(f"  进度 {pct}% | bar {bar_idx}/{min_len} | {speed:.0f} bar/s | "
                        f"交易 {total_trades} | WR {wins/max(total_trades,1)*100:.0f}% | "
                        f"净值 ${equity:.2f}")
            last_progress = (pct // 10) * 10

        # ── 检查持仓退出 ──
        to_remove = []
        for sym in list(positions.keys()):
            pos = positions[sym]
            pos["bars_held"] = pos.get("bars_held", 0) + 1
            df_sym = data[sym]
            row = df_sym.iloc[bar_idx]
            h2, lo, op = float(row["high"]), float(row["low"]), float(row["open"])

            pos = update_trail(pos, h2, lo)
            positions[sym] = pos
            res, ex_price = check_exit(pos, h2, lo, op)
            if res is None: continue

            qty = pos["qty"]
            raw = ((pos["entry"] - ex_price) * qty if pos["side"] == "short"
                   else (ex_price - pos["entry"]) * qty)
            fee = (pos["entry"] + ex_price) * qty * FEE
            net = raw - fee

            equity += net; total_trades += 1
            if net > 0: wins += 1
            else: losses += 1
            if equity > peak_equity: peak_equity = equity
            dd = (peak_equity - equity) / peak_equity * 100
            if dd > max_dd: max_dd = dd

            kelly.record(net); wrg.record(net > 0)
            sw.record(sym, net)

            # 日度统计
            ts = row.get("ts", "")
            day = str(ts)[:10] if ts else f"day{bar_idx}"
            if day not in daily_pnl: daily_pnl[day] = {"pnl": 0.0, "trades": 0, "wins": 0}
            daily_pnl[day]["pnl"] += net; daily_pnl[day]["trades"] += 1
            if net > 0: daily_pnl[day]["wins"] += 1

            to_remove.append(sym)

            rec = {"no": total_trades, "sym": sym, "side": pos["side"],
                   "entry": round(pos["entry"], 8),
                   "exit": round(ex_price, 8), "qty": round(qty, 6),
                   "notional": round(pos.get("notional", 0), 4),
                   "result": res, "net_pnl": round(net, 6),
                   "fee": round(fee, 6), "bars_held": pos["bars_held"],
                   "strategy": pos.get("strategy", "mr"),
                   "bar_open": bar_idx - pos["bars_held"],
                   "bar_close": bar_idx}
            trades.append(rec)
            if not quick:
                e = "✅" if net > 0 else "❌"
                logger.debug(f"{e} #{total_trades:3d} {sym} {pos['side'].upper()} {res} "
                           f"PnL={net:+.5f}U WR={wins/max(total_trades,1)*100:.1f}%")

        for sym in to_remove:
            del positions[sym]

        # ── 生成信号 ──
        if len(positions) < MAX_POS and not wrg.paused:
            corr = CORR(positions)
            ordered = sw.order()
            for sym in ordered:
                if sym not in syms_active: continue
                if sym in positions: continue
                if len(positions) >= MAX_POS: break

                # 冷却检查
                if cd.get(sym, 0) > bar_idx: continue

                df_sym = data[sym]
                sc = SC.get(sym, SC.get("BTCUSDT", {}))
                sig = mr_signal(sym, df_sym, sc, bar_idx)
                if sig is None: continue
                total_signals += 1

                # 防重复
                if lb.get(sym) == bar_idx:
                    skipped_lb += 1; continue
                score = compute_score(sig)
                if score < SIG_MIN:
                    skipped_score += 1; continue
                entry = sig["entry"]
                rr = abs(sig["tp"] - entry) / max(abs(entry - sig["sl"]), 1e-9)
                if rr < wrg.min_rr:
                    skipped_rr += 1; continue
                if not corr.allow(sym, sig["side"]):
                    skipped_corr += 1; continue

                # 仓位计算
                sp = abs(sig["entry"] - sig["sl"]) / sig["entry"]
                ru = kelly.risk(equity, sp)
                sd = abs(sig["entry"] - sig["sl"])
                if sd <= 0: continue
                qty = ru / sd; notional = qty * sig["entry"]
                if notional < MIN_NOTIONAL: continue
                if notional > equity * 0.3:
                    qty = equity * 0.3 / sig["entry"]
                    notional = qty * sig["entry"]

                lb[sym] = bar_idx
                cd[sym] = bar_idx + COOLDOWN

                positions[sym] = {
                    "side": sig["side"], "entry": sig["entry"],
                    "sl": sig["sl"], "tp": sig["tp"],
                    "qty": qty, "notional": round(notional, 4),
                    "score": score, "adx": sig["adx"],
                    "atr": sig["atr"], "rsi": sig.get("rsi", 50),
                    "tp_s": sig["tp_s"], "dynamic_tp": sig["dynamic_tp"],
                    "cu": sig["cu"], "cd": sig["cd"], "cc": sig["cc"],
                    "bars_held": 0, "trailing_active": False,
                    "strategy": sig.get("strategy", "mr")}

    # ── 统计 ──
    elapsed = time.time() - start_time
    wr = wins / max(total_trades, 1)
    pf = (sum(t["net_pnl"] for t in trades if t["net_pnl"] > 0) /
          abs(sum(t["net_pnl"] for t in trades if t["net_pnl"] < 0))) if any(t["net_pnl"] < 0 for t in trades) else float("inf")
    total_ret = (equity - INIT_EQ) / INIT_EQ * 100

    # Sharpe
    returns_list = [t["net_pnl"] for t in trades]
    sharpe = 0.0
    if len(returns_list) >= 5:
        mu = np.mean(returns_list); std = np.std(returns_list, ddof=1)
        sharpe = mu / std if std > 0 else 0

    # 按品种统计
    sym_stats = {}
    for t in trades:
        s = t["sym"]
        if s not in sym_stats:
            sym_stats[s] = {"trades": 0, "wins": 0, "pnl": 0.0, "side_long": 0, "side_short": 0}
        sym_stats[s]["trades"] += 1
        if t["net_pnl"] > 0: sym_stats[s]["wins"] += 1
        sym_stats[s]["pnl"] += t["net_pnl"]
        if t["side"] == "long": sym_stats[s]["side_long"] += 1
        else: sym_stats[s]["side_short"] += 1

    # 按结果统计
    tp_count = sum(1 for t in trades if t["result"] == "TP")
    sl_count = sum(1 for t in trades if t["result"] == "SL")
    to_count = sum(1 for t in trades if t["result"] == "TIMEOUT")

    stats = {
        "total_trades": total_trades,
        "wins": wins, "losses": losses,
        "wr": wr * 100,
        "profit_factor": round(pf, 3),
        "sharpe": round(sharpe, 3),
        "total_return_pct": round(total_ret, 2),
        "final_equity": round(equity, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "tp_count": tp_count, "sl_count": sl_count, "timeout_count": to_count,
        "avg_win": round(sum(t["net_pnl"] for t in trades if t["net_pnl"] > 0) / max(wins, 1), 6),
        "avg_loss": round(abs(sum(t["net_pnl"] for t in trades if t["net_pnl"] < 0)) / max(losses, 1), 6),
        "avg_bars": round(sum(t["bars_held"] for t in trades) / max(total_trades, 1), 1),
        "total_signals": total_signals,
        "skipped": {"score": skipped_score, "rr": skipped_rr,
                     "wrg": skipped_wrg, "corr": skipped_corr,
                     "cd": skipped_cd, "lb": skipped_lb},
        "sym_stats": sym_stats,
        "speed_bps": round((bar_idx - 220) / max(elapsed, 1), 0),
        "elapsed_sec": round(elapsed, 1)
    }

    # 按品种展示
    logger.info(f"\n{'═'*70}")
    logger.info(f" 白夜 v9.0 离线回放报告")
    logger.info(f"{'═'*70}")
    logger.info(f" 品种数: {len(syms_active)} | 总K线: {min_len} | Numba: {'✅' if HAS_NUMBA else '❌'}")
    logger.info(f"{'─'*70}")
    logger.info(f" 📊 总交易: {total_trades} 笔")
    logger.info(f" 🎯 胜率 (WR): {wr*100:.1f}%")
    logger.info(f" 📈 盈亏比 (PF): {pf:.2f}")
    logger.info(f" 📐 夏普比率: {sharpe:.3f}")
    logger.info(f" 💰 总收益: {total_ret:+.2f}%")
    logger.info(f" 📉 最大回撤: {max_dd:.2f}%")
    logger.info(f" 💵 最终净值: ${equity:.2f}")
    logger.info(f" ⏱️  耗时: {elapsed:.1f}s | {stats['speed_bps']:.0f} bar/s")
    logger.info(f" 🔔 信号: {total_signals} | 过滤: score={skipped_score} rr={skipped_rr} "
                f"corr={skipped_corr} cd={skipped_cd} lb={skipped_lb}")
    logger.info(f" 🏷️  TP={tp_count} SL={sl_count} TIMEOUT={to_count}")
    logger.info(f"{'─'*70}")
    logger.info(f" 品种明细:")
    logger.info(f" {'品种':<12s} {'笔数':>5s} {'胜率':>7s} {'PnL':>10s} {'LONG':>5s} {'SHORT':>6s}")
    logger.info(f" {'─'*50}")
    for s in sorted(sym_stats.keys()):
        st = sym_stats[s]
        wr_s = st["wins"] / max(st["trades"], 1) * 100
        logger.info(f" {s:<12s} {st['trades']:>5d} {wr_s:>6.1f}% {st['pnl']:>9.4f}U "
                    f"{st['side_long']:>5d} {st['side_short']:>6d}")
    logger.info(f"{'═'*70}")

    return trades, stats


def main():
    parser = argparse.ArgumentParser(description="白夜交易系统离线回放")
    parser.add_argument("--quick", action="store_true", help="快速摘要模式")
    parser.add_argument("--fast", action="store_true", help="仅SOL+BTC快速调试")
    parser.add_argument("--symbols", type=str, default="",
                        help="指定品种，逗号分隔 (如: SOLUSDT,BTCUSDT)")
    args = parser.parse_args()

    tag = "241x Numba" if HAS_NUMBA else "Python"
    logger.info(f"白夜 v9.0 离线回放 ({tag}) 启动...")

    # 确定分析品种
    if args.fast:
        syms = ["SOLUSDT", "BTCUSDT"]
    elif args.symbols:
        syms = [s.strip() for s in args.symbols.split(",")]
    else:
        # 只分析有CSV数据的品种
        syms = ["TONUSDT", "SUIUSDT", "POLUSDT", "DOTUSDT",
                "BTCUSDT", "SOLUSDT", "BNBUSDT", "LINKUSDT"]
        # 注: ADA/AVAX/NEAR/UNI/AAVE/OP/ARB/TIA/WIF 无CSV数据

    logger.info(f"加载数据: {len(syms)} 个品种")
    data = load_data(syms)

    # 过滤出实际加载成功的
    syms_active = [s for s in syms if s in data]
    logger.info(f"有效品种: {len(syms_active)}/{len(syms)}")

    if not syms_active:
        logger.error("无可用数据，退出")
        return

    # 运行回放
    logger.info(f"\n开始回放 (Numba: {'✅' if HAS_NUMBA else '❌ Python fallback'})")
    trades, stats = run_replay(data, syms_active, quick=args.quick)

    # 保存结果
    if not args.quick and trades:
        trade_file = _B / "data" / "trades_v90.jsonl"
        with open(trade_file, "w", encoding="utf-8") as f:
            for t in trades:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
        logger.info(f"\n✅ 交易记录已保存: {trade_file}")

        # 保存状态
        state_file = _B / "data" / "state_v90.json"
        state = {
            "equity": stats["final_equity"],
            "peak_equity": stats["final_equity"] + stats["final_equity"] * stats["max_drawdown_pct"] / 100,
            "max_drawdown": stats["max_drawdown_pct"],
            "total_trades": stats["total_trades"],
            "wins": stats["wins"],
            "losses": stats["losses"],
            "total_pnl": stats["final_equity"] - INIT_EQ,
            "sharpe": {"returns": [t["net_pnl"] for t in trades], "value": stats["sharpe"]},
            "generated_at": datetime.now(timezone.utc).isoformat()
        }
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        logger.info(f"✅ 状态已保存: {state_file}")

    # 最后汇总
    logger.info(f"\n{'═'*70}")
    logger.info(f" 回放完成: {stats['total_trades']}笔 | WR {stats['wr']:.1f}% | "
                f"PF {stats['profit_factor']:.2f} | Sharpe {stats['sharpe']:.3f} | "
                f"收益 {stats['total_return_pct']:+.2f}%")
    logger.info(f"{'═'*70}\n")


if __name__ == "__main__":
    main()
