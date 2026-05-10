#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
白夜纸交易 v6.4 — 策略修正版（Limit单/修正费率）
核心改动（vs v6.3）:
  1. FEE=0.0002 (模拟Maker限价单，合约Maker 0.02%单边)
  2. 开仓用下根K线收盘价（替代open价，模拟限价单延迟成交）
  3. 聚焦3个EV>0品种: LINK(tp=0.8×) + SOL(tp=1.0×) + BNB(tp=1.2×)
  4. 去除ADX太低的品种（LTC/AVAX/BTC/ETH/POL）
  5. MAX_POSITIONS=3, POLL_SECS=30
  6. 目标: 100笔完整闭环

手续费说明:
  - 合约Maker(限价挂单): 0.02%单边 (BNB折扣后约0.015%)
  - 模拟方式: entry使用下根K线close价 (更接近挂单成交价格)
"""
import json, time, requests, numpy as np, pandas as pd, warnings
from pathlib import Path
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
import logging
warnings.filterwarnings("ignore")

CAPITAL       = 150.0
RISK_PCT      = 0.02
FEE           = 0.0002    # Maker限价单 0.02%单边
MAX_POSITIONS = 3
POLL_SECS     = 30
LOG_FILE      = Path("logs/paper_v64.log")
STATE_FILE    = Path("logs/paper_v64_state.json")
TRADES_FILE   = Path("paper_trades_v64.json")
TARGET_TRADES = 100
LOG_FILE.parent.mkdir(exist_ok=True)

_h = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=2, encoding='utf-8')
_h.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
logger = logging.getLogger('paper_v64')
logger.addHandler(_h)
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)

# v6.4 最优参数 — 基于Maker费率验证EV>0
CONFIGS = {
    "LINKUSDT": dict(sc=7, lc=4, ccp=0.0025, adx_th=15, tp_s=0.8, tp_l=0.8, sl_atr=1.0, long_disabled=False),
    "SOLUSDT":  dict(sc=5, lc=4, ccp=0.0015, adx_th=25, tp_s=1.0, tp_l=1.0, sl_atr=1.0, long_disabled=False),
    "BNBUSDT":  dict(sc=5, lc=6, ccp=0.0015, adx_th=15, tp_s=1.2, tp_l=1.2, sl_atr=1.0, long_disabled=True),
}
SYMBOLS = list(CONFIGS.keys())

# ── 指标计算（Wilder's，与回测引擎一致）───────────────────────
def _wilder(arr, n):
    out = np.full(len(arr), np.nan)
    idx = np.where(~np.isnan(arr))[0]
    if len(idx) < n: return out
    s = idx[0]
    out[s+n-1] = np.nanmean(arr[s:s+n])
    for i in range(s+n, len(arr)):
        if not np.isnan(out[i-1]):
            out[i] = (out[i-1]*(n-1) + arr[i]) / n
    return out

def compute(df):
    df = df.copy()
    c, h, l = df['close'].values, df['high'].values, df['low'].values
    n = len(c)
    tr = np.maximum(h-l, np.maximum(np.abs(h-np.roll(c,1)), np.abs(l-np.roll(c,1))))
    tr[0] = h[0]-l[0]
    df['atr'] = _wilder(tr, 14)
    df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()
    up = np.diff(h, prepend=h[0]); dn = np.diff(l, prepend=l[0]) * -1
    pdm = np.where((up>dn)&(up>0), up, 0.0)
    ndm = np.where((dn>up)&(dn>0), dn, 0.0)
    atr14 = _wilder(tr, 14)
    pdi = 100*_wilder(pdm,14)/np.where(atr14>0,atr14,np.nan)
    ndi = 100*_wilder(ndm,14)/np.where(atr14>0,atr14,np.nan)
    dx = 100*np.abs(pdi-ndi)/np.where((pdi+ndi)>0,pdi+ndi,np.nan)
    df['adx'] = _wilder(dx, 14)
    cu=np.zeros(n,dtype=int); cd=np.zeros(n,dtype=int); cc=np.zeros(n)
    for i in range(1, n):
        if c[i] > c[i-1]:
            cu[i]=cu[i-1]+1; cd[i]=0
            cc[i] = (c[i]-c[i-1])/c[i-1] if cu[i]==1 else cc[i-1]+(c[i]-c[i-1])/c[i-1]
        elif c[i] < c[i-1]:
            cd[i]=cd[i-1]+1; cu[i]=0
            cc[i] = (c[i]-c[i-1])/c[i-1] if cd[i]==1 else cc[i-1]+(c[i]-c[i-1])/c[i-1]
        else:
            cu[i]=cu[i-1]; cd[i]=cd[i-1]; cc[i]=cc[i-1]
    df['cu']=cu; df['cd']=cd; df['cc']=cc
    return df

def fetch_klines(sym, limit=300):
    url = "https://api.binance.com/api/v3/klines"
    r = requests.get(url, params={'symbol':sym,'interval':'15m','limit':limit}, timeout=10)
    d = r.json()
    df = pd.DataFrame(d, columns=['ts','open','high','low','close','vol','ct','qv','tr','tbb','tbq','ign'])
    for col in ['open','high','low','close']: df[col]=df[col].astype(float)
    df['ts']=pd.to_datetime(df['ts'],unit='ms',utc=True)
    return df.set_index('ts')

def check_signal(sym, df, cfg):
    """检测信号 — 用倒数第2根已完成K线（[-2]），以[-1]收盘价模拟Limit成交"""
    if len(df) < 210: return None
    row = df.iloc[-2]
    adx = row['adx']
    if np.isnan(adx) or adx < cfg['adx_th']: return None
    atr = row['atr']
    if np.isnan(atr) or atr <= 0: return None
    # 用最新收盘价模拟Limit成交（更接近挂单）
    entry = float(df.iloc[-1]['close'])
    # 做空
    if row['cu'] >= cfg['sc'] and row['cc'] >= cfg['ccp']:
        sl = entry + cfg['sl_atr'] * atr
        tp = entry - cfg['tp_s'] * atr
        return ('做空', entry, sl, tp, adx, atr)
    # 做多（若不禁）
    if not cfg.get('long_disabled') and row['cd'] >= cfg['lc'] and row['cc'] <= -cfg['ccp'] and row['close'] > row['ema200']:
        sl = entry - cfg['sl_atr'] * atr
        tp = entry + cfg['tp_l'] * atr
        return ('做多', entry, sl, tp, adx, atr)
    return None

def load_state():
    if STATE_FILE.exists(): return json.loads(STATE_FILE.read_text())
    return {"positions":{}, "equity":CAPITAL, "last_bar":{}, "day_loss":0.0, "day_date":"", "total_trades":0}

def save_state(s): STATE_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False))
def load_trades():
    if TRADES_FILE.exists(): return json.loads(TRADES_FILE.read_text())
    return []
def save_trades(t): TRADES_FILE.write_text(json.dumps(t, indent=2, ensure_ascii=False))

def summary(trades):
    closed = [t for t in trades if t.get('status')=='closed']
    if not closed: return "尚无已关闭交易"
    wins = [t for t in closed if t.get('pnl',0)>0]
    pnl = sum(t.get('pnl',0) for t in closed)
    wr = len(wins)/len(closed)*100
    return f"[{len(closed)}/{TARGET_TRADES}] WR={wr:.1f}% 总PnL={pnl:+.3f}U"

def main():
    logger.info("="*60)
    logger.info("白夜纸交易 v6.4 — Maker限价单策略（修正费率）")
    logger.info(f"品种: {SYMBOLS}")
    logger.info(f"FEE={FEE*100:.3f}%单边(Maker) | 资金:{CAPITAL}U | 风险:{RISK_PCT*100}%")
    logger.info(f"LINK:tp=0.8×ATR | SOL:tp=1.0×ATR | BNB:tp=1.2×ATR(禁多)")
    logger.info("="*60)
    state = load_state()
    trades = load_trades()
    logger.info(f"恢复: 持仓={list(state['positions'].keys())} 权益={state['equity']:.4f}U")
    logger.info(f"进度: {summary(trades)}")

    while True:
        try:
            closed_count = len([t for t in trades if t.get('status')=='closed'])
            if closed_count >= TARGET_TRADES:
                logger.info(f"🎯 已完成{TARGET_TRADES}笔！{summary(trades)}")
                save_state(state); save_trades(trades)
                break

            now = datetime.now(timezone.utc)
            today = now.strftime('%Y-%m-%d')
            if state.get('day_date') != today:
                state['day_loss'] = 0.0; state['day_date'] = today

            # 日熔断（亏损≥6%）
            if state['day_loss'] <= -(CAPITAL * 0.06):
                logger.warning(f"日熔断: 今日亏损={state['day_loss']:.3f}U，暂停300s")
                time.sleep(300); continue

            # 检查持仓
            for sym in list(state['positions'].keys()):
                pos = state['positions'][sym]
                try:
                    df = compute(fetch_klines(sym))
                    cur = float(df.iloc[-1]['close'])
                    entry=pos['entry']; tp=pos['tp']; sl=pos['sl']
                    hit = None
                    if pos['dir'] == '做空':
                        if cur <= tp: hit = '止盈'
                        elif cur >= sl: hit = '止损'
                        pnl_price = entry - cur
                    else:
                        if cur >= tp: hit = '止盈'
                        elif cur <= sl: hit = '止损'
                        pnl_price = cur - entry
                    if hit:
                        exit_price = tp if hit == '止盈' else sl
                        pnl = pnl_price/entry*pos['notional'] - pos['notional']*FEE*2
                        state['equity'] += pnl
                        state['day_loss'] += min(pnl, 0)
                        pos.update(status='closed', exit=exit_price, pnl=round(pnl,4),
                                   exit_reason=hit, exit_time=now.isoformat())
                        trades.append(pos)
                        del state['positions'][sym]
                        icon = '✅' if hit == '止盈' else '❌'
                        logger.info(f"{icon}#{closed_count+1} {sym} {pos['dir']} {hit} | "
                                    f"in={entry:.4f} out={exit_price:.4f} pnl={pnl:+.4f}U | "
                                    f"权益={state['equity']:.2f}U")
                except Exception as e:
                    logger.warning(f"{sym} 持仓检查: {e}")

            # 扫描新信号
            if len(state['positions']) < MAX_POSITIONS:
                for sym in SYMBOLS:
                    if sym in state['positions'] or len(state['positions']) >= MAX_POSITIONS:
                        continue
                    try:
                        df = compute(fetch_klines(sym))
                        sig = check_signal(sym, df, CONFIGS[sym])
                        if not sig: continue
                        direction, entry, sl, tp, adx_val, atr_val = sig
                        bar_ts = str(df.index[-2])
                        key = f"{sym}_{direction}"
                        if state.get('last_bar', {}).get(key) == bar_ts: continue
                        risk_amt = state['equity'] * RISK_PCT
                        sl_dist = abs(entry - sl)
                        if sl_dist <= 0: continue
                        qty = risk_amt / sl_dist
                        notional = qty * entry
                        trade_no = len([t for t in trades if t.get('status')=='closed']) + len(state['positions']) + 1
                        pos = dict(no=trade_no, sym=sym, dir=direction, entry=entry,
                                   sl=sl, tp=tp, qty=round(qty,6),
                                   notional=round(notional,4),
                                   adx=round(adx_val,2), atr=round(atr_val,6),
                                   open_time=now.isoformat(), status='open', pnl=0)
                        state['positions'][sym] = pos
                        state.setdefault('last_bar', {})[key] = bar_ts
                        logger.info(f"🔔 #{trade_no} 开仓 {sym} {direction} | "
                                    f"in={entry:.4f} sl={sl:.4f} tp={tp:.4f} | "
                                    f"ADX={adx_val:.1f} ATR={atr_val:.4f} notional={notional:.1f}U")
                    except Exception as e:
                        logger.warning(f"{sym} 扫描: {e}")

            save_state(state); save_trades(trades)
            closed_count = len([t for t in trades if t.get('status')=='closed'])
            logger.info(f"权益={state['equity']:.4f}U 持仓={list(state['positions'].keys())} {summary(trades)}")
            time.sleep(POLL_SECS)

        except KeyboardInterrupt:
            logger.info("纸交易停止"); save_state(state); save_trades(trades); break
        except Exception as e:
            logger.error(f"主循环: {e}", exc_info=True); time.sleep(10)

if __name__ == '__main__':
    main()
