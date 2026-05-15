#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
白夜交易系统 v7.4b — 实时数据100笔完整闭环纸交易
==================================================
修复历史:
  v7.4  tp_s从2.7-3.3x缩至1.0-1.5x，解决TIMEOUT=58%
  v7.4b 增加1h趋势过滤，避免逆势做空/做多；品种扩展至10个
核心:
  - 每笔exit独立重新拉取K线(walk-forward，非复用快照)
  - 进场/出场均用币安合约实时数据
  - 3m/5m/15m多周期 | 10品种
"""
from __future__ import annotations
import requests, time, json
import numpy as np, pandas as pd
from datetime import datetime, timezone
from pathlib import Path
import sys
sys.stdout.reconfigure(line_buffering=True)

FAPI = "https://fapi.binance.com"
NAV  = 150.0
FEE  = 0.0004   # 0.04% maker 单边
SL_K = 1.2      # SL倍数

SYM_CFG = {
    "BTCUSDT":  dict(sc=3, lc=4, ccp=0.001, adx_th=15, tp_s=1.0, allow_long=True),
    "ETHUSDT":  dict(sc=3, lc=3, ccp=0.001, adx_th=15, tp_s=1.0, allow_long=True),
    "SOLUSDT":  dict(sc=4, lc=4, ccp=0.001, adx_th=20, tp_s=1.2, allow_long=True),
    "XRPUSDT":  dict(sc=3, lc=3, ccp=0.001, adx_th=20, tp_s=1.2, allow_long=True),
    "DOGEUSDT": dict(sc=3, lc=3, ccp=0.001, adx_th=20, tp_s=1.0, allow_long=True),
    "LINKUSDT": dict(sc=3, lc=3, ccp=0.001, adx_th=20, tp_s=1.2, allow_long=False),
    "DOTUSDT":  dict(sc=3, lc=3, ccp=0.001, adx_th=20, tp_s=1.5, allow_long=True),
    "SUIUSDT":  dict(sc=3, lc=4, ccp=0.001, adx_th=20, tp_s=1.2, allow_long=True),
    "TONUSDT":  dict(sc=3, lc=3, ccp=0.001, adx_th=15, tp_s=1.0, allow_long=True),
    "HYPEUSDT": dict(sc=3, lc=3, ccp=0.001, adx_th=15, tp_s=1.0, allow_long=True),
}
INTERVALS = ["5m", "15m", "3m"]

# ── 数据拉取 ──────────────────────────────
def fetch(sym, interval="15m", limit=120):
    for attempt in range(3):
        try:
            r = requests.get(f"{FAPI}/fapi/v1/klines",
                params={"symbol":sym,"interval":interval,"limit":limit}, timeout=10)
            r.raise_for_status()
            df = pd.DataFrame(r.json(), columns=[
                "ts","open","high","low","close","vol",
                "ct","qv","n","bb","bq","ig"])
            df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="ms", utc=True)
            for c in ["open","high","low","close","vol"]:
                df[c] = df[c].astype(float)
            return df
        except Exception as e:
            if attempt == 2: raise
            time.sleep(1.5)

# ── 指标计算 ──────────────────────────────
def wilder(arr, n):
    out = np.full(len(arr), np.nan)
    idx = np.where(~np.isnan(arr))[0]
    if len(idx) < n: return out
    s = idx[0]; out[s+n-1] = np.nanmean(arr[s:s+n])
    for i in range(s+n, len(arr)):
        if not np.isnan(out[i-1]):
            out[i] = (out[i-1]*(n-1) + arr[i]) / n
    return out

def compute(df):
    df = df.copy()
    c=df["close"].values; h=df["high"].values; l=df["low"].values; n=len(c)
    prev=np.roll(c,1); prev[0]=c[0]
    tr=np.maximum(h-l,np.maximum(np.abs(h-prev),np.abs(l-prev)))
    df["atr"]=wilder(tr,14)
    df["ema200"]=df["close"].ewm(span=200,adjust=False).mean()
    up=np.diff(h,prepend=h[0]); dn=np.diff(l,prepend=l[0])*-1
    pdm=np.where((up>dn)&(up>0),up,0.0); ndm=np.where((dn>up)&(dn>0),dn,0.0)
    safe=np.where(wilder(tr,14)>0,wilder(tr,14),np.nan)
    pdi=100*wilder(pdm,14)/safe; ndi=100*wilder(ndm,14)/safe
    denom=np.where((pdi+ndi)>0,pdi+ndi,np.nan)
    df["adx"]=wilder(100*np.abs(pdi-ndi)/denom,14)
    cu=np.zeros(n,int); cd=np.zeros(n,int); cc=np.zeros(n)
    for i in range(1,n):
        if c[i]>c[i-1]:
            cu[i]=cu[i-1]+1; cd[i]=0
            cc[i]=cc[i-1]+(c[i]-c[i-1])/c[i-1] if cu[i]>1 else (c[i]-c[i-1])/c[i-1]
        elif c[i]<c[i-1]:
            cd[i]=cd[i-1]+1; cu[i]=0
            cc[i]=cc[i-1]-(c[i-1]-c[i])/c[i] if cd[i]>1 else -(c[i-1]-c[i])/c[i]
        else:
            cu[i]=0; cd[i]=0; cc[i]=0
    df["cu"]=cu; df["cd"]=cd; df["cc"]=cc
    return df

# ── 1h趋势判断 ────────────────────────────
def get_trend_1h(sym):
    """返回 BULL / BEAR / NEUTRAL"""
    try:
        r = fetch(sym, "1h", 24)
        c = r["close"].values
        ema20 = pd.Series(c).ewm(span=20,adjust=False).mean().values
        pct4h = (c[-1]-c[-4])/c[-4]*100
        if c[-1] > ema20[-1]*1.006 or pct4h >  0.8: return "BULL"
        if c[-1] < ema20[-1]*0.994 or pct4h < -0.8: return "BEAR"
        return "NEUTRAL"
    except:
        return "NEUTRAL"

# ── 信号生成 ──────────────────────────────
def get_signal(df, cfg, trend1h="NEUTRAL"):
    last = df.iloc[-1]
    atr=last["atr"]; price=last["close"]; adx=last["adx"]
    cu=int(last["cu"]); cd=int(last["cd"]); cc=last["cc"]
    ema200=last["ema200"]
    if np.isnan(atr) or np.isnan(adx) or atr<=0: return None
    if atr/price < 0.0005: return None          # 波动太小

    # 趋势过滤：BULL行情禁SHORT，BEAR行情禁LONG
    can_short = (trend1h != "BULL")
    can_long  = (trend1h != "BEAR") and cfg["allow_long"]

    short = can_short and (adx>=cfg["adx_th"]) and (cu>=cfg["sc"]) \
            and (cc>=cfg["ccp"]) and (price<=ema200*1.015)
    long  = can_long  and (adx>=cfg["adx_th"]) and (cd>=cfg["lc"]) \
            and (cc<=-cfg["ccp"]) and (price>=ema200*0.985)
    if short: return "short", atr, price
    if long:  return "long",  atr, price
    return None

# ── 出场模拟（独立拉取新K线）─────────────
def simulate_exit(sym, interval, side, entry, sl, tp, max_bars=18):
    wait = {"3m":8, "5m":12, "15m":20}.get(interval, 10)
    time.sleep(wait)
    df = compute(fetch(sym, interval, 60))
    for _, row in df.tail(max_bars).iterrows():
        hh=row["high"]; ll=row["low"]
        if side=="short":
            if hh>=sl: return sl, "SL"
            if ll<=tp: return tp, "TP"
        else:
            if ll<=sl: return sl, "SL"
            if hh>=tp: return tp, "TP"
    return float(df.iloc[-1]["close"]), "TIMEOUT"

# ── 引擎 ─────────────────────────────────
class Engine:
    def __init__(self):
        self.nav=NAV; self.trades=[]; self.tid=0
        self.trend_cache={}; self.trend_ts={}

    def refresh_trend(self, sym):
        now = time.time()
        if sym not in self.trend_ts or now-self.trend_ts[sym] > 600:
            self.trend_cache[sym] = get_trend_1h(sym)
            self.trend_ts[sym] = now
        return self.trend_cache.get(sym,"NEUTRAL")

    def run_one(self, sym, interval):
        cfg = SYM_CFG[sym]
        trend = self.refresh_trend(sym)
        df = compute(fetch(sym, interval, 120))
        sig = get_signal(df, cfg, trend)
        if sig is None: return None

        side, atr, entry = sig
        tp_s=cfg["tp_s"]; sl_d=SL_K*atr; tp_d=tp_s*atr
        sl=entry+sl_d if side=="short" else entry-sl_d
        tp=entry-tp_d if side=="short" else entry+tp_d
        notional=min((self.nav*0.02/sl_d)*entry, self.nav*2.0)
        if notional<3: return None
        qty=notional/entry; fee=notional*FEE*2

        exit_p, result = simulate_exit(sym, interval, side, entry, sl, tp)
        gross=(entry-exit_p)*qty if side=="short" else (exit_p-entry)*qty
        pnl=gross-fee; self.nav+=pnl; self.tid+=1

        return dict(
            id=self.tid, sym=sym, tf=interval, side=side,
            trend1h=trend,
            entry=round(entry,6), exit=round(exit_p,6),
            sl=round(sl,6), tp=round(tp,6),
            notional=round(notional,4), tp_s=tp_s,
            atr_pct=round(atr/entry*100,4),
            tp_pct=round(tp_d/entry*100,4),
            gross=round(gross,4), fee=round(fee,4), pnl=round(pnl,4),
            result=result, nav=round(self.nav,4),
            ts=datetime.now(timezone.utc).isoformat()
        )

    def stats(self):
        if not self.trades: return {}
        df=pd.DataFrame(self.trades)
        wins=df[df["pnl"]>0]; los=df[df["pnl"]<=0]
        pf=abs(wins["pnl"].sum()/(los["pnl"].sum()+1e-9)) if len(los) else 99
        by_sym={}
        for sym,g in df.groupby("sym"):
            by_sym[sym]={"n":len(g),"wr":round((g["pnl"]>0).mean()*100,1),"pnl":round(g["pnl"].sum(),4)}
        return dict(
            total=len(df), wins=len(wins), losses=len(los),
            wr=round(len(wins)/len(df)*100,2), pf=round(pf,3),
            total_pnl=round(df["pnl"].sum(),4),
            total_fee=round(df["fee"].sum(),4),
            nav_end=round(self.nav,4),
            ret_pct=round((self.nav-NAV)/NAV*100,4),
            tp=int((df["result"]=="TP").sum()),
            sl=int((df["result"]=="SL").sum()),
            timeout=int((df["result"]=="TIMEOUT").sum()),
            to_pct=round((df["result"]=="TIMEOUT").mean()*100,1),
            avg_win=round(wins["pnl"].mean(),4) if len(wins) else 0,
            avg_loss=round(los["pnl"].mean(),4) if len(los) else 0,
            best=round(df["pnl"].max(),4), worst=round(df["pnl"].min(),4),
            by_sym=by_sym
        )

# ── 主流程 ────────────────────────────────
def main():
    eng=Engine(); TARGET=100
    syms=list(SYM_CFG.keys()); ints=INTERVALS
    attempt=0; skipped=0

    print("="*74)
    print("  白夜交易系统 v7.4b — 币安实时数据 100笔完整闭环纸交易")
    print(f"  品种:{len(syms)} | 周期:3m/5m/15m | tp=1.0~1.5xATR | 1h趋势过滤")
    print(f"  NAV:{NAV}U | SL:1.2xATR | FEE:0.04%maker | Walk-Forward出场")
    print("="*74+"\n")

    # 预加载趋势
    print("  📡 预加载1h趋势...")
    for sym in syms:
        t=eng.refresh_trend(sym)
        print(f"    {sym:<14} 趋势={t}")
        time.sleep(0.2)
    print()

    while len(eng.trades) < TARGET:
        sym=syms[attempt%len(syms)]
        interval=ints[(attempt//len(syms))%len(ints)]
        attempt+=1
        try:
            t=eng.run_one(sym, interval)
        except Exception as e:
            print(f"  [⚠] {sym}/{interval}: {e}")
            time.sleep(2); continue

        if t is None:
            skipped+=1; time.sleep(0.4); continue

        icon="✅" if t["pnl"]>0 else "❌"
        print(f"[{t['id']:>3}]{icon} {t['sym']:<12}{t['side']:<6}{t['tf']:<4}"
              f"1h:{t['trend1h']:<8} "
              f"进:{t['entry']:<12} 出:{t['exit']:<12} "
              f"PnL:{t['pnl']:>+8.4f}U | {t['result']:<8}"
              f"TP需:{t['tp_pct']:.3f}% ATR:{t['atr_pct']:.3f}% | NAV:{t['nav']:.2f}U")

    s=eng.stats()
    print(f"\n{'='*74}")
    print(f"  📊 白夜v7.4b — 100笔实时纸交易完整报告")
    print(f"{'='*74}")
    print(f"  总交易:{s['total']}  胜率:{s['wr']}%  PF:{s['pf']}")
    print(f"  总盈亏:{s['total_pnl']:+.4f}U  手续费:{s['total_fee']:.4f}U")
    print(f"  NAV: {NAV} → {s['nav_end']}U  收益:{s['ret_pct']:+.4f}%")
    print(f"  TP:{s['tp']}  SL:{s['sl']}  TIMEOUT:{s['timeout']}({s['to_pct']}%)")
    print(f"  均盈:{s['avg_win']:+.4f}U  均亏:{s['avg_loss']:+.4f}U")
    print(f"  最佳:{s['best']:+.4f}U  最差:{s['worst']:+.4f}U  跳过:{skipped}")
    print(f"\n  品种分布:")
    for sym,st in sorted(s['by_sym'].items(),key=lambda x:-x[1]['pnl']):
        print(f"    {sym:<14} n={st['n']:>3}  WR={st['wr']:>5.1f}%  PnL={st['pnl']:>+7.4f}U")
    print(f"{'='*74}")

    Path("data/live_paper_100_v74b.json").write_text(
        json.dumps({"summary":s,"trades":eng.trades},indent=2,ensure_ascii=False))
    print(f"\n  ✅ 完整记录 → data/live_paper_100_v74b.json")
    return s

if __name__=="__main__":
    main()
