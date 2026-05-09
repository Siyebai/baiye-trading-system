#!/usr/bin/env python3
"""
白夜高频纸交易引擎 v2.0 — 1m周期 × 20品种
目标：今日100笔闭环交易（实际可达1000+）
- 每10秒扫描一次所有品种
- 1m K线，sc=3连涨/lc=3连跌，信号高频
- 极简内存：全局GC，无大数据集常驻
"""
import json, time, gc, warnings
import numpy as np, pandas as pd
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

warnings.filterwarnings("ignore")

CAPITAL   = 150.0
RISK_PCT  = 0.02
FEE       = 0.0009
MAX_POS   = 8          # 允许8个同时持仓
INTERVAL  = "1m"
KLINE_LIM = 280
POLL_SECS = 10         # 10秒扫一次（更高频）
MAX_HOLD  = 60         # 最多60根K线（60分钟）超时平仓
LOG_FILE  = Path("logs/hft_engine.log")
STATE_FILE= Path("logs/hft_state.json")
LOG_FILE.parent.mkdir(exist_ok=True)

SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "ADAUSDT","DOTUSDT","LINKUSDT","UNIUSDT","POLUSDT",
    "LTCUSDT","ATOMUSDT","NEARUSDT","APTUSDT","ARBUSDT",
    "OPUSDT","DOGEUSDT","AAVEUSDT","AVAXUSDT","INJUSDT"
]

# 参数：1m宽松版（sc=3，低ccp，低adx）
PARAMS = {s: dict(sc=3, lc=3, ccp=0.0005, adx=8, tp_s=0.8, tp_l=0.8, long_ok=True) for s in SYMBOLS}
# 禁LONG品种
for s in ["BNBUSDT","POLUSDT","DOGEUSDT"]: PARAMS[s]["long_ok"] = False

_last_bar = {}   # 去重

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f: f.write(line + "\n")

def fetch_klines(sym):
    url = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={INTERVAL}&limit={KLINE_LIM}"
    try:
        with urllib.request.urlopen(url, timeout=8) as r: return json.loads(r.read())
    except: return None

def compute_signal(data, p):
    if not data or len(data) < 230: return 0, 0, 0, 0
    close = np.array([float(k[4]) for k in data], dtype=np.float64)
    high  = np.array([float(k[2]) for k in data], dtype=np.float64)
    low   = np.array([float(k[3]) for k in data], dtype=np.float64)
    n = len(close)
    # ATR Wilder's
    tr = np.empty(n); tr[0] = high[0]-low[0]
    for i in range(1,n): tr[i]=max(high[i]-low[i],abs(high[i]-close[i-1]),abs(low[i]-close[i-1]))
    atr = np.zeros(n); atr[13] = tr[:14].mean()
    for i in range(14,n): atr[i]=(atr[i-1]*13+tr[i])/14
    # ADX Wilder's
    up=np.diff(high,prepend=high[0]); down=-np.diff(low,prepend=low[0])
    pdm=np.where((up>down)&(up>0),up,0.0); ndm=np.where((down>up)&(down>0),down,0.0)
    a14=np.zeros(n); p14=np.zeros(n); d14=np.zeros(n)
    a14[13]=tr[:14].mean(); p14[13]=pdm[:14].mean(); d14[13]=ndm[:14].mean()
    for i in range(14,n):
        a14[i]=(a14[i-1]*13+tr[i])/14; p14[i]=(p14[i-1]*13+pdm[i])/14; d14[i]=(d14[i-1]*13+ndm[i])/14
    with np.errstate(invalid='ignore',divide='ignore'):
        pdi=np.where(a14>0,100*p14/a14,0.0); ndi=np.where(a14>0,100*d14/a14,0.0)
        dx=np.where((pdi+ndi)>0,100*np.abs(pdi-ndi)/(pdi+ndi),0.0)
    adx=np.zeros(n); adx[13]=dx[:14].mean()
    for i in range(14,n): adx[i]=(adx[i-1]*13+dx[i])/14
    ema200=pd.Series(close).ewm(span=200,adjust=False).mean().values
    # 连涨跌（检查已完成K线，倒数第2根）
    idx = n-2
    cu=cd=0; cc_up=cc_dn=0.0
    for i in range(max(0,idx-15),idx+1):
        c=(close[i]-close[i-1])/close[i-1] if i>0 and close[i-1]>0 else 0
        if c>0:
            cu+=1; cd=0
            cc_up = c if cu==1 else cc_up+c; cc_dn=0
        elif c<0:
            cd+=1; cu=0
            cc_dn = c if cd==1 else cc_dn+c; cc_up=0
        else:
            cu=cd=0; cc_up=cc_dn=0
    sc=p['sc']; lc=p['lc']; ccp=p['ccp']; adx_th=p['adx']
    cur_adx=adx[idx]; cur_atr=atr[idx]; entry=close[-1]; cur_ema=ema200[idx]
    if cur_atr<=0: return 0,0,0,0
    if cu>=sc and cc_up>=ccp and cur_adx>=adx_th:
        return -1, entry, entry-cur_atr*p['tp_s'], entry+cur_atr*1.0
    if cd>=lc and cc_dn<=-ccp and cur_adx>=adx_th and close[idx]>cur_ema and p['long_ok']:
        return 1, entry, entry+cur_atr*p['tp_l'], entry-cur_atr*1.0
    return 0,0,0,0

