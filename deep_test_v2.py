#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deep_test_v2.py — 白夜交易系统 v8.2 深度测试第二阶段

【测试目标】
  1. 多周期协同确认: 15m主信号 + 1h趋势一致性 提升WR
  2. 动态止盈优化: 基于ATR%动态调整tp_mult（高波动时扩大TP）
  3. 追踪止损激活门槛优化: 0.3/0.4/0.5/0.6 ATR对比
  4. 成交量放大验证: vol_th 1.0/1.2/1.5/2.0 对比

【关注品种】
  核心有效: SUI/TON/DOGE/POL/SOL/DOT
  问题修复: HYPE (任何条件下能否达到PF>1?)

【输出】
  research/deep_test_v82.json
"""
from __future__ import annotations
import json, warnings
import numpy as np, pandas as pd, requests
warnings.filterwarnings("ignore")

FAPI = "https://fapi.binance.com"
FEE = 0.0002; MAX_HOLD = 25; RISK = 150*0.02

def wilder(s, n):
    out=np.zeros(len(s)); out[n-1]=s[:n].mean()
    for i in range(n,len(s)): out[i]=(out[i-1]*(n-1)+s[i])/n
    return pd.Series(out, index=s.index)

def calc_all(df, p=14):
    """计算 ATR/ADX/RSI/EMA200/vol_ratio/ATR%"""
    h,l,c,v = df["high"],df["low"],df["close"],df["vol"]
    tr = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    atr = wilder(tr, p)
    up=h.diff(); dn=(-l.diff())
    pdm=np.where((up>dn)&(up>0),up,0.); ndm=np.where((dn>up)&(dn>0),dn,0.)
    a14=wilder(tr,p)
    pdi=100*wilder(pd.Series(pdm,index=df.index),p)/a14
    ndi=100*wilder(pd.Series(ndm,index=df.index),p)/a14
    dx=(100*(pdi-ndi).abs()/(pdi+ndi).replace(0,np.nan)).fillna(0)
    adx=wilder(dx,p)
    delta=c.diff(); gain=delta.clip(lower=0); loss=(-delta).clip(lower=0)
    avg_g=wilder(gain,p); avg_l=wilder(loss,p)
    rsi=100-100/(1+avg_g/avg_l.replace(0,0.001))
    ema200=c.ewm(span=200,adjust=False).mean()
    vol_ma=v.rolling(20).mean(); vol_r=v/vol_ma.replace(0,1)
    atr_pct = atr/c  # ATR占价格比例
    return atr.values,adx.values,rsi.values,ema200.values,vol_r.values,atr_pct.values

def fetch(sym, interval="15m", limit=1500):
    r=requests.get(f"{FAPI}/fapi/v1/klines",
        params={"symbol":sym,"interval":interval,"limit":limit},timeout=12)
    raw=r.json()
    if not isinstance(raw,list): return None
    df=pd.DataFrame(raw,columns=["ts","open","high","low","close","vol","ct","qvol","n","tbv","tqv","x"])
    for col2 in ["open","high","low","close","vol"]: df[col2]=df[col2].astype(float)
    return df

def bt(df_oos, sc, lc, ccp, adx_th, base_tp, sl_m, long_ok,
       trl_thresh=0.5, vol_f=False, vol_th=1.2,
       rsi_f=False, dynamic_tp=False, atr_pct_low=0.003, atr_pct_high=0.008):
    """
    完整回测引擎，支持:
      trl_thresh  — 追踪止损激活门槛（ATR倍数）
      vol_f       — 成交量过滤
      vol_th      — 成交量过滤阈值
      rsi_f       — RSI方向过滤
      dynamic_tp  — 动态止盈（ATR%高时放大TP）
      atr_pct_low/high — 动态TP触发区间
    """
    c,h,l=df_oos["close"].values,df_oos["high"].values,df_oos["low"].values
    atr,adx,rsi,ema200,vol_r,atr_pct=calc_all(df_oos)
    n=len(c)
    cu=np.zeros(n,int); cd=np.zeros(n,int); cc=np.zeros(n)
    for i in range(1,n):
        chg=(c[i]-c[i-1])/c[i-1]
        if c[i]>c[i-1]: cu[i]=cu[i-1]+1; cd[i]=0; cc[i]=chg if cd[i-1]>0 else cc[i-1]+chg
        elif c[i]<c[i-1]: cd[i]=cd[i-1]+1; cu[i]=0; cc[i]=chg if cu[i-1]>0 else cc[i-1]+chg
        else: cu[i]=cu[i-1]; cd[i]=cd[i-1]; cc[i]=cc[i-1]

    wins=losses=0; wpnl=lpnl=0.0
    in_pos=False; side=None; entry=tp_p=sl_p=trl_p=0.; bars=0
    trl_active=False

    for i in range(50,n-1):
        if in_pos:
            bars+=1
            # 追踪止损更新
            if trl_active:
                if side=="short":
                    trl_p=min(trl_p, c[i]+trl_thresh*0.8*atr[i])
                else:
                    trl_p=max(trl_p, c[i]-trl_thresh*0.8*atr[i])

            ex=None; w=0
            if side=="short":
                # 检查追踪止损
                if trl_active and h[i]>=trl_p: ex=trl_p; w=1 if trl_p<entry else 0
                elif l[i]<=tp_p: ex=tp_p; w=1
                elif h[i]>=sl_p: ex=sl_p; w=0
                # 激活追踪止损
                if not trl_active and l[i]<=entry-trl_thresh*atr[i]:
                    trl_active=True; trl_p=entry-trl_thresh*0.5*atr[i]
            else:
                if trl_active and l[i]<=trl_p: ex=trl_p; w=1 if trl_p>entry else 0
                elif h[i]>=tp_p: ex=tp_p; w=1
                elif l[i]<=sl_p: ex=sl_p; w=0
                if not trl_active and h[i]>=entry+trl_thresh*atr[i]:
                    trl_active=True; trl_p=entry+trl_thresh*0.5*atr[i]

            if ex is None and bars>=MAX_HOLD:
                ex=c[i]; w=int(c[i]<entry if side=="short" else c[i]>entry)
            if ex is not None:
                ret=((entry-ex)/entry if side=="short" else (ex-entry)/entry)-FEE*2
                if ret>0: wins+=1; wpnl+=ret
                else: losses+=1; lpnl+=abs(ret)
                in_pos=False; trl_active=False
            continue

        if adx[i]<adx_th or atr[i]<=0: continue
        # 动态TP: 波动率高时放大tp
        tp_m = base_tp
        if dynamic_tp:
            if atr_pct[i] >= atr_pct_high: tp_m = base_tp*1.5
            elif atr_pct[i] >= atr_pct_low: tp_m = base_tp*1.2

        if cu[i]>=sc and cc[i]>=ccp:
            ok=True
            if vol_f and vol_r[i]<vol_th: ok=False
            if rsi_f and rsi[i]<55: ok=False
            if ema200[i]>0 and c[i]>ema200[i]: ok=False  # EMA200过滤
            if ok:
                in_pos=True; side="short"; entry=c[i]
                tp_p=c[i]-tp_m*atr[i]; sl_p=c[i]+sl_m*atr[i]; bars=0; trl_active=False
        elif long_ok and cd[i]>=lc and cc[i]<=-ccp:
            ok=True
            if vol_f and vol_r[i]<vol_th: ok=False
            if rsi_f and rsi[i]>45: ok=False
            if ema200[i]>0 and c[i]<ema200[i]: ok=False  # EMA200过滤
            if ok:
                in_pos=True; side="long"; entry=c[i]
                tp_p=c[i]+tp_m*atr[i]; sl_p=c[i]-sl_m*atr[i]; bars=0; trl_active=False

    tot=wins+losses
    if tot<5: return None
    pf=wpnl/lpnl if lpnl>0 else 99.0
    pnl=round((wpnl-lpnl)*RISK,4)
    return dict(wr=round(wins/tot,3), pf=round(pf,3), n=tot, pnl=pnl)

# ─── v8.2 有效品种配置 ───
CORE = {
    "SUIUSDT":  dict(sc=6,lc=4,ccp=0.001,adx_th=25,tp=0.6,sl=1.5,long_ok=True,  vol_f=True,  rsi_f=False),
    "TONUSDT":  dict(sc=3,lc=3,ccp=0.001,adx_th=15,tp=0.8,sl=1.5,long_ok=True,  vol_f=False, rsi_f=True),
    "DOGEUSDT": dict(sc=3,lc=3,ccp=0.001,adx_th=15,tp=1.5,sl=1.8,long_ok=True,  vol_f=False, rsi_f=False),
    "POLUSDT":  dict(sc=3,lc=3,ccp=0.001,adx_th=25,tp=1.2,sl=1.5,long_ok=False, vol_f=False, rsi_f=False),
    "SOLUSDT":  dict(sc=7,lc=5,ccp=0.001,adx_th=15,tp=0.8,sl=1.5,long_ok=True,  vol_f=False, rsi_f=False),
    "DOTUSDT":  dict(sc=4,lc=4,ccp=0.001,adx_th=30,tp=0.8,sl=1.0,long_ok=True,  vol_f=False, rsi_f=False),
    "HYPEUSDT": dict(sc=3,lc=3,ccp=0.001,adx_th=35,tp=0.8,sl=1.5,long_ok=True,  vol_f=True,  rsi_f=False),
}

print("="*78)
print("  白夜交易系统 v8.2 — 深度测试第二阶段")
print("  追踪止损激活门槛优化 + 动态TP + 成交量阈值对比")
print("="*78)

all_results = {}

for sym, cfg in CORE.items():
    print(f"\n  ▶ {sym}", end=" ", flush=True)
    df = fetch(sym)
    if df is None: print("获取失败"); continue
    df_oos = df.iloc[400:].reset_index(drop=True)
    print(f"({len(df_oos)}根OOS)")

    base_args = dict(
        sc=cfg["sc"],lc=cfg["lc"],ccp=cfg["ccp"],
        adx_th=cfg["adx_th"],base_tp=cfg["tp"],sl_m=cfg["sl"],
        long_ok=cfg["long_ok"],vol_f=cfg["vol_f"],rsi_f=cfg["rsi_f"]
    )

    # 实验1: 追踪止损激活门槛 0.3/0.4/0.5/0.6
    print(f"    {'追踪门槛':<10}", end="")
    best_trl = None; best_trl_val = -1
    for trl in [0.3, 0.4, 0.5, 0.6]:
        r = bt(df_oos, **base_args, trl_thresh=trl)
        if r:
            flag = "✅" if r["pf"]>1 else "  "
            print(f"  trl={trl}: WR={r['wr']:.0%} PF={r['pf']:.2f} n={r['n']}{flag}", end="")
            if r["pf"] > best_trl_val and r["n"] >= 8:
                best_trl_val = r["pf"]; best_trl = trl
        else:
            print(f"  trl={trl}: n/a", end="")
    print(f"  → 最优trl={best_trl}")

    # 实验2: 动态TP开关
    r_no_dtp = bt(df_oos, **base_args, trl_thresh=best_trl or 0.5)
    r_dtp    = bt(df_oos, **base_args, trl_thresh=best_trl or 0.5, dynamic_tp=True)
    if r_no_dtp and r_dtp:
        improve = r_dtp["pf"] - r_no_dtp["pf"]
        print(f"    动态TP:     无={r_no_dtp['pf']:.2f} 有={r_dtp['pf']:.2f} 变化={improve:+.2f} {'✅' if improve>0.05 else '—'}")

    # 实验3: vol_th对比(仅vol_f=True的品种)
    if cfg["vol_f"]:
        print(f"    成交量阈值:", end="")
        for vth in [1.0, 1.2, 1.5, 2.0]:
            r = bt(df_oos, **{**base_args, "vol_f": True}, vol_th=vth, trl_thresh=best_trl or 0.5)
            if r: print(f"  vol≥{vth}: WR={r['wr']:.0%} PF={r['pf']:.2f} n={r['n']}", end="")
        print()

    all_results[sym] = {
        "best_trl": best_trl,
        "dtp_base": r_no_dtp,
        "dtp_on": r_dtp
    }

# ─── 汇总最优配置 ───
print()
print("="*78)
print(f"  {'品种':<12} {'最优trl':>8} {'无DTP_PF':>9} {'有DTP_PF':>9} {'推荐'}")
print("-"*78)
for sym, r in all_results.items():
    t = r["best_trl"] or 0.5
    b = r["dtp_base"]["pf"] if r["dtp_base"] else 0
    d = r["dtp_on"]["pf"]   if r["dtp_on"]   else 0
    rec = "动态TP" if d > b+0.05 else "固定TP"
    print(f"  {sym:<12} {t:>8.1f} {b:>9.2f} {d:>9.2f}  {rec}")

with open("research/deep_test_v82.json", "w") as f:
    json.dump(all_results, f, indent=2, ensure_ascii=False, default=str)
print(f"\n  📄 结果保存: research/deep_test_v82.json")
print("="*78)
