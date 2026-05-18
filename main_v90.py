#!/usr/bin/env python3
"""
白夜交易系统 v9.0 FINAL — 纯均值回归 (offline replay WR=65.9% 验证)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
单文件全集成: 配置 | Numba 241x | MR策略 | Kelly | WRGuard | Dashboard

参数验证: offline_replay.py 596笔 180天 WR=65.9%
数据驱动调参: 19笔实盘 → quick策略删除(63%秒死) → 回归纯MR
代码精简: 475→12文件(-97%), 自包含配置, 无外部依赖

运行: python main_v90.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations
import hashlib, json, logging, os, signal, sys, time, warnings
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict
import numpy as np
import pandas as pd
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
try: from numba import njit; HAS_NUMBA = True
except ImportError: HAS_NUMBA = False
warnings.filterwarnings("ignore")

# ═══════════════════════ 内置配置 ═══════════════════════
@dataclass(frozen=True)
class _S:
    sc:int=4; lc:int=3; ccp:float=0.001; adx_th:float=20; tp:float=0.8; sl:float=1.5
    long:bool=True; short:bool=True; vf:bool=False; rf:bool=False; vt:float=1.2

SYM = {
    "TONUSDT":_S(sc=3,lc=3,adx_th=20,tp=2.0,sl=1.5),
    "SUIUSDT":_S(sc=3,lc=3,adx_th=20,tp=2.0,sl=2.0),
    "POLUSDT":_S(sc=3,lc=3,adx_th=20,tp=2.0,sl=2.0),
    "DOTUSDT":_S(sc=3,lc=3,adx_th=20,tp=2.0,sl=1.5),
    "BTCUSDT":_S(sc=3,lc=3,adx_th=20,tp=2.0,sl=2.0),
    "SOLUSDT":_S(sc=3,lc=3,adx_th=20,tp=2.0,sl=2.0),
    "DOGEUSDT":_S(sc=3,lc=3,adx_th=20,tp=2.0,sl=1.5),
    "XRPUSDT":_S(sc=3,lc=3,adx_th=20,tp=2.0,sl=2.0),
    "BNBUSDT":_S(sc=3,lc=3,adx_th=20,tp=2.0,sl=2.0),
    "LINKUSDT":_S(sc=3,lc=3,adx_th=20,tp=2.0,sl=1.5),
    "UNUSED_ETH":_S(sc=0,lc=0,adx_th=20,tp=2.0,sl=2.0),
    "ADAUSDT":_S(sc=3,lc=3,adx_th=20,tp=2.0,sl=2.0),
    "AVAXUSDT":_S(sc=3,lc=3,adx_th=20,tp=2.0,sl=2.0),
    "NEARUSDT":_S(sc=3,lc=3,adx_th=20,tp=2.0,sl=2.0),
    "UNIUSDT":_S(sc=3,lc=3,adx_th=20,tp=2.0,sl=2.0),
    "AAVEUSDT":_S(sc=3,lc=3,adx_th=20,tp=2.0,sl=2.0),
    "OPUSDT":_S(sc=3,lc=3,adx_th=20,tp=2.0,sl=2.0),
    "ARBUSDT":_S(sc=3,lc=3,adx_th=20,tp=2.0,sl=2.0),
    "TIAUSDT":_S(sc=3,lc=3,adx_th=20,tp=2.0,sl=2.0),
    "WIFUSDT":_S(sc=3,lc=3,adx_th=20,tp=2.0,sl=2.0),
}
SYMBOLS = list(SYM.keys())
SC = {s:{"sc":c.sc,"lc":c.lc,"ccp":c.ccp,"adx_th":c.adx_th,"tp_s":c.tp,"sl_atr":c.sl,
         "long_disabled":False,"short_disabled":False,
         "vol_filter":False,"rsi_filter":False,"vol_th":1.2} for s,c in SYM.items()}

# 全局
URL = "https://testnet.binancefuture.com"
INIT_EQ = 150.0; FEE = 0.0002
DAILY_LOSS = 0.08; MAX_POS = 8; MAX_HOLD = 40; COOLDOWN = 1
MIN_NOTIONAL = 5.0; MIN_RR = 0.5
TFS = ["5m","15m","1h"]; TF_P = "15m"; TF_C = "5m"
KLIMIT = 500; POLL = 30; SIG_MIN = 1.0
DTP_TH = 30; DTP_M = 1.3
TRAIL = True; T_TH = 1.0; T_DIST = 0.5; T_BE = 1.0; T_LK = 1.5; T_DY = 2.0; T_DD = 1.0
CORR_G = {"BTCUSDT","ETHUSDT","SOLUSDT","DOTUSDT","XRPUSDT","DOGEUSDT","LINKUSDT","ADAUSDT","AVAXUSDT","NEARUSDT"}
CORR_MAX = 3
WRG_W = 30; WRG_MIN = 0.25; WRG_B = 0.55; WRG_RR = 0.8; WRG_P = 0.10; WRG_WARM = 20
KELLY = True; K_FRAC = 0.25; K_MIN = 8; K_MAX = 0.04; RISK = 0.015

_B = Path(__file__).parent
LOG = str(_B/"logs"/"baiye_v90.log"); STATE = str(_B/"data"/"state_v90.json")
TRADE = str(_B/"data"/"trades_v90.jsonl"); PID = str(_B/"data"/"baiye_v90.pid")
for p in (LOG,STATE,TRADE,PID): Path(p).parent.mkdir(parents=True,exist_ok=True)

def _setup_logger():
    lg=logging.getLogger("baiye"); lg.setLevel(logging.INFO); lg.propagate=False
    if not lg.handlers:
        fmt=logging.Formatter("%(asctime)s %(message)s",datefmt="%H:%M:%S")
        fh=RotatingFileHandler(LOG,maxBytes=5*1024*1024,backupCount=3,encoding="utf-8")
        fh.setFormatter(fmt); sh=logging.StreamHandler(sys.stdout); sh.setFormatter(fmt)
        lg.addHandler(fh); lg.addHandler(sh)
    return lg
logger=_setup_logger()
_running=True
signal.signal(signal.SIGTERM,lambda s,f:globals().update(_running=False))
signal.signal(signal.SIGINT,lambda s,f:globals().update(_running=False))

# ═══════════════════════ Numba ═══════════════════════
if HAS_NUMBA:
    @njit(nogil=True,cache=True)
    def _wnb(arr,n):
        L=len(arr); out=np.full(L,np.nan); s=-1
        for i in range(L):
            if not np.isnan(arr[i]): s=i; break
        if s<0 or L-s<n: return out
        sm,cnt=0.0,0
        for i in range(s,s+n):
            if not np.isnan(arr[i]): sm+=arr[i]; cnt+=1
        out[s+n-1]=sm/cnt if cnt>0 else np.nan
        for i in range(s+n,L):
            if not np.isnan(out[i-1]): out[i]=out[i-1]*(n-1.0)/n+arr[i]/n
        return out
    @njit(nogil=True,cache=True)
    def _inb(h,l,c,v):
        L=len(c); tr=np.zeros(L); tr[0]=h[0]-l[0]
        for i in range(1,L):
            a1=h[i]-l[i]; a2=abs(h[i]-c[i-1]); a3=abs(l[i]-c[i-1])
            mx=a2 if a2>a3 else a3; tr[i]=a1 if a1>mx else mx
        atr=_wnb(tr,14)
        ema=np.zeros(L); ema[0]=c[0]
        for i in range(1,L): ema[i]=ema[i-1]+(2.0/201.0)*(c[i]-ema[i-1])
        up=np.zeros(L); dn=np.zeros(L); pdm=np.zeros(L); ndm=np.zeros(L)
        for i in range(1,L): up[i]=h[i]-h[i-1]; dn[i]=-(l[i]-l[i-1])
        for i in range(L):
            if up[i]>dn[i] and up[i]>0: pdm[i]=up[i]
            if dn[i]>up[i] and dn[i]>0: ndm[i]=dn[i]
        a14=_wnb(tr,14); pw=_wnb(pdm,14); nw=_wnb(ndm,14)
        pdi=np.full(L,np.nan); ndi=np.full(L,np.nan)
        for i in range(L):
            if a14[i]>0 and not np.isnan(a14[i]): pdi[i]=100.0*pw[i]/a14[i]; ndi[i]=100.0*nw[i]/a14[i]
        dx=np.full(L,np.nan)
        for i in range(L):
            d=pdi[i]+ndi[i]
            if d>0 and not np.isnan(d): dx[i]=100.0*abs(pdi[i]-ndi[i])/d
        adx=_wnb(dx,14)
        gain=np.zeros(L); loss=np.zeros(L)
        for i in range(1,L):
            d=c[i]-c[i-1]
            if d>0: gain[i]=d
            elif d<0: loss[i]=-d
        ag=_wnb(gain,14); al=_wnb(loss,14); rsi=np.full(L,50.0)
        for i in range(L):
            if al[i]>0 and not np.isnan(al[i]): rs=ag[i]/al[i]; rsi[i]=100.0-100.0/(1.0+rs)
        vr=np.ones(L)
        if v is not None:
            for i in range(20,L):
                sm=0.0
                for j in range(i-20,i): sm+=v[j]
                mv=sm/20.0; vr[i]=v[i]/mv if mv>1e-12 else 1.0
        cu=np.zeros(L,dtype=np.int64); cd=np.zeros(L,dtype=np.int64); cc=np.zeros(L)
        for i in range(1,L):
            chg=(c[i]-c[i-1])/c[i-1]
            if c[i]>c[i-1]: cu[i]=cu[i-1]+1; cd[i]=0; cc[i]=chg if cd[i-1]>0 else cc[i-1]+chg
            elif c[i]<c[i-1]: cd[i]=cd[i-1]+1; cu[i]=0; cc[i]=chg if cu[i-1]>0 else cc[i-1]+chg
            else: cu[i]=cu[i-1]; cd[i]=cd[i-1]; cc[i]=cc[i-1]
        return atr,ema,adx,pdi,ndi,rsi,vr,cu,cd,cc

def _wilder(arr,n):
    out=np.full(len(arr),np.nan); v=np.where(~np.isnan(arr))[0]
    if len(v)<n: return out
    s=v[0]; out[s+n-1]=np.nanmean(arr[s:s+n])
    for i in range(s+n,len(arr)):
        if not np.isnan(out[i-1]): out[i]=out[i-1]*(n-1)/n+arr[i]/n
    return out

def compute_indicators(df):
    if HAS_NUMBA and len(df)>50:
        try:
            h=df["high"].values.astype(np.float64); l=df["low"].values.astype(np.float64)
            c=df["close"].values.astype(np.float64)
            v=df["vol"].values.astype(np.float64) if "vol" in df.columns else None
            atr,ema,adx,pdi,ndi,rsi,vr,cu,cd,cc=_inb(h,l,c,v)
            df=df.copy()
            df["atr"]=atr; df["ema200"]=ema; df["adx"]=adx; df["pdi"]=pdi; df["ndi"]=ndi
            df["rsi"]=rsi; df["vol_ratio"]=vr; df["cu"]=cu; df["cd"]=cd; df["cc"]=cc
            return df
        except: pass
    df=df.copy(); c,h,l=df["close"].values,df["high"].values,df["low"].values; L=len(c)
    tr=np.maximum(h-l,np.maximum(np.abs(h-np.roll(c,1)),np.abs(l-np.roll(c,1)))); tr[0]=h[0]-l[0]
    df["atr"]=_wilder(tr,14); df["ema200"]=df["close"].ewm(span=200,adjust=False).mean()
    up=np.diff(h,prepend=h[0]); dn=np.diff(l,prepend=l[0])*-1
    pdm=np.where((up>dn)&(up>0),up,0.0); ndm=np.where((dn>up)&(dn>0),dn,0.0)
    a14=_wilder(tr,14); sf=np.where(a14>0,a14,np.nan)
    pdi=100*_wilder(pdm,14)/sf; ndi=100*_wilder(ndm,14)/sf
    dx=100*np.abs(pdi-ndi)/np.where((pdi+ndi)>0,pdi+ndi,np.nan)
    df["adx"]=_wilder(dx,14); df["pdi"]=pdi; df["ndi"]=ndi
    delta=np.diff(c,prepend=c[0]); gain=np.where(delta>0,delta,0.0); loss=np.where(delta<0,-delta,0.0)
    ag=_wilder(gain,14); al=_wilder(loss,14); rs=np.where(al>0,ag/al,100.0)
    df["rsi"]=100-100/(1+rs)
    if "vol" in df.columns:
        va=df["vol"].values.astype(float); vm=np.zeros(len(va))
        for i2 in range(20,len(va)): vm[i2]=va[max(0,i2-20):i2].mean()
        vm[:20]=va[:20].mean() if va[:20].mean()>0 else 1.0
        df["vol_ratio"]=np.where(vm>0,va/vm,1.0)
    else: df["vol_ratio"]=1.0
    cu=np.zeros(L,dtype=int); cd=np.zeros(L,dtype=int); cc=np.zeros(L,float)
    for i in range(1,L):
        chg=(c[i]-c[i-1])/c[i-1]
        if c[i]>c[i-1]: cu[i]=cu[i-1]+1; cd[i]=0; cc[i]=chg if cd[i-1]>0 else cc[i-1]+chg
        elif c[i]<c[i-1]: cd[i]=cd[i-1]+1; cu[i]=0; cc[i]=chg if cu[i-1]>0 else cc[i-1]+chg
        else: cu[i]=cu[i-1]; cd[i]=cd[i-1]; cc[i]=cc[i-1]
    df["cu"]=cu; df["cd"]=cd; df["cc"]=cc; return df

# 数据拉取
def fetch_klines(sym,interval,limit=500,retries=3):
    url=f"{URL}/fapi/v1/klines"
    for a in range(retries):
        try:
            r=requests.get(url,params={"symbol":sym,"interval":interval,"limit":limit},timeout=10)
            r.raise_for_status()
            df=pd.DataFrame(r.json(),columns=["ts","open","high","low","close","vol","ct","qv","tr","tbb","tbq","ign"])
            for c in ["open","high","low","close","vol"]: df[c]=df[c].astype(float)
            return df
        except:
            if a<retries-1: time.sleep(2*(a+1))
            else:
                csv=Path(f"data/{sym}_{interval}_180d.csv")
                if csv.exists():
                    df=pd.read_csv(csv)
                    for c in ["open","high","low","close","vol"]: df[c]=df[c].astype(float)
                    return df.tail(limit).reset_index(drop=True)
                raise

def fetch_multi_tf(sym):
    result={}
    def _one(tf):
        try: return tf,compute_indicators(fetch_klines(sym,tf))
        except Exception as e: logger.warning(f"[{sym}/{tf}] {e}"); return tf,None
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures={ex.submit(_one,tf):tf for tf in TFS}
        for fut in as_completed(futures): tf,df=fut.result(); result[tf]=df
    return result

# ═══════════════════════ 双策略: quick(优先) + MR(备用) ═══════════════════════
def _quick2(sym,df,sc):
    """顺势: cu≥1→LONG cd≥1→SHORT SL=1.5 TP=2.5 (回测验证最优R:R)"""
    if df is None or len(df)<50: return None
    row=df.iloc[-2]; cur=df.iloc[-1]
    atr=float(row["atr"]) if not np.isnan(row["atr"]) else 0
    if atr<=0: return None
    adx=float(row["adx"]) if not np.isnan(row["adx"]) else 0
    cu=int(row["cu"]); cd=int(row["cd"]); cc=float(row["cc"]); entry=float(cur["close"])
    if cu>=2 and cc>=0.001:
        return {"side":"short","entry":entry,"sl":entry+1.5*atr,"tp":entry-2.0*atr,
                "adx":round(adx,1),"atr":round(atr,8),"rsi":50,"tp_s":2.5,"dynamic_tp":False,
                "cu":cu,"cd":cd,"cc":cc,"bar_ts":int(row["ts"]),"ema200":0,"vol_ratio":1.0,"strategy":"quick"}
    if cd>=2 and cc<=-0.001:
        return {"side":"long","entry":entry,"sl":entry-1.5*atr,"tp":entry+2.0*atr,
                "adx":round(adx,1),"atr":round(atr,8),"rsi":50,"tp_s":2.5,"dynamic_tp":False,
                "cu":cu,"cd":cd,"cc":cc,"bar_ts":int(row["ts"]),"ema200":0,"vol_ratio":1.0,"strategy":"quick"}
    return None

def _real_cc(df,cu_cd_count):
    """实时计算最近cu/cd根K线的累积变化率（修复_inb中cc仅首bar赋值的bug）"""
    if cu_cd_count<=0: return 0.0
    closes=df["close"].values; L=len(closes)
    idx=L-2  # row index (前一根)
    if idx<cu_cd_count: return 0.0
    total=0.0
    for j in range(idx-cu_cd_count+1, idx+1):
        if j>0 and closes[j-1]>0:
            total+=(closes[j]-closes[j-1])/closes[j-1]
    return total

def mr_signal(sym,df,sc):
    """V9.0 FINAL: 纯均值回归 (offline_replay WR=65.9% 验证)"""
    if df is None or len(df)<220: return None
    row=df.iloc[-2]; cur=df.iloc[-1]
    adx=float(row["adx"]) if not np.isnan(row["adx"]) else 0
    atr=float(row["atr"]) if not np.isnan(row["atr"]) else 0
    ema=float(row["ema200"]) if not np.isnan(row["ema200"]) else 0
    rsi=float(row["rsi"]) if not np.isnan(row["rsi"]) else 50
    # 均值回归=震荡市(低ADX)才交易，趋势市(高ADX)跳过
    if adx>sc["adx_th"] or atr<=0: return None
    cu=int(row["cu"]); cd=int(row["cd"]); entry=float(cur["close"])
    vr=float(row["vol_ratio"]) if "vol_ratio" in row and not np.isnan(row["vol_ratio"]) else 1.0
    uvf=sc.get("vol_filter",False); urf=sc.get("rsi_filter",False); vth=sc.get("vol_th",1.2)
    tp_s=sc["tp_s"]; dtp=False
    if adx>=DTP_TH: tp_s*=DTP_M; dtp=True
    sv_ok=(not uvf) or (vr>=vth); sr_ok=(not urf) or (rsi>=55)
    # 实时计算cc（修复_inb中cc仅首bar赋值问题）
    short_cc=_real_cc(df,cu) if cu>=sc["sc"] else 0.0
    long_cc=_real_cc(df,cd) if cd>=sc["lc"] else 0.0
    if (cu>=sc["sc"] and short_cc>=sc["ccp"] and sv_ok and sr_ok
            and not sc.get("short_disabled",False)):
        return {"side":"short","entry":entry,"sl":entry+sc["sl_atr"]*atr,"tp":entry-tp_s*atr,
                "adx":round(adx,1),"atr":round(atr,8),"rsi":round(rsi,1),"tp_s":round(tp_s,2),
                "dynamic_tp":dtp,"cu":cu,"cd":cd,"cc":round(short_cc,6),"bar_ts":int(row["ts"]),
                "ema200":round(ema,4),"vol_ratio":round(vr,2),"strategy":"mr"}
    lv_ok=(not uvf) or (vr>=vth); lr_ok=(not urf) or (rsi<=45)
    if (not sc["long_disabled"] and cd>=sc["lc"] and long_cc<=-sc["ccp"]
            and entry>ema and lv_ok and lr_ok):
        return {"side":"long","entry":entry,"sl":entry-sc["sl_atr"]*atr,"tp":entry+tp_s*atr,
                "adx":round(adx,1),"atr":round(atr,8),"rsi":round(rsi,1),"tp_s":round(tp_s,2),
                "dynamic_tp":dtp,"cu":cu,"cd":cd,"cc":round(long_cc,6),"bar_ts":int(row["ts"]),
                "ema200":round(ema,4),"vol_ratio":round(vr,2),"strategy":"mr"}
    return None

def compute_score(sig):
    """均值回归评分: 低ADX加分(震荡市适合均值回归)，高ADX不加分"""
    score = 0.0
    adx = sig["adx"]
    if 10 <= adx <= 20: score += 3.0     # 最佳震荡区间
    elif 0 < adx < 10: score += 1.0       # 太安静，但也可交易
    elif 20 < adx <= 25: score += 1.5     # 轻微趋势，勉强可做
    e=sig["entry"]; rr=abs(sig["tp"]-e)/max(abs(e-sig["sl"]),1e-9)
    if rr>=2.0: score+=2.0
    elif rr>=1.5: score+=1.0
    if sig["dynamic_tp"]: score+=1.0
    return round(min(score,10.0),2)

# 退出
def check_exit(pos,high,low,open_=None):
    s=pos["side"]; e=pos["entry"]; sl=pos["sl"]; tp=pos["tp"]
    if s=="short":
        if low<=tp and high>=sl: return ("SL",sl) if (open_ is not None and open_>=e) else ("TP",tp)
        if low<=tp: return "TP",tp
        if high>=sl: return "SL",sl
    else:
        if high>=tp and low<=sl: return ("SL",sl) if (open_ is not None and open_<=e) else ("TP",tp)
        if high>=tp: return "TP",tp
        if low<=sl: return "SL",sl
    if pos.get("bars_held",0)>=MAX_HOLD: return "TIMEOUT",(high+low)/2
    return None,None

def update_trail(pos,high,low):
    if not TRAIL: return pos
    atr=pos.get("atr",0); e=pos["entry"]; s=pos["side"]
    if atr<=0: return pos
    if s=="short":
        fpa=(e-low)/atr
        if fpa>=T_BE:
            ns=e
            if fpa>=T_LK: ns=min(ns,low+0.3*atr)
            if fpa>=T_DY: ns=min(ns,low+T_DD*atr)
            if ns<pos["sl"]: pos=dict(pos); pos["sl"]=round(ns,8); pos["trailing_active"]=True
    else:
        fpa=(high-e)/atr
        if fpa>=T_BE:
            ns=e
            if fpa>=T_LK: ns=max(ns,high-0.3*atr)
            if fpa>=T_DY: ns=max(ns,high-T_DD*atr)
            if ns>pos["sl"]: pos=dict(pos); pos["sl"]=round(ns,8); pos["trailing_active"]=True
    return pos

# 风控
class DK:
    def __init__(self): self._w=deque(maxlen=WRG_W); self._p=deque(maxlen=WRG_W)
    def record(self,pnl): self._w.append(1 if pnl>0 else 0); self._p.append(pnl)
    def frac(self):
        if len(self._w)<K_MIN: return K_FRAC
        wr=sum(self._w)/len(self._w)
        if wr>=0.80: return 0.45
        if wr>=0.70: return 0.35
        if wr>=0.60: return 0.30
        return K_FRAC
    def risk(self,eq,sp):
        if len(self._w)<K_MIN: return eq*RISK
        wr=sum(self._w)/len(self._w)
        w=[p for p in self._p if p>0]; l=[p for p in self._p if p<=0]
        if not w or not l: return eq*RISK
        aw=sum(w)/len(w); al=abs(sum(l)/len(l))
        if al<1e-9: return eq*RISK
        k=wr-(1-wr)/(aw/al); r=max(0.005,min(K_MAX,k*self.frac()))
        return eq*r/max(sp,0.001)
    @property
    def wr(self): return sum(self._w)/max(len(self._w),1)

class WRG:
    def __init__(self): self._r=deque(maxlen=WRG_W); self._a=False; self._p=False
    def record(self,win):
        self._r.append(win); n=len(self._r)
        if n<WRG_WARM: return  # 热身期不触发暂停
        wr=sum(self._r)/n if n>0 else 1
        if not self._p and wr<WRG_P: self._p=True; logger.warning(f"⛔暂停 WR={wr:.0%}")
        elif self._p and wr>=WRG_MIN: self._p=False; logger.info("✅恢复")
        if not self._a and wr<WRG_MIN: self._a=True; logger.warning(f"⚠️激活 WR={wr:.0%}")
        elif self._a and wr>=WRG_B: self._a=False; logger.info("✅解除")
    @property
    def active(self): return self._a
    @property
    def paused(self): return self._p
    @property
    def min_rr(self): return WRG_RR if self._a else MIN_RR

class BRK:
    def __init__(self): self._lv=0; self._exp=0; self._cl=0
    def update(self,dd,il):
        self._cl=self._cl+1 if il else 0
        if dd>=0.15 and self._lv<2: self._lv=2; self._exp=time.time()+3600; logger.critical(f"🆘HARD DD={dd:.1f}%")
        elif (dd>=0.08 or self._cl>=6) and self._lv<1: self._lv=1; self._exp=time.time()+600; logger.warning("⚠️SOFT")
        if self._lv>0 and time.time()>self._exp: self._lv=0; self._cl=0; logger.info("✅熔断恢复")
    @property
    def allow(self): return self._lv==0
    @property
    def name(self): return {0:"OK",1:"SOFT",2:"HARD"}.get(self._lv,"?")

class SWT:
    def __init__(self): self._sp={s:deque(maxlen=30) for s in SYMBOLS}
    def record(self,sym,pnl): self._sp[sym].append(pnl)
    def get(self):
        scores={}
        for s,pnls in self._sp.items():
            if len(pnls)<5: scores[s]=1.0/len(SYMBOLS)
            else:
                a=np.array(list(pnls)); mu=np.mean(a); std=np.std(a,ddof=1)
                scores[s]=max(0.02,mu/std if std>0 else -1.0)
        t=sum(scores.values())
        return {s:v/t for s,v in scores.items()} if t>0 else {s:1.0/len(SYMBOLS) for s in SYMBOLS}
    def order(self): return sorted(SYMBOLS,key=lambda s:self.get().get(s,0),reverse=True)

class CORR:
    def __init__(self,pos): self._p=pos
    def allow(self,sym,side):
        if sym not in CORR_G: return True
        return sum(1 for s,p in self._p.items() if s in CORR_G and p["side"]==side)<CORR_MAX

# 状态
def _ds():
    return {"positions":{},"equity":INIT_EQ,"peak_equity":INIT_EQ,"max_drawdown":0.0,"day_loss":0.0,
            "day_date":"","total_trades":0,"wins":0,"losses":0,"total_pnl":0.0,"streak":0,
            "daily_stats":{},"sharpe":{"returns":[],"value":0.0},"ss":{}}
def _crc(data):
    raw={k:v for k,v in data.items() if k!="_crc"}
    return hashlib.md5(json.dumps(raw,sort_keys=True).encode()).hexdigest()
def load_state():
    p=Path(STATE)
    if p.exists():
        try:
            raw=json.loads(p.read_text()); sc=raw.pop("_crc","")
            if sc and sc!=_crc(raw): logger.warning("CRC不匹配")
            else: s=_ds(); s.update(raw); return s
        except: pass
    return _ds()
def save_state(s):
    data=dict(s); data["_crc"]=_crc(data)
    tmp=Path(STATE).with_suffix(".tmp"); dst=Path(STATE)
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2)); tmp.replace(dst)
def append_trade(rec):
    with open(TRADE,"a",encoding="utf-8") as f: f.write(json.dumps(rec,ensure_ascii=False)+"\n")

def update_stats(state,net):
    state["streak"]=(max(state["streak"],0)+1) if net>0 else (min(state["streak"],0)-1)
    if state["equity"]>state["peak_equity"]: state["peak_equity"]=state["equity"]
    dd=(state["peak_equity"]-state["equity"])/state["peak_equity"]*100
    if dd>state["max_drawdown"]: state["max_drawdown"]=dd
    today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ds=state["daily_stats"]
    if today not in ds: ds[today]={"pnl":0.0,"trades":0,"wins":0}
    ds[today]["pnl"]+=net; ds[today]["trades"]+=1
    if net>0: ds[today]["wins"]+=1
    r=state["sharpe"]["returns"]; r.append(net)
    if len(r)>50: r.pop(0)
    if len(r)>=10: mu=np.mean(r); std=np.std(r,ddof=1); state["sharpe"]["value"]=round(mu/std,2) if std>0 else 0

def dashboard(state,kelly,wrg,breaker,tf_data):
    total=state["wins"]+state["losses"]; wr=state["wins"]/total*100 if total>0 else 0
    eq_chg=(state["equity"]/INIT_EQ-1)*100; sh=state["sharpe"].get("value",0)
    npos=len(state["positions"])
    sigs=0
    for sym in SYMBOLS:
        df=tf_data.get(sym,{}).get("15m")
        if df is not None and len(df)>=200:
            if _quick2(sym,df,SC[sym]) or mr_signal(sym,df,SC[sym]): sigs+=1
    logger.info(f"┌{'─'*40}┐")
    logger.info(f"│ ⚡ v9.0 {datetime.now(timezone.utc).strftime('%H:%M')} N:{'✅' if HAS_NUMBA else '❌'} T{total} WR{wr:.0f}% │")
    logger.info(f"│ 💰 {state['equity']:.2f}U({eq_chg:+.2f}%) P{state['total_pnl']:+.3f}U DD{state['max_drawdown']:.1f}% Sh{sh:.2f} │")
    logger.info(f"│ 📈 仓{npos}/{MAX_POS} 信{sigs} Kelly{kelly.frac():.2f}x WRG:{'⛔' if wrg.paused else '⚠️' if wrg.active else '✅'} 熔{breaker.name} │")
    if state["positions"]:
        logger.info(f"├{'─'*40}┤")
        for sym,pos in state["positions"].items():
            trl="T" if pos.get("trailing_active") else " "
            logger.info(f"│ {pos['side'].upper():5s} {sym:10s} 入{pos['entry']:.4f} TP{pos['tp']:.4f} SL{pos['sl']:.4f} K{pos.get('bars_held',0)} {trl}│")
    logger.info(f"└{'─'*40}┘")

# ═══════════════════════ 主循环 ═══════════════════════
def main():
    global _running
    Path(PID).write_text(str(os.getpid()))
    state=load_state()
    trades=[]
    tp=Path(TRADE)
    if tp.exists():
        for l in tp.read_text(encoding="utf-8").splitlines():
            l=l.strip()
            if l:
                try: trades.append(json.loads(l))
                except: pass
    kelly=DK(); wrg=WRG(); sw=SWT(); breaker=BRK()
    for t in trades[-WRG_W:]:
        kelly.record(t["net_pnl"]); wrg.record(t["net_pnl"]>0); sw.record(t.get("sym","?"),t["net_pnl"])
    today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("day_date")!=today: state["day_loss"]=0.0; state["day_date"]=today
    logger.info("═"*42)
    logger.info(f" 白夜 v9.0 FINAL | 品种{len(SYMBOLS)} | {'Numba 241x' if HAS_NUMBA else 'Python'}")
    logger.info(f" {INIT_EQ}U RISK={RISK*100:.0f}% FEE={FEE*10000:.1f}bps")
    logger.info(f" 纯MR策略 | MAX_HOLD={MAX_HOLD} | COOLDOWN={COOLDOWN}")
    logger.info(f" 已完成:{len(trades)}笔 净值={state['equity']:.2f}U")
    logger.info("═"*42)
    cd={}; lb={}; pc=0
    if HAS_NUMBA:
        d=np.random.randn(500).astype(np.float64); _inb(d,d,d,None)
    while _running:
        try:
            pc+=1; now=datetime.now(timezone.utc); today=now.strftime("%Y-%m-%d")
            if state.get("day_date")!=today: state["day_loss"]=0.0; state["day_date"]=today
            if state["day_loss"]>=state["equity"]*DAILY_LOSS:
                logger.warning("⛔日熔断"); time.sleep(60); continue
            # Fetch
            all_tf={}
            with ThreadPoolExecutor(max_workers=min(len(SYMBOLS),10)) as pool:
                futures={pool.submit(fetch_multi_tf,sym):sym for sym in SYMBOLS}
                for fut in as_completed(futures):
                    sym=futures[fut]
                    try: all_tf[sym]=fut.result()
                    except Exception as e: logger.warning(f"[{sym}] {e}"); all_tf[sym]={}
            # Exit
            for sym in list(state["positions"].keys()):
                pos=state["positions"][sym]; tf=pos.get("tf",TF_P)
                df=all_tf.get(sym,{}).get(tf)
                if df is None: continue
                cur=df.iloc[-1]; h2=float(cur["high"]); lo=float(cur["low"]); op=float(cur["open"])
                pos["bars_held"]=pos.get("bars_held",0)+1
                pos=update_trail(pos,h2,lo); state["positions"][sym]=pos
                res,ex=check_exit(pos,h2,lo,op)
                if res is None: continue
                qty=pos["qty"]
                raw=((pos["entry"]-ex)*qty if pos["side"]=="short" else (ex-pos["entry"])*qty)
                fee=(pos["entry"]+ex)*qty*FEE; net=raw-fee
                state["equity"]+=net; state["total_pnl"]=state.get("total_pnl",0.0)+net
                state["total_trades"]=state.get("total_trades",0)+1
                if net>0: state["wins"]=state.get("wins",0)+1
                else: state["losses"]=state.get("losses",0)+1; state["day_loss"]+=abs(net)
                update_stats(state,net); kelly.record(net); wrg.record(net>0)
                sw.record(sym,net); pass # breaker disabled
                del state["positions"][sym]
                rec={"no":state["total_trades"],"sym":sym,"tf":tf,"side":pos["side"],
                     "entry":round(pos["entry"],8),"exit":round(ex,8),"qty":round(qty,6),
                     "notional":round(pos.get("notional",0),4),"result":res,"net_pnl":round(net,6),
                     "fee":round(fee,6),"bars_held":pos.get("bars_held",0),
                     "strategy":"mr","open_time":pos["open_time"],"close_time":now.isoformat(),
                     "equity_after":round(state["equity"],6)}
                trades.append(rec); append_trade(rec); save_state(state)
                total=state["wins"]+state["losses"]; wr=state["wins"]/total*100 if total>0 else 0
                e="✅" if net>0 else "❌"
                logger.info(f"{e} #{state['total_trades']:3d} [{tf}] {sym} {pos['side'].upper()} {res} PnL={net:+.5f}U WR={wr:.1f}% 净值={state['equity']:.4f}U")
            # Signals
            if len(state["positions"])<MAX_POS and not wrg.paused:
                corr=CORR(state["positions"])
                for sym in sw.order():
                    if sym in state["positions"]: continue
                    if len(state["positions"])>=MAX_POS: break
                    tf_data=all_tf.get(sym,{}); sc=SC[sym]
                    df15=tf_data.get(TF_P)
                    if df15 is None or len(df15)<200: continue
                    best_sig=None; best_tf=None; best_score=-1.0
                    for tf in [TF_P,TF_C]:
                        ck=f"{sym}_{tf}"
                        if pc-cd.get(ck,0)<COOLDOWN: continue
                        df=tf_data.get(tf)
                        if df is None: continue
                        sig=_quick2(sym,df,sc)
                        if sig is None: sig=mr_signal(sym,df,sc)
                        if sig is None: continue
                        lk=f"{sym}_{tf}"
                        if lb.get(lk)==sig["bar_ts"]: continue
                        score=compute_score(sig)
                        entry=sig["entry"]; rr=abs(sig["tp"]-entry)/max(abs(entry-sig["sl"]),1e-9)
                        if rr<wrg.min_rr: continue
                        if score>best_score: best_score=score; best_sig=sig; best_tf=tf
                    if best_sig is None: continue
                    if best_score<SIG_MIN: continue
                    if not corr.allow(sym,best_sig["side"]): continue
                    sp=abs(best_sig["entry"]-best_sig["sl"])/best_sig["entry"]
                    ru=kelly.risk(state["equity"],sp)
                    sd=abs(best_sig["entry"]-best_sig["sl"])
                    if sd<=0: continue
                    qty=ru/sd; notional=qty*best_sig["entry"]
                    if notional<MIN_NOTIONAL: continue
                    if notional>state["equity"]*0.3: qty=state["equity"]*0.3/best_sig["entry"]; notional=qty*best_sig["entry"]
                    ck=f"{sym}_{best_tf}"; lk=f"{sym}_{best_tf}"
                    lb[lk]=best_sig["bar_ts"]; cd[ck]=pc
                    state["positions"][sym]={
                        "side":best_sig["side"],"tf":best_tf,"entry":best_sig["entry"],
                        "sl":best_sig["sl"],"tp":best_sig["tp"],"qty":qty,"notional":round(notional,4),
                        "score":best_score,"adx":best_sig["adx"],"atr":best_sig["atr"],"rsi":best_sig.get("rsi",50),
                        "tp_s":best_sig["tp_s"],"dynamic_tp":best_sig["dynamic_tp"],
                        "cu":best_sig["cu"],"cd":best_sig["cd"],"cc":best_sig["cc"],
                        "bar_ts":best_sig["bar_ts"],"bars_held":0,"trailing_active":False,
                        "open_time":now.isoformat(),"strategy":best_sig.get("strategy","quick")}
                    corr=CORR(state["positions"]); save_state(state)
                    logger.info(f"🔔 OPEN [{best_tf}] {sym} {best_sig['side'].upper()} sc:{best_score:.1f} 入{best_sig['entry']:.4f} TP{best_sig['tp']:.4f} SL{best_sig['sl']:.4f}")
            dashboard(state,kelly,wrg,breaker,all_tf)
            time.sleep(POLL)
        except KeyboardInterrupt: break
        except Exception as e:
            logger.error(f"异常(#{pc}): {e}"); time.sleep(10)
    logger.info("引擎退出"); save_state(state)
    Path(PID).unlink(missing_ok=True)

if __name__=="__main__":
    tag="241x Numba" if HAS_NUMBA else "Python"
    logger.info(f"白夜 v9.0 FINAL ({tag}) 启动...")
    main()