def load_state():
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text())
        except: pass
    return {"equity":CAPITAL,"positions":{},"trades":[],"round":0}

def save_state(s):
    STATE_FILE.write_text(json.dumps(s,indent=2,default=str))

def main():
    log("🚀 白夜HFT v2.0 启动 | 1m×20品种 | 目标100笔")
    state = load_state()
    last_n = len(state.get("trades",[]))
    log(f"   资金:{state['equity']:.2f}U 历史:{last_n}笔")
    
    while True:
        state["round"] = state.get("round",0)+1
        rnd = state["round"]
        
        # 1. 检查持仓TP/SL
        to_close = []
        for sym, pos in list(state["positions"].items()):
            try:
                kurl = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={INTERVAL}&limit=2"
                with urllib.request.urlopen(kurl,timeout=5) as r: kd=json.loads(r.read())
                bar_h=float(kd[-1][2]); bar_l=float(kd[-1][3]); cur_p=float(kd[-1][4])
            except: continue
            d=pos["dir"]; tp=pos["tp"]; sl=pos["sl"]
            if d==1:
                if bar_h>=tp: to_close.append((sym,"TP",tp))
                elif bar_l<=sl: to_close.append((sym,"SL",sl))
            else:
                if bar_l<=tp: to_close.append((sym,"TP",tp))
                elif bar_h>=sl: to_close.append((sym,"SL",sl))
            if rnd - pos.get("open_round",0) >= MAX_HOLD:
                if sym not in [x[0] for x in to_close]:
                    to_close.append((sym,"TIMEOUT",cur_p))
        
        for sym,reason,exit_p in to_close:
            if sym not in state["positions"]: continue
            pos=state["positions"].pop(sym)
            notional=pos["risk_amt"]/pos["sl_pct"] if pos["sl_pct"]>0 else 0
            fee_cost=notional*FEE*2
            if reason=="TP": pnl=pos["risk_amt"]*pos["tp_mult"]-fee_cost
            elif reason=="SL": pnl=-pos["risk_amt"]-fee_cost
            else:
                move=(exit_p-pos["entry"])/pos["entry"]*pos["dir"]
                pnl=move*notional-fee_cost
            state["equity"]+=pnl
            state["trades"].append({
                "sym":sym,"dir":"LONG" if pos["dir"]==1 else "SHORT",
                "entry":pos["entry"],"exit":exit_p,"reason":reason,
                "pnl":round(pnl,4),"equity":round(state["equity"],4),
                "fee":round(fee_cost,4),
                "open_ts":pos["open_ts"],"close_ts":datetime.now(timezone.utc).isoformat()
            })
            if len(state["trades"])>2000: state["trades"]=state["trades"][-2000:]
            emoji="✅" if reason=="TP" else ("❌" if reason=="SL" else "⏰")
            log(f"  {emoji} {sym:10} {'LONG' if pos['dir']==1 else 'SHORT':5} [{reason:7}] PnL={pnl:+.3f}U 资金={state['equity']:.2f}U")
        
        # 2. 扫描新信号
        if len(state["positions"])<MAX_POS:
            for sym in SYMBOLS:
                if sym in state["positions"] or len(state["positions"])>=MAX_POS: continue
                p=PARAMS[sym]
                data=fetch_klines(sym)
                if not data: continue
                last_bar_ts=data[-2][0]  # 已完成K线时间戳
                if _last_bar.get(sym)==last_bar_ts: continue  # 同一根K线不重复开仓
                sig,entry,tp,sl=compute_signal(data,p)
                if sig==0: continue
                _last_bar[sym]=last_bar_ts
                sl_dist=abs(sl-entry); sl_pct=sl_dist/entry if entry>0 else 0.005
                risk_amt=state["equity"]*RISK_PCT
                side="LONG" if sig==1 else "SHORT"
                log(f"  📍 {sym:10} {side:5} 入:{entry:.5g} TP:{tp:.5g} SL:{sl:.5g}")
                state["positions"][sym]={
                    "dir":sig,"entry":entry,"tp":tp,"sl":sl,
                    "risk_amt":risk_amt,"sl_pct":sl_pct,"tp_mult":p["tp_s"] if sig==-1 else p["tp_l"],
                    "open_ts":datetime.now(timezone.utc).isoformat(),"open_round":rnd
                }
                time.sleep(0.03)
        
        # 3. 汇报
        trades=state["trades"]; n_t=len(trades)
        wins=sum(1 for t in trades if t["pnl"]>0)
        wr=wins/n_t*100 if n_t>0 else 0
        pnl_total=state["equity"]-CAPITAL
        if rnd%20==1 or n_t!=last_n:
            log(f"  📊 轮{rnd:5d}|持仓:{len(state['positions'])}|交易:{n_t:4d}笔 WR:{wr:.1f}%|PnL:{pnl_total:+.2f}U|资金:{state['equity']:.2f}U")
            last_n=n_t
        save_state(state)
        gc.collect()
        time.sleep(POLL_SECS)

if __name__=="__main__":
    main()
