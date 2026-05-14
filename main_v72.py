#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""白夜交易系统 v7.2 深度整合终版
融合: Wilder指标+cc信号 | 6层评分 | 3段追踪止损 | 弹性WRGuard | 资金费率过滤 | Kelly | 相关性控制
"""
from __future__ import annotations
import hashlib, json, logging, os, signal, sys, time, warnings
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")

try:
    import config as cfg
except ImportError:
    raise RuntimeError("未找到 config.py")

cfg.validate()
VERSION = f"v{cfg.VERSION}"

for _p in (cfg.LOG_FILE, cfg.STATE_FILE, cfg.TRADE_LOG, cfg.PID_FILE):
    Path(_p).parent.mkdir(parents=True, exist_ok=True)

# ── 日志 ──────────────────────────────────────────────────────
def _setup_logger():
    lg = logging.getLogger("baiye_v72")
    lg.setLevel(logging.INFO); lg.propagate = False
    if lg.handlers: return lg
    fmt = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(cfg.LOG_FILE, maxBytes=5<<20, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout); sh.setFormatter(fmt)
    lg.addHandler(fh); lg.addHandler(sh)
    return lg

logger = _setup_logger()
_running = True

def _on_signal(sig, _):
    global _running
    logger.warning(f"收到信号{sig}，准备退出...")
    _running = False

signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT,  _on_signal)

# ══════════════════════════════════════════════════════════════
# §1  技术指标（Wilder平滑，与回测完全一致）
# ══════════════════════════════════════════════════════════════
def _wilder(arr: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(arr), np.nan)
    v = np.where(~np.isnan(arr))[0]
    if len(v) < n: return out
    s = v[0]; out[s+n-1] = np.nanmean(arr[s:s+n])
    for i in range(s+n, len(arr)):
        if not np.isnan(out[i-1]):
            out[i] = out[i-1]*(n-1)/n + arr[i]/n
    return out

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    c, h, l = df["close"].values, df["high"].values, df["low"].values
    n = len(c)
    tr = np.maximum(h-l, np.maximum(np.abs(h-np.roll(c,1)), np.abs(l-np.roll(c,1))))
    tr[0] = h[0]-l[0]
    df["atr"] = _wilder(tr, 14)

    # EMA 9/21/55/200
    df["ema9"]   = df["close"].ewm(span=9,   adjust=False).mean()
    df["ema21"]  = df["close"].ewm(span=21,  adjust=False).mean()
    df["ema55"]  = df["close"].ewm(span=55,  adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()

    # ADX
    up = np.diff(h, prepend=h[0]); dn = np.diff(l, prepend=l[0])*-1
    pdm = np.where((up>dn)&(up>0), up, 0.0)
    ndm = np.where((dn>up)&(dn>0), dn, 0.0)
    atr14 = _wilder(tr, 14); safe = np.where(atr14>0, atr14, np.nan)
    pdi = 100*_wilder(pdm,14)/safe; ndi = 100*_wilder(ndm,14)/safe
    denom = np.where((pdi+ndi)>0, pdi+ndi, np.nan)
    df["adx"] = _wilder(100*np.abs(pdi-ndi)/denom, 14)
    df["pdi"] = pdi; df["ndi"] = ndi

    # RSI
    delta = np.diff(c, prepend=c[0])
    df["rsi"] = 100 - 100/(1 + _wilder(np.where(delta>0,delta,0.0),14) /
                           np.where(_wilder(np.where(delta<0,-delta,0.0),14)>0,
                                    _wilder(np.where(delta<0,-delta,0.0),14), 1e-9))

    # MACD
    ema12 = df["close"].ewm(span=cfg.MACD_FAST, adjust=False).mean()
    ema26 = df["close"].ewm(span=cfg.MACD_SLOW, adjust=False).mean()
    macd  = ema12 - ema26
    sig9  = macd.ewm(span=cfg.MACD_SIG, adjust=False).mean()
    df["macd_h"]  = (macd - sig9).values
    df["macd_hp"] = df["macd_h"].shift(1).values  # 前一根histogram

    # 成交量比（当前/20根均量）
    df["vol_ma"] = df["vol"].rolling(20, min_periods=5).mean()
    df["vol_r"]  = df["vol"] / df["vol_ma"].replace(0, np.nan)

    # 连涨(cu)/连跌(cd)/累涨跌(cc) — 方向切换重置修复
    cu = np.zeros(n, int); cd = np.zeros(n, int); cc = np.zeros(n)
    for i in range(1, n):
        chg = (c[i]-c[i-1])/c[i-1]
        if c[i] > c[i-1]:
            cu[i]=cu[i-1]+1; cd[i]=0
            cc[i] = chg if cd[i-1]>0 else cc[i-1]+chg
        elif c[i] < c[i-1]:
            cd[i]=cd[i-1]+1; cu[i]=0
            cc[i] = chg if cu[i-1]>0 else cc[i-1]+chg
        else:
            cu[i]=cu[i-1]; cd[i]=cd[i-1]; cc[i]=cc[i-1]
    df["cu"]=cu; df["cd"]=cd; df["cc"]=cc
    return df

# ══════════════════════════════════════════════════════════════
# §2  数据拉取
# ══════════════════════════════════════════════════════════════
def fetch_klines(symbol: str, interval: str, limit: int = cfg.KLINE_LIMIT) -> pd.DataFrame:
    for attempt in range(3):
        try:
            r = requests.get(f"{cfg.BINANCE_BASE_URL}/fapi/v1/klines",
                params={"symbol":symbol,"interval":interval,"limit":limit}, timeout=10)
            r.raise_for_status()
            df = pd.DataFrame(r.json(),
                columns=["ts","open","high","low","close","vol","ct","qv","tr","tbb","tbq","ign"])
            for col in ["open","high","low","close","vol"]:
                df[col] = df[col].astype(float)
            return df
        except Exception as e:
            if attempt < 2: time.sleep(2*(attempt+1))
            else: raise RuntimeError(f"[{symbol}/{interval}] 拉取失败: {e}")

def fetch_multi_tf(symbol: str) -> Dict[str, Optional[pd.DataFrame]]:
    out: Dict[str, Optional[pd.DataFrame]] = {}
    for tf in cfg.TIMEFRAMES:
        try: out[tf] = compute_indicators(fetch_klines(symbol, tf))
        except Exception as e:
            logger.warning(f"[{symbol}/{tf}] 数据异常: {e}"); out[tf] = None
    return out

# ══════════════════════════════════════════════════════════════
# §3  资金费率过滤器
# ══════════════════════════════════════════════════════════════
class FundingFilter:
    def __init__(self):
        self._rate: Dict[str, float] = {}
        self._next: Dict[str, float] = {}
        self._last_update = 0.0

    def refresh(self):
        now = time.time()
        if now - self._last_update < cfg.FUNDING_UPDATE_SEC: return
        try:
            r = requests.get(f"{cfg.BINANCE_BASE_URL}/fapi/v1/premiumIndex", timeout=10)
            r.raise_for_status()
            for item in r.json():
                sym = item.get("symbol","")
                if sym in cfg.SYMBOLS:
                    self._rate[sym] = float(item.get("lastFundingRate", 0))
                    self._next[sym] = float(item.get("nextFundingTime", 0))/1000
            self._last_update = now
            logger.info(f"[Funding] 费率更新完成，共{len(self._rate)}品种")
        except Exception as e:
            logger.warning(f"[Funding] 更新失败: {e}")

    def skip(self, symbol: str) -> bool:
        rate = abs(self._rate.get(symbol, 0.0))
        nxt  = self._next.get(symbol, 0.0)
        if rate >= cfg.FUNDING_SKIP_RATE:
            return True
        if nxt > 0 and (nxt - time.time()) < cfg.FUNDING_SKIP_WINDOW:
            return True
        return False


# ══════════════════════════════════════════════════════════════
# §4  6层信号评分引擎
# ══════════════════════════════════════════════════════════════
def _score_6layer(row, tf_data: dict, symbol: str, side: str) -> float:
    """6层信号评分，满分7.0，开仓要求≥SIGNAL_MIN_SCORE(3.2)"""
    lg = (side == "long")
    score = 0.0

    # L1 EMA趋势对齐 (0~1.0)
    e9,e21,e55 = float(row["ema9"]), float(row["ema21"]), float(row["ema55"])
    if   lg  and e9>e21>e55: score += 1.0
    elif not lg and e9<e21<e55: score += 1.0
    elif lg  and e9>e21:     score += 0.5
    elif not lg and e9<e21:  score += 0.5

    # L2 RSI区间 (0~1.0)
    rsi = float(row["rsi"]) if not np.isnan(row["rsi"]) else 50.0
    if   lg  and cfg.RSI_LONG_MIN  <= rsi <= cfg.RSI_LONG_MAX:  score += 1.0
    elif not lg and cfg.RSI_SHORT_MIN <= rsi <= cfg.RSI_SHORT_MAX: score += 1.0
    elif lg  and rsi < cfg.RSI_LONG_MIN:  score += 0.3
    elif not lg and rsi > cfg.RSI_SHORT_MAX: score += 0.3

    # L3 MACD histogram方向+加速 (0~1.0)
    mh  = float(row["macd_h"])  if not np.isnan(row["macd_h"])  else 0.0
    mhp = float(row["macd_hp"]) if not np.isnan(row["macd_hp"]) else 0.0
    if   lg  and mh>0 and mh>mhp: score += 1.0
    elif not lg and mh<0 and mh<mhp: score += 1.0
    elif lg  and mh>0:             score += 0.5
    elif not lg and mh<0:          score += 0.5

    # L4 成交量放大 (0~1.0)
    vr = float(row["vol_r"]) if not np.isnan(row["vol_r"]) else 1.0
    sc_obj = cfg.SYM_CFG.get(symbol, cfg.DEFAULT_SYM_CFG)
    if   vr >= sc_obj.tp_mult * 0.65: score += 1.0  # 成交量达tp_mult 65%以上
    elif vr >= 1.0:                    score += 0.4

    # L5 ADX强度两档 (0~1.0)
    adx = float(row["adx"]) if not np.isnan(row["adx"]) else 0.0
    if adx >= cfg.ADX_MIN:       score += 0.5
    if adx >= cfg.ADX_MIN * 1.5: score += 0.5

    # L6 多TF确认 (0~1.0): 5m确认+0.5, 1h过滤+0.5
    df5 = tf_data.get(cfg.TF_CONFIRM)
    if df5 is not None and len(df5) >= 30:
        r5 = df5.iloc[-2]
        e9_5=float(r5["ema9"]); e21_5=float(r5["ema21"])
        mh_5=float(r5["macd_h"]) if not np.isnan(r5["macd_h"]) else 0.0
        if (lg and e9_5>e21_5 and mh_5>0) or (not lg and e9_5<e21_5 and mh_5<0):
            score += 0.5

    df1h = tf_data.get(cfg.TF_FILTER)
    if df1h is not None and len(df1h) >= 30:
        r1h = df1h.iloc[-2]
        e21_h=float(r1h["ema21"]); e55_h=float(r1h["ema55"])
        if (lg and e21_h>e55_h) or (not lg and e21_h<e55_h):
            score += 0.5

    return round(min(score, 7.0), 2)


def check_raw_signal(symbol: str, df: pd.DataFrame, sc: object, side: str) -> Optional[dict]:
    """基于cc/cu/cd的原始信号（Wilder逻辑，v7.1验证）"""
    if df is None or len(df) < 220: return None
    row = df.iloc[-2]; cur = df.iloc[-1]
    adx = float(row["adx"]) if not np.isnan(row["adx"]) else 0.0
    atr = float(row["atr"]) if not np.isnan(row["atr"]) else 0.0
    if adx < sc.adx_th or atr <= 0: return None

    entry = float(cur["close"])
    cu=int(row["cu"]); cd=int(row["cd"]); cc=float(row["cc"])

    # 动态TP
    tp_m = sc.tp_mult * (cfg.DYNAMIC_TP_MULT if adx >= cfg.DYNAMIC_TP_ADX_TH else 1.0)

    if side == "short" and sc.allow_short and cu >= sc.sc and cc >= sc.ccp:
        return dict(side="short", entry=entry,
                    sl=round(entry + sc.sl_mult*atr, 8),
                    tp=round(entry - tp_m*atr, 8),
                    atr=atr, adx=adx, cu=cu, cd=cd, cc=cc,
                    rsi=float(row["rsi"]) if not np.isnan(row["rsi"]) else 50.0,
                    dynamic_tp=(adx>=cfg.DYNAMIC_TP_ADX_TH), bar_ts=int(row["ts"]))

    ema200 = float(row["ema200"]) if not np.isnan(row["ema200"]) else 0.0
    if side == "long" and sc.allow_long and cd >= sc.lc and cc <= -sc.ccp and entry > ema200:
        return dict(side="long", entry=entry,
                    sl=round(entry - sc.sl_mult*atr, 8),
                    tp=round(entry + tp_m*atr, 8),
                    atr=atr, adx=adx, cu=cu, cd=cd, cc=cc,
                    rsi=float(row["rsi"]) if not np.isnan(row["rsi"]) else 50.0,
                    dynamic_tp=(adx>=cfg.DYNAMIC_TP_ADX_TH), bar_ts=int(row["ts"]))
    return None


def scan_signal(symbol: str, tf_data: dict, wr_guard: "WRGuard") -> Optional[dict]:
    """扫描所有周期，返回评分最高的信号（通过评分+WRGuard过滤）"""
    sc = cfg.SYM_CFG.get(symbol, cfg.DEFAULT_SYM_CFG)
    best = None; best_score = -1.0

    for tf in [cfg.TF_PRIMARY, cfg.TF_CONFIRM, cfg.TF_FAST]:
        df = tf_data.get(tf)
        if df is None or len(df) < 220: continue
        row = df.iloc[-2]
        for side in ("short","long"):
            sig = check_raw_signal(symbol, df, sc, side)
            if sig is None: continue
            score = _score_6layer(row, tf_data, symbol, side)
            if score < wr_guard.min_score: continue
            rr = abs(sig["tp"]-sig["entry"]) / max(abs(sig["sl"]-sig["entry"]),1e-9)
            if rr < wr_guard.min_rr: continue
            if score > best_score:
                best_score = score; best = {**sig, "tf":tf, "score":score, "rr":round(rr,2)}

    return best


# ══════════════════════════════════════════════════════════════
# §5  3段追踪止损
# ══════════════════════════════════════════════════════════════
def trail_update(pos: dict, high: float, low: float) -> dict:
    """3阶段追踪止损：init→保本→锁利→动态追踪（只升级不降级）"""
    atr   = pos.get("atr", 0)
    entry = pos["entry"]; side = pos["side"]
    if atr <= 0: return pos

    px  = low  if side=="short" else high
    fav = ((entry-px)/atr if side=="short" else (px-entry)/atr)
    stage = pos.get("trail_stage","init")
    sl = pos["sl"]

    if fav >= cfg.TRAIL_DYNAMIC_ATR:
        new_sl = (px+cfg.TRAIL_DYNAMIC_DIST*atr if side=="short"
                  else px-cfg.TRAIL_DYNAMIC_DIST*atr)
        sl = (min(sl,new_sl) if side=="short" else max(sl,new_sl))
        stage = "trail"

    elif fav >= cfg.TRAIL_LOCK_ATR and stage in ("init","be"):
        sl = (entry-0.3*atr if side=="short" else entry+0.3*atr)
        stage = "lock"

    elif fav >= cfg.TRAIL_BREAKEVEN_ATR and stage == "init":
        sl = entry; stage = "be"

    if sl != pos["sl"] or stage != pos.get("trail_stage","init"):
        pos = dict(pos); pos["sl"] = round(sl,8); pos["trail_stage"] = stage
    return pos


def check_exit(pos: dict, high: float, low: float, open_: float=None) -> Tuple[Optional[str],Optional[float]]:
    """检查TP/SL/TIMEOUT，双触发时用open_判断先后"""
    side=pos["side"]; entry=pos["entry"]; sl=pos["sl"]; tp=pos["tp"]
    if side=="short":
        tp_hit=low<=tp; sl_hit=high>=sl
        if tp_hit and sl_hit:
            return ("SL",sl) if (open_ and open_>=entry) else ("TP",tp)
        if tp_hit: return "TP",tp
        if sl_hit: return "SL",sl
    else:
        tp_hit=high>=tp; sl_hit=low<=sl
        if tp_hit and sl_hit:
            return ("SL",sl) if (open_ and open_<=entry) else ("TP",tp)
        if tp_hit: return "TP",tp
        if sl_hit: return "SL",sl
    if pos.get("bars_held",0) >= cfg.MAX_HOLD_BARS:
        return "TIMEOUT",(high+low)/2
    return None,None


# ══════════════════════════════════════════════════════════════
# §6  弹性WRGuard
# ══════════════════════════════════════════════════════════════
class WRGuard:
    def __init__(self):
        self._buf: deque = deque(maxlen=cfg.WR_GUARD_WINDOW)

    def record(self, pnl: float):
        self._buf.append(1.0 if pnl>0 else 0.0)

    @property
    def wr(self) -> float:
        return sum(self._buf)/max(len(self._buf),1)

    @property
    def paused(self) -> bool:
        return len(self._buf)>=5 and self.wr < cfg.WR_GUARD_PAUSE_WR

    @property
    def min_score(self) -> float:
        if len(self._buf)<10: return cfg.SIGNAL_MIN_SCORE
        w = self.wr
        if w < cfg.WR_GUARD_MIN_WR:   return cfg.SIGNAL_MIN_SCORE * 1.4
        if w > cfg.WR_GUARD_BOOST_WR: return cfg.SIGNAL_MIN_SCORE * 0.8
        return cfg.SIGNAL_MIN_SCORE

    @property
    def min_rr(self) -> float:
        return (cfg.WR_GUARD_MIN_RR
                if len(self._buf)>=10 and self.wr < cfg.WR_GUARD_MIN_WR
                else cfg.MIN_RR_RATIO)

    def status(self) -> str:
        n=len(self._buf)
        if not n: return "冷启动"
        w=self.wr
        if self.paused: return f"⛔WR={w:.0%}({n})暂停开仓"
        if w < cfg.WR_GUARD_MIN_WR: return f"⚠️WR={w:.0%}({n})高RR模式"
        if w > cfg.WR_GUARD_BOOST_WR: return f"✅WR={w:.0%}({n})宽松模式"
        return f"✅WR={w:.0%}({n})正常"


# ══════════════════════════════════════════════════════════════
# §7  Kelly仓位
# ══════════════════════════════════════════════════════════════
class KellySizer:
    def __init__(self):
        self._wins: deque = deque(maxlen=cfg.WR_GUARD_WINDOW)
        self._pnls: deque = deque(maxlen=cfg.WR_GUARD_WINDOW)

    def record(self, pnl: float):
        self._wins.append(1 if pnl>0 else 0)
        self._pnls.append(pnl)

    def risk_amount(self, equity: float, sl_pct: float) -> float:
        denom = max(sl_pct, 0.001)
        if not cfg.KELLY_ENABLED or len(self._wins)<cfg.KELLY_MIN_TRADES:
            return equity*cfg.RISK_PCT
        wr = sum(self._wins)/len(self._wins)
        wins  = [p for p in self._pnls if p>0]
        loses = [p for p in self._pnls if p<=0]
        if not wins or not loses: return equity*cfg.RISK_PCT
        aw = sum(wins)/len(wins); al = abs(sum(loses)/len(loses))
        if al<1e-9: return equity*cfg.RISK_PCT
        k = wr - (1-wr)/(aw/al)
        r = max(0.005, min(cfg.KELLY_MAX_RISK, k*cfg.KELLY_FRACTION))
        return equity*r/denom


# ══════════════════════════════════════════════════════════════
# §8  相关性过滤
# ══════════════════════════════════════════════════════════════
class CorrFilter:
    def __init__(self, positions: dict):
        self._pos = positions

    def allow(self, symbol: str, side: str) -> bool:
        if symbol not in cfg.HIGH_CORR_GROUP: return True
        n = sum(1 for s,p in self._pos.items()
                if s in cfg.HIGH_CORR_GROUP and p["side"]==side)
        return n < cfg.MAX_CORR_SAME_SIDE


# ══════════════════════════════════════════════════════════════
# §9  状态持久化（CRC校验+原子写）
# ══════════════════════════════════════════════════════════════
def _crc(data: dict) -> str:
    raw = {k:v for k,v in data.items() if k!="_crc"}
    return hashlib.md5(json.dumps(raw,sort_keys=True).encode()).hexdigest()

def _default_state() -> dict:
    return dict(positions={}, equity=cfg.INITIAL_EQUITY,
                peak_equity=cfg.INITIAL_EQUITY, max_drawdown=0.0,
                day_loss=0.0, day_date="", total_trades=0,
                wins=0, losses=0, total_pnl=0.0, streak=0, daily_stats={})

def load_state() -> dict:
    p = Path(cfg.STATE_FILE)
    if p.exists():
        try:
            raw=json.loads(p.read_text()); stored=raw.pop("_crc","")
            if stored and stored!=_crc(raw):
                logger.warning("⚠️ state CRC不匹配，重置")
            else:
                s=_default_state(); s.update(raw); return s
        except Exception as e:
            logger.warning(f"⚠️ 状态加载失败({e})")
    return _default_state()

def save_state(s: dict):
    data=dict(s); data["_crc"]=_crc(data)
    tmp=Path(cfg.STATE_FILE).with_suffix(".tmp")
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2))
    tmp.replace(Path(cfg.STATE_FILE))

def append_trade(rec: dict):
    with open(cfg.TRADE_LOG,"a",encoding="utf-8") as f:
        f.write(json.dumps(rec,ensure_ascii=False)+"\n")

def load_trades() -> list:
    p=Path(cfg.TRADE_LOG)
    if not p.exists(): return []
    trades=[]
    for line in p.read_text(encoding="utf-8").splitlines():
        line=line.strip()
        if line:
            try: trades.append(json.loads(line))
            except: pass
    return trades

def update_stats(state: dict, net_pnl: float):
    state["streak"] = (max(state["streak"],0)+1 if net_pnl>0
                       else min(state["streak"],0)-1)
    if state["equity"] > state["peak_equity"]:
        state["peak_equity"] = state["equity"]
    dd=(state["peak_equity"]-state["equity"])/state["peak_equity"]*100
    if dd > state["max_drawdown"]: state["max_drawdown"]=dd
    today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today not in state["daily_stats"]:
        state["daily_stats"][today]={"pnl":0.0,"trades":0,"wins":0}
    state["daily_stats"][today]["pnl"]    += net_pnl
    state["daily_stats"][today]["trades"] += 1
    if net_pnl>0: state["daily_stats"][today]["wins"]+=1


# ══════════════════════════════════════════════════════════════
# §10  汇总打印
# ══════════════════════════════════════════════════════════════
def print_summary(state: dict, trades: list):
    total=state["wins"]+state["losses"]
    wr=state["wins"]/total*100 if total>0 else 0
    logger.info("═"*70)
    logger.info(f"  白夜交易系统 {VERSION} — 阶段汇总")
    logger.info(f"  完成:{total}笔 WR:{wr:.1f}% PnL:{state['total_pnl']:+.4f}U")
    logger.info(f"  权益:{cfg.INITIAL_EQUITY}U→{state['equity']:.4f}U "
                f"({(state['equity']/cfg.INITIAL_EQUITY-1)*100:+.2f}%)")
    logger.info(f"  最大回撤:{state['max_drawdown']:.1f}% 连胜/败:{state['streak']:+d}")
    if trades:
        w=[t["net_pnl"] for t in trades if t["net_pnl"]>0]
        l=[t["net_pnl"] for t in trades if t["net_pnl"]<0]
        aw=sum(w)/len(w) if w else 0; al=sum(l)/len(l) if l else 0
        pf=(sum(w)/abs(sum(l))) if l else 0
        logger.info(f"  均盈:{aw:+.4f}U 均亏:{al:+.4f}U PF={pf:.2f}")
        by_tf: Dict[str,list]={}
        for t in trades: by_tf.setdefault(t.get("tf","15m"),[]).append(t["net_pnl"])
        for tf,pnls in sorted(by_tf.items()):
            tw=sum(1 for p in pnls if p>0)
            logger.info(f"    [{tf}] {len(pnls)}笔 WR={tw/len(pnls)*100:.0f}% PnL={sum(pnls):+.3f}U")
    if state.get("daily_stats"):
        logger.info("  每日:")
        for date,ds in sorted(state["daily_stats"].items()):
            dwr=ds["wins"]/ds["trades"]*100 if ds["trades"]>0 else 0
            logger.info(f"    {date}: PnL={ds['pnl']:+.3f}U WR={dwr:.0f}%({ds['wins']}/{ds['trades']})")
    logger.info("═"*70)


# ══════════════════════════════════════════════════════════════
# §11  主循环
# ══════════════════════════════════════════════════════════════
def main():
    global _running
    Path(cfg.PID_FILE).write_text(str(os.getpid()))
    state   = load_state()
    trades  = load_trades()
    kelly   = KellySizer()
    wg      = WRGuard()
    funding = FundingFilter()

    # 预热Kelly/WRGuard
    for t in trades[-cfg.WR_GUARD_WINDOW:]:
        kelly.record(t["net_pnl"]); wg.record(t["net_pnl"])

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("day_date") != today:
        state["day_loss"]=0.0; state["day_date"]=today

    logger.info("═"*70)
    logger.info(f"  白夜交易系统 {VERSION} 启动  模式={cfg.RUN_MODE}")
    logger.info(f"  品种({len(cfg.SYMBOLS)}): {cfg.SYMBOLS}")
    logger.info(f"  周期:{cfg.TIMEFRAMES} 主={cfg.TF_PRIMARY} 确认={cfg.TF_CONFIRM} 过滤={cfg.TF_FILTER}")
    logger.info(f"  资金={cfg.INITIAL_EQUITY}U RISK={cfg.RISK_PCT*100:.0f}% FEE={cfg.FEE*10000:.1f}bps")
    logger.info(f"  Kelly={'ON' if cfg.KELLY_ENABLED else 'OFF'} "
                f"WRGuard={cfg.WR_GUARD_MIN_WR:.0%}/{cfg.WR_GUARD_BOOST_WR:.0%} "
                f"Funding过滤=ON TrailStop=3段")
    logger.info(f"  评分门槛={cfg.SIGNAL_MIN_SCORE} 最低RR={cfg.MIN_RR_RATIO}")
    logger.info(f"  历史:{state['wins']+state['losses']}笔 "
                f"权益={state['equity']:.2f}U 回撤={state['max_drawdown']:.1f}%")
    logger.info("═"*70)

    cooldown: Dict[str,int] = {}
    last_bar: Dict[str,int] = {}
    poll_count = 0

    while _running:
        try:
            poll_count += 1
            now   = datetime.now(timezone.utc)
            today = now.strftime("%Y-%m-%d")

            if state.get("day_date") != today:
                state["day_loss"]=0.0; state["day_date"]=today
                logger.info("━━ 新的一天，日熔断重置 ━━")

            # 日熔断
            if state["day_loss"] >= state["equity"]*cfg.DAILY_LOSS_PCT:
                logger.warning(f"⛔ 日熔断! 亏损={state['day_loss']:.4f}U 等待60s")
                time.sleep(60); continue

            # 资金费率刷新（每15min自动）
            funding.refresh()

            # 拉取所有品种多周期K线
            all_tf: Dict[str, Dict] = {sym: fetch_multi_tf(sym) for sym in cfg.SYMBOLS}

            # ── 检查持仓退出+追踪止损 ────────────────────────
            for sym in list(state["positions"].keys()):
                pos  = state["positions"][sym]
                tf   = pos.get("tf", cfg.TF_PRIMARY)
                df   = all_tf.get(sym,{}).get(tf)
                if df is None: continue
                cur  = df.iloc[-1]
                h,l,o = float(cur["high"]),float(cur["low"]),float(cur["open"])
                pos["bars_held"] = pos.get("bars_held",0)+1

                # 追踪止损（3段）
                pos = trail_update(pos, h, l)
                state["positions"][sym] = pos

                result, exit_px = check_exit(pos, h, l, o)
                if result is None: continue

                # 结算
                qty     = pos["qty"]
                raw_pnl = ((pos["entry"]-exit_px)*qty if pos["side"]=="short"
                           else (exit_px-pos["entry"])*qty)
                fee     = (pos["entry"]+exit_px)*qty*cfg.FEE
                net_pnl = raw_pnl - fee

                state["equity"]      += net_pnl
                state["total_pnl"]    = state.get("total_pnl",0.0)+net_pnl
                state["total_trades"] = state.get("total_trades",0)+1
                if net_pnl>0: state["wins"]=state.get("wins",0)+1
                else:
                    state["losses"]=state.get("losses",0)+1
                    state["day_loss"]+=abs(net_pnl)

                update_stats(state, net_pnl)
                kelly.record(net_pnl); wg.record(net_pnl)
                del state["positions"][sym]

                rec = dict(no=state["total_trades"], sym=sym, tf=tf,
                           side=pos["side"], entry=round(pos["entry"],8),
                           exit=round(exit_px,8), qty=round(qty,6),
                           result=result, net_pnl=round(net_pnl,6),
                           fee=round(fee,6), bars_held=pos.get("bars_held",0),
                           score=pos.get("score",0), adx=pos.get("adx"),
                           atr=pos.get("atr"), rr=pos.get("rr",0),
                           trail_stage=pos.get("trail_stage","init"),
                           dynamic_tp=pos.get("dynamic_tp",False),
                           open_time=pos["open_time"],
                           close_time=now.isoformat(),
                           equity_after=round(state["equity"],6))
                trades.append(rec); append_trade(rec); save_state(state)

                total=state["wins"]+state["losses"]
                wr=state["wins"]/total*100 if total>0 else 0
                icon="✅" if net_pnl>0 else "❌"
                stk=f"连{'胜' if state['streak']>0 else '败'}{abs(state['streak'])}"
                ts=pos.get("trail_stage","init")
                logger.info(
                    f"{icon} #{state['total_trades']:3d} [{tf}] {sym} "
                    f"{pos['side'].upper()} {result} trail={ts} | "
                    f"入={pos['entry']:.5g} 出={exit_px:.5g} PnL={net_pnl:+.5f}U | "
                    f"WR={wr:.1f}%({state['wins']}/{total}) "
                    f"净值={state['equity']:.4f}U {stk} {wg.status()}"
                )

            # ── 扫描新信号 ───────────────────────────────────
            if not wg.paused and len(state["positions"])<cfg.MAX_OPEN_POSITIONS:
                corr = CorrFilter(state["positions"])
                for sym in cfg.SYMBOLS:
                    if sym in state["positions"]: continue
                    if len(state["positions"])>=cfg.MAX_OPEN_POSITIONS: break
                    if funding.skip(sym):
                        logger.debug(f"[Funding] {sym} 跳过"); continue

                    ck = f"{sym}"
                    if poll_count - cooldown.get(ck,0) < cfg.COOLDOWN_BARS: continue

                    sig = scan_signal(sym, all_tf.get(sym,{}), wg)
                    if sig is None: continue

                    # 同根K线去重
                    lk = f"{sym}_{sig['tf']}"
                    if last_bar.get(lk)==sig.get("bar_ts"): continue

                    if not corr.allow(sym, sig["side"]): continue

                    # Kelly仓位
                    sl_pct = abs(sig["entry"]-sig["sl"])/max(sig["entry"],1e-9)
                    risk_u = kelly.risk_amount(state["equity"], sl_pct)
                    sl_d   = abs(sig["entry"]-sig["sl"])
                    if sl_d<=0: continue
                    qty      = risk_u/sl_d
                    notional = qty*sig["entry"]
                    if notional<cfg.MIN_NOTIONAL: continue
                    if notional>state["equity"]*0.5:
                        qty=state["equity"]*0.5/sig["entry"]; notional=qty*sig["entry"]

                    last_bar[lk]=sig.get("bar_ts"); cooldown[ck]=poll_count
                    state["positions"][sym] = dict(
                        side=sig["side"], tf=sig["tf"],
                        entry=sig["entry"], sl=sig["sl"], tp=sig["tp"],
                        qty=qty, notional=round(notional,4),
                        score=sig["score"], adx=sig["adx"], atr=sig["atr"],
                        rsi=sig.get("rsi",50), rr=sig["rr"],
                        dynamic_tp=sig["dynamic_tp"],
                        cu=sig["cu"], cd=sig["cd"], cc=sig["cc"],
                        bar_ts=sig.get("bar_ts"), bars_held=0,
                        trail_stage="init", open_time=now.isoformat(),
                    )
                    corr = CorrFilter(state["positions"])
                    save_state(state)

                    dtp=" 🚀DTP" if sig["dynamic_tp"] else ""
                    logger.info(
                        f"🔔 #{state['total_trades']+len(state['positions']):3d} "
                        f"[{sig['tf']}] {sym} {sig['side'].upper()}{dtp} "
                        f"评分={sig['score']:.1f} RR={sig['rr']:.2f} | "
                        f"入={sig['entry']:.5g} TP={sig['tp']:.5g} SL={sig['sl']:.5g} | "
                        f"ADX={sig['adx']:.0f} RSI={sig.get('rsi',0):.0f} "
                        f"notional={notional:.1f}U {wg.status()}"
                    )

            # ── 每轮状态摘要 ─────────────────────────────────
            total=state["wins"]+state["losses"]
            wr=state["wins"]/total*100 if total>0 else 0
            pos_str=(
                " | ".join(f"{s}({v['side'].upper()}/{v.get('tf','?')}"
                           f" sc={v.get('score',0):.1f} ts={v.get('trail_stage','init')})"
                           for s,v in state["positions"].items())
                or "无持仓"
            )
            logger.info(
                f"[{now.strftime('%H:%M')} #{poll_count}] "
                f"完成={total} WR={wr:.0f}% PnL={state['total_pnl']:+.3f}U "
                f"净值={state['equity']:.2f}U 回撤={state['max_drawdown']:.1f}% | {pos_str}"
            )

            # 无持仓显示各品种信号距离
            if not state["positions"]:
                for sym in cfg.SYMBOLS:
                    df=all_tf.get(sym,{}).get(cfg.TF_PRIMARY)
                    if df is None or len(df)<5: continue
                    row=df.iloc[-2]
                    sc=cfg.SYM_CFG.get(sym,cfg.DEFAULT_SYM_CFG)
                    adx=float(row["adx"]) if not np.isnan(row["adx"]) else 0
                    cu=int(row["cu"]); cd=int(row["cd"]); cc=float(row["cc"])*100
                    short_ok=(sc.allow_short and cu>=sc.sc and cc/100>=sc.ccp and adx>=sc.adx_th)
                    long_ok=(sc.allow_long and cd>=sc.lc and cc/100<=-sc.ccp and adx>=sc.adx_th)
                    s_s="✅SHORT" if short_ok else f"SHORT[cu{cu}/{sc.sc} cc{cc:.2f}%/{sc.ccp*100:.2f}% adx{adx:.0f}/{sc.adx_th}]"
                    l_s="LONG禁" if not sc.allow_long else ("✅LONG" if long_ok else
                        f"LONG[cd{cd}/{sc.lc} cc{cc:.2f}%/{-sc.ccp*100:.2f}% adx{adx:.0f}/{sc.adx_th}]")
                    logger.info(f"  {sym:10s} ADX={adx:4.0f} | {s_s} | {l_s}")

            if poll_count%20==0 and total>0:
                h=poll_count*cfg.POLL_SECS/3600
                logger.info(f"╪ 性能 #{poll_count} 运行{h:.1f}h | {total/max(h,0.01):.1f}笔/h | "
                            f"Kelly WR={kelly.risk_amount(state['equity'],0.01)/state['equity']*100:.1f}% | "
                            f"{wg.status()}")

            time.sleep(cfg.POLL_SECS)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"主循环异常(#{poll_count}): {e}", exc_info=True)
            time.sleep(30)

    logger.info("引擎退出，保存状态...")
    save_state(state)
    print_summary(state, trades)
    try: Path(cfg.PID_FILE).unlink(missing_ok=True)
    except: pass


if __name__ == "__main__":
    main()
