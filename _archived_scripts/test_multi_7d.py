#!/usr/bin/env python3
import json, numpy as np, tempfile, os
from pathlib import Path
from engine.ws_feeder import Kline
from engine.signal_engine import SignalEngine
from engine.risk_engine import RiskEngine

def load_bars(fp):
    raw=json.load(open(fp))
    data=raw if isinstance(raw,list) else raw.get('data',[])
    bars=[]
    for row in data:
        if isinstance(row,(list,tuple)):
            bars.append(Kline(int(row[0]),row[1],row[2],row[3],row[4],row[5],True))
        else:
            ts=int(row.get('ts',row.get('timestamp',0)))
            bars.append(Kline(ts,row.get('open',0),row.get('high',0),row.get('low',0),row.get('close',0),row.get('volume',0),True))
    return bars

print('🧪 4品种7天模拟验证')
print('='*50)

symbols=['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT']
cfg={'risk_control':{'mode':'FIXED','capital':150.0,'risk_per_trade_u':3.0,
       'max_daily_loss_u':15.0,'max_monthly_loss_u':45.0,
       'consecutive_loss_reduce':3,'reduced_risk_u':1.5}}

all_trades=[]
for sym in symbols:
    fp=Path(f'data/{sym}_15m_live.json')
    if not fp.exists():
        print(f'{sym}: 无数据')
        continue
    bars=load_bars(fp)
    n_7d=int(7*24*4)
    bars=bars[-n_7d:]
    
    eng=SignalEngine(symbol=sym)
    state_f=tempfile.mktemp(suffix='.json')
    risk=RiskEngine(state_f,cfg)
    
    WINDOW=250; MAX_HOLD=20; FEE=0.0018
    position=None; pos_idx=-1; trades=[]
    
    for i in range(WINDOW,len(bars)):
        window=bars[max(0,i-WINDOW):i+1]
        sig=eng.evaluate(window)
        
        if position:
            dm=-1 if position.direction=='SHORT' else 1
            closed=False; outcome=None
            if dm==-1:
                if bars[i].high>=position.sl_price: closed=True; outcome='loss'
                elif bars[i].low<=position.tp_price: closed=True; outcome='win'
            else:
                if bars[i].low<=position.sl_price: closed=True; outcome='loss'
                elif bars[i].high>=position.tp_price: closed=True; outcome='win'
            if not closed and (i-pos_idx)>=MAX_HOLD:
                closed=True
                pnl_d=(position.entry_price-bars[i].close)/position.entry_price if dm==-1 else (bars[i].close-position.entry_price)/position.entry_price
                outcome='win' if pnl_d>FEE else 'loss'
            if closed:
                e=position.entry_price
                sl_dist=abs(e-position.sl_price)/e
                risk_amt=risk.get_risk_amount()
                notional=risk_amt/sl_dist if sl_dist>0 else 0
                tp_dist=abs(position.tp_price-e)/e
                pnl=(notional*tp_dist-notional*FEE) if outcome=='win' else (-notional*sl_dist-notional*FEE)
                risk.on_trade_close(pnl,outcome)
                trades.append({'sym':sym,'outcome':outcome,'pnl':pnl})
                position=None
        
        if position: continue
        if sig.direction=='NONE': continue
        can,_=risk.can_trade()
        if not can: continue
        qty,_=risk.calc_position(sig.entry_price,sig.sl_price)
        if qty<=0: continue
        position=type('P',(),{
            'direction':sig.direction,
            'entry_price':bars[i].close,
            'sl_price':sig.sl_price,
            'tp_price':sig.tp_price
        })()
        risk.on_trade_open(sig.direction,position.entry_price,sig.sl_price,sig.tp_price,qty)
        pos_idx=i
    
    if n>0: wins_cnt=len([t for t in trades if t["outcome"]=="win"]); wr=wins_cnt/n if n>0 else 0; wins_cnt=0; wins_cnt=len([t for t in trades if t["outcome"]=="win"]); wr=wins_cnt/n if n>0 else 0 if t['outcome']=='win']
    n=len(trades)
    wr=wins/n if n>0 else 0
    pnl=sum(t['pnl'] for t in trades)
    all_trades.extend(trades)
    print(f'{sym}: n={n} WR={wr:.1%} PnL={pnl:+.2f}U')
    os.unlink(state_f)

print('='*50)
n=len(all_trades)
wins=[t for t in all_trades if t['outcome']=='win']
wr=wins/n if n>0 else 0
pnl=sum(t['pnl'] for t in all_trades)
monthly=pnl/7*30
print(f'合计7天: n={n} WR={wr:.1%} PnL={pnl:+.2f}U')
print(f'预估月: {monthly:+.1f}U ({monthly/150*100:+.1f}%)')
