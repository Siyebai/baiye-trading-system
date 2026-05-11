#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
白夜纸交易 FINAL — 一次性修复所有Bug
关键修复:
  1. 仓位: qty = risk_u / (sl_atr * atr_usdt)  正确风控
  2. cc方向切换时正确重置
  3. 单次fetch用于信号+退出双检查
  4. 无notional上限（允许小名义值持仓）
  5. 捕获SIGTERM优雅退出
"""
import json, time, requests, numpy as np, pandas as pd, warnings, sys, signal
from pathlib import Path
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
import logging
warnings.filterwarnings("ignore")

# ── 配置 ────────────────────────────────────────────────────
CAPITAL        = 150.0
RISK_PCT       = 0.02       # 每笔风险2% = 3U
FEE            = 0.0004     # 0.04%单边
MAX_POS        = 4
POLL_SECS      = 20
TARGET         = 10         # 目标完成笔数
DAILY_LOSS_MAX = 0.06       # 日熔断6%
TF             = "1m"

BASE    = Path(__file__).parent
LOG_F   = BASE / "logs/paper_final.log"
STATE_F = BASE / "logs/paper_final_state.json"
TRADE_F = BASE / "logs/paper_final_trades.json"
LOG_F.parent.mkdir(exist_ok=True)

def _mklog():
    h = RotatingFileHandler(LOG_F, maxBytes=5*1024*1024, backupCount=2, encoding='utf-8')
    h.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    lg = logging.getLogger('final')
    lg.propagate = False  # 防止重复输出
    if not lg.handlers:
        lg.addHandler(h); lg.setLevel(logging.INFO)
    return lg

log = _mklog()

# ── 品种（1m参数）────────────────────────────────────────────
CFG = {
    "ETHUSDT":  dict(sc=3, lc=3, ccp=0.0003, adx_th=10, tp_m=2.0, sl_m=1.0, no_long=False, no_ema=False),
    "SOLUSDT":  dict(sc=3, lc=3, ccp=0.0003, adx_th=10, tp_m=2.0, sl_m=1.0, no_long=False, no_ema=False),
    "LINKUSDT": dict(sc=3, lc=3, ccp=0.0004, adx_th=10, tp_m=2.0, sl_m=1.0, no_long=False, no_ema=False),
    "BNBUSDT":  dict(sc=3, lc=3, ccp=0.0003, adx_th=10, tp_m=2.0, sl_m=1.0, no_long=True,  no_ema=False),
    "DOTUSDT":  dict(sc=3, lc=3, ccp=0.0003, adx_th=10, tp_m=2.0, sl_m=1.0, no_long=False, no_ema=True),
    "ADAUSDT":  dict(sc=3, lc=3, ccp=0.0003, adx_th=10, tp_m=2.0, sl_m=1.0, no_long=False, no_ema=True),
    "XRPUSDT":  dict(sc=3, lc=3, ccp=0.0003, adx_th=10, tp_m=2.0, sl_m=1.0, no_long=False, no_ema=True),
    "BTCUSDT":  dict(sc=3, lc=3, ccp=0.0003, adx_th=10, tp_m=2.0, sl_m=1.0, no_long=False, no_ema=True),
}
SYMS = list(CFG.keys())

# ── 信号终止 ─────────────────────────────────────────────────
_run = True
def _sig(s, f): global _run; _run = False; log.warning(f"收到信号{s}，准备退出")
signal.signal(signal.SIGTERM, _sig)
signal.signal(signal.SIGINT, _sig)

# ── 指标计算 ─────────────────────────────────────────────────
def _wilder(s, n):
    out = np.full(len(s), np.nan)
    if len(s) < n: return out
    out[n-1] = s[:n].mean()
    for i in range(n, len(s)): out[i] = out[i-1]*(n-1)/n + s[i]/n
    return out

def compute(df):
    df = df.copy()
    pc = df['close'].shift(1).fillna(df['close'].iloc[0]).values
    tr = np.maximum(df['high'].values - df['low'].values,
         np.maximum(np.abs(df['high'].values-pc), np.abs(df['low'].values-pc)))
    df['atr'] = _wilder(tr, 14)
    pm = np.clip(np.diff(df['high'].values, prepend=df['high'].values[0]), 0, None)
    nm = np.clip(-np.diff(df['low'].values, prepend=df['low'].values[0]), 0, None)
    trw = _wilder(tr, 14)
    pdi = np.where(trw>0, _wilder(np.where(pm>nm,pm,0.),14)/trw*100, 0.)
    ndi = np.where(trw>0, _wilder(np.where(nm>pm,nm,0.),14)/trw*100, 0.)
    df['adx'] = _wilder(np.where((pdi+ndi)>0, np.abs(pdi-ndi)/(pdi+ndi)*100, 0.), 14)
    df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()
    c = df['close'].values
    cu=np.zeros(len(c),int); cd=np.zeros(len(c),int); cc=np.zeros(len(c))
    for i in range(1, len(c)):
        chg=(c[i]-c[i-1])/c[i-1]
        if c[i]>c[i-1]:
            cu[i]=cu[i-1]+1; cd[i]=0
            cc[i]=chg if cd[i-1]>0 else cc[i-1]+chg
        elif c[i]<c[i-1]:
            cd[i]=cd[i-1]+1; cu[i]=0
            cc[i]=chg if cu[i-1]>0 else cc[i-1]+chg
        else:
            cu[i]=cu[i-1]; cd[i]=cd[i-1]; cc[i]=cc[i-1]
    df['cu']=cu; df['cd']=cd; df['cc']=cc
    return df

def fetch(sym, n=300):
    r = requests.get("https://api.binance.com/api/v3/klines",
        params={'symbol':sym,'interval':TF,'limit':n}, timeout=10)
    r.raise_for_status()
    df = pd.DataFrame(r.json(), columns=['ts','open','high','low','close','vol','ct','qv','tr','tbb','tbq','ign'])
    for c in ['open','high','low','close']: df[c]=df[c].astype(float)
    return compute(df)

def get_signal(sym, df):
    if len(df) < 220: return None
    row = df.iloc[-2]
    adx = float(row['adx']) if not np.isnan(row['adx']) else 0
    atr = float(row['atr']) if not np.isnan(row['atr']) else 0
    if adx < CFG[sym]['adx_th'] or atr <= 0: return None
    entry = float(df.iloc[-1]['close'])
    cu=int(row['cu']); cd=int(row['cd']); cc=float(row['cc'])
    ema=float(row['ema200']) if not np.isnan(row['ema200']) else 0
    c = CFG[sym]
    if cu >= c['sc'] and cc >= c['ccp']:
        return dict(dir='SHORT', entry=entry,
                    sl=entry+c['sl_m']*atr, tp=entry-c['tp_m']*atr,
                    adx=adx, atr=atr, cu=cu, cd=cd, cc=cc)
    if not c['no_long'] and cd >= c['lc'] and cc <= -c['ccp'] and (c.get('no_ema', False) or entry > ema):
        return dict(dir='LONG', entry=entry,
                    sl=entry-c['sl_m']*atr, tp=entry+c['tp_m']*atr,
                    adx=adx, atr=atr, cu=cu, cd=cd, cc=cc)
    return None

def chk_exit(pos, hi, lo):
    if pos['dir']=='SHORT':
        if lo<=pos['tp']: return '止盈', pos['tp']
        if hi>=pos['sl']: return '止损', pos['sl']
    else:
        if hi>=pos['tp']: return '止盈', pos['tp']
        if lo<=pos['sl']:  return '止损', pos['sl']
    return None, None

def ld_state():
    if STATE_F.exists(): return json.loads(STATE_F.read_text())
    return dict(pos={}, equity=CAPITAL, day_loss=0., day_date='',
                wins=0, losses=0, pnl=0., n=0)

def sv_state(s): STATE_F.write_text(json.dumps(s, ensure_ascii=False, indent=2))

def ld_trades():
    if TRADE_F.exists(): return json.loads(TRADE_F.read_text())
    return []

def sv_trades(t): TRADE_F.write_text(json.dumps(t, ensure_ascii=False, indent=2))

# ── 主循环 ───────────────────────────────────────────────────
def main():
    global _run
    st = ld_state(); trades = ld_trades()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if st['day_date'] != today: st['day_loss']=0.; st['day_date']=today

    log.info("="*65)
    log.info("白夜纸交易 FINAL — 1m实时数据 | 全Bug修复版")
    log.info(f"品种={SYMS}")
    log.info(f"资金={CAPITAL}U  风险={RISK_PCT*100}%(={CAPITAL*RISK_PCT:.1f}U/笔)  目标={TARGET}笔")
    log.info("="*65)
    log.info(f"恢复: 权益={st['equity']:.2f}U  持仓={list(st['pos'].keys())}  已完={st['n']}笔")

    cooldown={}; last_bar={}; poll=0

    while _run:
        try:
            poll += 1
            now = datetime.now(timezone.utc)
            today = now.strftime('%Y-%m-%d')
            if st['day_date'] != today: st['day_loss']=0.; st['day_date']=today

            if st['day_loss'] >= st['equity']*DAILY_LOSS_MAX:
                log.warning(f"⛔ 日熔断 day_loss={st['day_loss']:.2f}U")
                time.sleep(60); continue

            # 抓数据（每品种一次，复用）
            data = {}
            for sym in SYMS:
                try: data[sym] = fetch(sym)
                except Exception as e: log.warning(f"fetch {sym}: {e}")

            # ── 检查持仓退出 ──────────────────────────────────
            for sym in list(st['pos'].keys()):
                pos = st['pos'][sym]
                df = data.get(sym)
                if df is None: continue
                cur = df.iloc[-1]
                result, ep = chk_exit(pos, float(cur['high']), float(cur['low']))
                if not result: continue

                qty = pos['qty']
                raw = (pos['entry']-ep)*qty if pos['dir']=='SHORT' else (ep-pos['entry'])*qty
                fee = (pos['entry']+ep)*qty*FEE
                net = raw - fee
                st['equity'] += net
                if net > 0: st['wins'] += 1
                else: st['losses'] += 1; st['day_loss'] += abs(net)
                st['pnl'] = round(st.get('pnl',0)+net, 4)
                st['n'] += 1
                del st['pos'][sym]

                trades.append(dict(
                    no=st['n'], sym=sym, dir=pos['dir'],
                    entry=round(pos['entry'],6), exit=round(ep,6),
                    qty=round(qty,6), result=result,
                    net_pnl=round(net,4), equity=round(st['equity'],4),
                    open_t=pos['open_t'], close_t=now.isoformat(),
                    adx=pos['adx'], atr=pos['atr'],
                ))
                sv_trades(trades); sv_state(st)

                W=st['wins']; L=st['losses']; tot=W+L
                wr=W/tot*100 if tot>0 else 0
                emoji="✅" if net>0 else "❌"
                log.info(
                    f"{emoji} #{st['n']} 平仓 {sym} {pos['dir']} {result} "
                    f"入={pos['entry']:.5g} 出={ep:.5g} PnL={net:+.4f}U | "
                    f"WR={wr:.0f}% 累计PnL={st['pnl']:+.3f}U 权益={st['equity']:.2f}U"
                )
                if st['n'] >= TARGET:
                    log.info(f"🎯 目标达成！{TARGET}笔完成")

            # ── 扫描开仓信号 ──────────────────────────────────
            if len(st['pos']) < MAX_POS:
                for sym in SYMS:
                    if sym in st['pos']: continue
                    if poll - cooldown.get(sym,0) < 3: continue
                    df = data.get(sym)
                    if df is None: continue
                    bt = int(df.iloc[-2]['ts'])
                    if last_bar.get(sym) == bt: continue

                    sig = get_signal(sym, df)
                    if not sig: continue

                    last_bar[sym] = bt
                    cooldown[sym] = poll
                    e = sig['entry']; atr = sig['atr']

                    # ✅ 正确仓位计算: qty = risk / (sl_atr * atr_price)
                    sl_dist = abs(sig['sl'] - e)   # = sl_m * atr
                    if sl_dist <= 0: continue
                    risk_u = st['equity'] * RISK_PCT
                    qty = risk_u / sl_dist           # qty使得损失=risk_u
                    notional = qty * e

                    log.info(
                        f"🔔 #{st['n']+len(st['pos'])+1} 开仓 {sym} {sig['dir']} "
                        f"入={e:.5g} TP={sig['tp']:.5g} SL={sig['sl']:.5g} | "
                        f"ADX={sig['adx']:.1f} cu={sig['cu']} cd={sig['cd']} cc={sig['cc']*100:.3f}% "
                        f"qty={qty:.4f} 名义={notional:.2f}U"
                    )

                    st['pos'][sym] = dict(
                        dir=sig['dir'], entry=e, sl=sig['sl'], tp=sig['tp'],
                        qty=qty, notional=round(notional,2),
                        adx=round(sig['adx'],1), atr=round(sig['atr'],6),
                        open_t=now.isoformat(), bar_ts=bt,
                    )
                    sv_state(st)
                    if len(st['pos']) >= MAX_POS: break

            # ── 状态输出 ──────────────────────────────────────
            W=st['wins']; L=st['losses']; tot=W+L
            wr=W/tot*100 if tot>0 else 0
            open_s=[f"{s}({v['dir']}@{v['entry']:.4g})" for s,v in st['pos'].items()]
            log.info(
                f"[{now.strftime('%H:%M:%S')}] 完成={tot}/{TARGET} WR={wr:.0f}% "
                f"PnL={st['pnl']:+.3f}U 权益={st['equity']:.2f}U | "
                f"持仓={open_s if open_s else '无'}"
            )

            # 无持仓时显示信号距离
            if not st['pos']:
                for sym in SYMS:
                    df = data.get(sym)
                    if df is None: continue
                    row = df.iloc[-2]
                    adx=float(row['adx']) if not np.isnan(row['adx']) else 0
                    cu=int(row['cu']); cd=int(row['cd']); cc=float(row['cc'])*100
                    c=CFG[sym]
                    short_ok = "✅" if cu>=c['sc'] and cc/100>=c['ccp'] else f"cu{cu}/{c['sc']}|cc{cc:.3f}%"
                    long_ok  = "✅" if cd>=c['lc'] and cc/100<=-c['ccp'] else f"cd{cd}/{c['lc']}|cc{cc:.3f}%"
                    log.info(f"  {sym}: ADX={adx:.0f} | S=[{short_ok}] L=[{long_ok}]")

            time.sleep(POLL_SECS)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"异常: {e}", exc_info=True)
            time.sleep(15)

    sv_state(st)
    W=st['wins']; L=st['losses']; tot=W+L
    wr=W/tot*100 if tot>0 else 0
    log.info(f"退出汇总: 完成={tot}笔 WR={wr:.1f}% PnL={st['pnl']:+.4f}U 权益={st['equity']:.4f}U")

if __name__ == "__main__":
    main()
