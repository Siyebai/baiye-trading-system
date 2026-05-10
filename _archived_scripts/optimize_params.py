#!/usr/bin/env python3
"""参数优化扫描：n_bars, min_pct, adx_min"""
import json, numpy as np
from pathlib import Path
from engine.ws_feeder import Kline

def load_bars(fp):
    raw=json.load(open(fp)); data=raw if isinstance(raw,list) else raw.get('data',[])
    bars=[]
    for row in data:
        if isinstance(row,(list,tuple)): bars.append(Kline(int(row[0]),row[1],row[2],row[3],row[4],row[5],True))
        else:
            ts=int(row.get('ts',row.get('timestamp',0)))
            bars.append(Kline(ts,row.get('open',0),row.get('high',0),row.get('low',0),row.get('close',0),row.get('volume',0),True))
    return bars

def calc_atr(h,l,c,n=14):
    tr=np.maximum(h-l,np.maximum(np.abs(h-np.roll(c,1)),np.abs(l-np.roll(c,1)))); tr[0]=h[0]-l[0]
    atr=np.zeros(len(tr)); atr[:n]=tr[:n].mean()
    for i in range(n,len(tr)): atr[i]=atr[i-1]*(n-1)/n+tr[i]/n
    return atr

def calc_adx(h,l,c,n=14):
    tr=np.maximum(h-l,np.maximum(np.abs(h-np.roll(c,1)),np.abs(l-np.roll(c,1)))); tr[0]=h[0]-l[0]
    pdm=np.where((h-np.roll(h,1)>np.roll(l,1)-l)&(h-np.roll(h,1)>0),h-np.roll(h,1),0.0)
    ndm=np.where((np.roll(l,1)-l>h-np.roll(h,1))&(np.roll(l,1)-l>0),np.roll(l,1)-l,0.0)
    pdm[0]=ndm[0]=0
    a14=np.zeros(len(tr)); a14[:n]=tr[:n].mean()
    p14=np.zeros(len(tr)); p14[:n]=pdm[:n].mean()
    d14=np.zeros(len(tr)); d14[:n]=ndm[:n].mean()
    for i in range(n,len(tr)):
        a14[i]=a14[i-1]*(n-1)/n+tr[i]/n; p14[i]=p14[i-1]*(n-1)/n+pdm[i]/n; d14[i]=d14[i-1]*(n-1)/n+ndm[i]/n
    with np.errstate(divide='ignore',invalid='ignore'):
        pdi=np.where(a14>0,100*p14/a14,0); ndi=np.where(a14>0,100*d14/a14,0)
        dx=np.where((pdi+ndi)>0,100*np.abs(pdi-ndi)/(pdi+ndi),0)
    adx=np.zeros(len(dx)); adx[:n]=dx[:n].mean()
    for i in range(n,len(dx)): adx[i]=adx[i-1]*(n-1)/n+dx[i]/n
    return adx

def ema_vec(s,n):
    a=2/(n+1); out=np.zeros(len(s)); out[0]=s[0]
    for i in range(1,len(s)): out[i]=s[i]*a+out[i-1]*(1-a)
    return out

def s4_short(c,n,mp):
    out=[]
    for i in range(n+1,len(c)-1):
        mvs=[c[i-k]-c[i-k-1] for k in range(n)]; cum=(c[i]-c[i-n])/c[i-n] if c[i-n]>0 else 0
        if all(m>0 for m in mvs) and cum>=mp: out.append(i)
    return out

def s4_long(c,n,mp):
    out=[]
    for i in range(n+1,len(c)-1):
        mvs=[c[i-k]-c[i-k-1] for k in range(n)]; cum=(c[i-n]-c[i])/c[i-n] if c[i-n]>0 else 0
        if all(m<0 for m in mvs) and cum>=mp: out.append(i)
    return out

def bt_atr(idx,direction,tp_m,sl_m,h,l,c,atr,MAX_HOLD=20,FEE=0.0018):
    wins=losses=0
    for i in idx:
        if i+MAX_HOLD>=len(c): continue
        entry=c[i]; sl_d=atr[i]*sl_m; tp_d=atr[i]*tp_m
        sl_p=entry-direction*sl_d; tp_p=entry+direction*tp_d
        outcome=2
        for j in range(i+1,min(i+MAX_HOLD+1,len(c))):
            if direction==-1:
                if h[j]>=sl_p: outcome=0; break
                if l[j]<=tp_p: outcome=1; break
            else:
                if l[j]<=sl_p: outcome=0; break
                if h[j]>=tp_p: outcome=1; break
        if outcome==1: wins+=1
        elif outcome==0: losses+=1
        else:
            pnl=(entry-c[i+MAX_HOLD])/entry if direction==-1 else (c[i+MAX_HOLD]-entry)/entry
            if pnl>FEE: wins+=1
            else: losses+=1
    n=wins+losses
    return wins/n if n>0 else 0, n, wins, losses

print('='*60)
print('参数优化扫描 (BTC 180d)')
print('='*60)

bars=load_bars('data/BTCUSDT_15m_180d.json')
c=np.array([b.close for b in bars]); h=np.array([b.high for b in bars]); l=np.array([b.low for b in bars])
atr=calc_atr(h,l,c); adx=calc_adx(h,l,c); ema200=ema_vec(c,200)
N=len(c); days=180

print(f'\n扫描范围: n_bars=[4,5,6,7], min_pct=[0.001,0.002,0.003], adx_min=[15,20,25]')
print(f'目标: WR≥58% 且 n≥100 (月≥17笔)\n')

best=[]
for n_s in [4,5,6,7]:
    for mp in [0.001,0.002,0.003]:
        for adx_th in [15,20,25]:
            si=[i for i in s4_short(c,n_s,mp) if i<len(adx) and adx[i]>=adx_th]
            li=[i for i in s4_long(c,4,mp) if i<len(adx) and adx[i]>=adx_th and c[i]>ema200[i]]
            wr_s,n_s_cnt,_,_=bt_atr(si,-1,1.0,1.0,h,l,c,atr)
            wr_l,n_l_cnt,_,_=bt_atr(li,1,0.8,1.0,h,l,c,atr)
            n_total=n_s_cnt+n_l_cnt
            if n_total<50: continue
            wr_comb=(wr_s*n_s_cnt+wr_l*n_l_cnt)/n_total if n_total>0 else 0
            monthly=n_total/days*30
            ev=wr_comb*0.9-0.1-0.0018  # 简化EV估算
            flag='✅✅' if wr_comb>=0.58 and monthly>=15 else ('✅' if wr_comb>=0.55 and monthly>=10 else '')
            if flag:
                print(f'n={n_s} pct={mp:.3f} ADX≥{adx_th}: n={n_total}({monthly:.0f}/月) WR={wr_comb:.1%} EV≈{ev:.3f} {flag}')
                best.append((wr_comb,monthly,n_total,n_s,mp,adx_th,ev))

print(f'\nTop 5:')
for i,(wr,m,n,ns,mp,adx,ev) in enumerate(sorted(best,reverse=True)[:5]):
    print(f'  {i+1}. n={ns} pct={mp:.3f} ADX≥{adx}: WR={wr:.1%} 月={m:.0f}笔 EV≈{ev:.3f}')
" 2>&1