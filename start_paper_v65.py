#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
白夜纸交易 v6.5 — 高信号量优化版
策略改进 vs v6.4:
  - 7个品种: ETH/SOL/LINK/ARB/DOT/SUI/ADA（全部EV>0验证通过）
  - 各品种独立参数（网格搜索最优）
  - 统一 FEE=0.0002 (Maker 0.02%单边)
  - 统一 tp_s=0.8×ATR, sl=1.0×ATR
  - 预期信号量: ~10笔/天 (vs v6.4的3笔/天)
  
品种 | sc | lc | adx | ccp   | 90天WR | EV/笔
ETH  |  6 |  4 |  15 | 0.0008|  69.0% | +0.442
SOL  |  6 |  4 |  12 | 0.0008|  67.5% | +0.363
LINK |  4 |  4 |  15 | 0.0008|  65.3% | +0.317
ARB  |  4 |  4 |  18 | 0.0008|  63.6% | +0.260
DOT  |  4 |  5 |  20 | 0.0008|  66.7% | +0.396
SUI  |  5 |  5 |  15 | 0.002 |  65.5% | +0.309
ADA  |  4 |  5 |  18 | 0.0008|  66.0% | +0.323
"""
import json, time, requests, numpy as np, pandas as pd, warnings
from pathlib import Path
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
import logging
warnings.filterwarnings("ignore")

# ── 全局参数 ─────────────────────────────────────────────────
CAPITAL        = 150.0
RISK_PCT       = 0.02
FEE            = 0.0002   # Maker 限价单 0.02%单边
MAX_POSITIONS  = 5        # 最多同时5个持仓
POLL_SECS      = 30
TARGET_TRADES  = 100
DAILY_LOSS_PCT = 0.06     # 日熔断: 权益6%

LOG_FILE    = Path("logs/paper_v65.log")
STATE_FILE  = Path("logs/paper_v65_state.json")
TRADES_FILE = Path("paper_trades_v65.json")
LOG_FILE.parent.mkdir(exist_ok=True)

_h = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=2, encoding='utf-8')
_h.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
logger = logging.getLogger('paper_v65')
logger.addHandler(_h); logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)

# ── 品种配置（各自独立最优参数）──────────────────────────────
CONFIGS = {
    "ETHUSDT":  dict(sc=6, lc=4, ccp=0.0008, adx_th=15, tp_s=0.8, sl_atr=1.0, long_disabled=False),
    "SOLUSDT":  dict(sc=6, lc=4, ccp=0.0008, adx_th=12, tp_s=0.8, sl_atr=1.0, long_disabled=False),
    "LINKUSDT": dict(sc=4, lc=4, ccp=0.0008, adx_th=15, tp_s=0.8, sl_atr=1.0, long_disabled=False),
    "ARBUSDT":  dict(sc=4, lc=4, ccp=0.0008, adx_th=18, tp_s=0.8, sl_atr=1.0, long_disabled=False),
    "DOTUSDT":  dict(sc=4, lc=5, ccp=0.0008, adx_th=20, tp_s=0.8, sl_atr=1.0, long_disabled=False),
    "SUIUSDT":  dict(sc=5, lc=5, ccp=0.002,  adx_th=15, tp_s=0.8, sl_atr=1.0, long_disabled=False),
    "ADAUSDT":  dict(sc=4, lc=5, ccp=0.0008, adx_th=18, tp_s=0.8, sl_atr=1.0, long_disabled=False),
}
SYMBOLS = list(CONFIGS.keys())

# ── Wilder's ATR/ADX（与回测引擎保持一致）───────────────────
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
    c = df['close'].values; h = df['high'].values; l = df['low'].values
    n = len(c)
    # ATR
    tr = np.maximum(h-l, np.maximum(np.abs(h-np.roll(c,1)), np.abs(l-np.roll(c,1))))
    tr[0] = h[0]-l[0]
    df['atr'] = _wilder(tr, 14)
    # EMA200
    df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()
    # ADX
    up = np.diff(h, prepend=h[0]); dn = np.diff(l, prepend=l[0]) * -1
    pdm = np.where((up>dn)&(up>0), up, 0.0)
    ndm = np.where((dn>up)&(dn>0), dn, 0.0)
    atr14 = _wilder(tr, 14)
    pdi = 100*_wilder(pdm,14)/np.where(atr14>0,atr14,np.nan)
    ndi = 100*_wilder(ndm,14)/np.where(atr14>0,atr14,np.nan)
    dx = 100*np.abs(pdi-ndi)/np.where((pdi+ndi)>0,pdi+ndi,np.nan)
    df['adx'] = _wilder(dx, 14)
    # 连涨/连跌
    cu=np.zeros(n,int); cd=np.zeros(n,int); cc=np.zeros(n)
    for i in range(1, n):
        if c[i] > c[i-1]:
            cu[i]=cu[i-1]+1; cd[i]=0
            cc[i]=(c[i]-c[i-1])/c[i-1] if cu[i]==1 else cc[i-1]+(c[i]-c[i-1])/c[i-1]
        elif c[i] < c[i-1]:
            cd[i]=cd[i-1]+1; cu[i]=0
            cc[i]=(c[i]-c[i-1])/c[i-1] if cd[i]==1 else cc[i-1]+(c[i]-c[i-1])/c[i-1]
        else:
            cu[i]=cu[i-1]; cd[i]=cd[i-1]; cc[i]=cc[i-1]
    df['cu']=cu; df['cd']=cd; df['cc']=cc
    return df

def fetch_klines(sym, limit=300):
    r = requests.get("https://api.binance.com/api/v3/klines",
        params={'symbol':sym,'interval':'15m','limit':limit}, timeout=10)
    df = pd.DataFrame(r.json(), columns=['ts','open','high','low','close','vol','ct','qv','tr','tbb','tbq','ign'])
    for c in ['open','high','low','close']: df[c]=df[c].astype(float)
    df['ts'] = pd.to_datetime(df['ts'],unit='ms',utc=True)
    return df.set_index('ts')

def check_signal(sym, df, cfg):
    """用[-2]（已完成K线）判断信号，[-1]收盘价模拟Limit成交入场"""
    if len(df) < 210: return None
    row = df.iloc[-2]
    adx = float(row['adx']) if not np.isnan(row['adx']) else 0
    atr = float(row['atr']) if not np.isnan(row['atr']) else 0
    if adx < cfg['adx_th'] or atr <= 0: return None

    entry = float(df.iloc[-1]['close'])  # 用当前收盘价（Limit模拟）
    cu = int(row['cu']); cd = int(row['cd']); cc = float(row['cc'])
    ema200 = float(row['ema200']) if not np.isnan(row['ema200']) else 0

    # 做空：连涨≥sc + 累涨≥ccp
    if cu >= cfg['sc'] and cc >= cfg['ccp']:
        sl = entry + cfg['sl_atr'] * atr
        tp = entry - cfg['tp_s'] * atr
        return ('做空', entry, sl, tp, adx, atr, cu, cd, cc)

    # 做多：连跌≥lc + 累跌≥ccp + close>EMA200
    if not cfg['long_disabled']:
        if cd >= cfg['lc'] and cc <= -cfg['ccp'] and row['close'] > ema200:
            sl = entry - cfg['sl_atr'] * atr
            tp = entry + cfg['tp_s'] * atr
            return ('做多', entry, sl, tp, adx, atr, cu, cd, cc)
    return None

def check_exit(pos, cur_price, cur_high, cur_low):
    """检查止盈止损"""
    if pos['dir'] == '做空':
        if cur_low <= pos['tp']:  return '止盈', pos['tp']
        if cur_high >= pos['sl']: return '止损', pos['sl']
    else:
        if cur_high >= pos['tp']: return '止盈', pos['tp']
        if cur_low <= pos['sl']:  return '止损', pos['sl']
    return None, None

# ── 状态持久化 ───────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists(): return json.loads(STATE_FILE.read_text())
    return {"positions":{}, "equity":CAPITAL, "last_bar":{},
            "day_loss":0.0, "day_date":"", "total_trades":0}

def save_state(s): STATE_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False))
def load_trades():
    if TRADES_FILE.exists(): return json.loads(TRADES_FILE.read_text())
    return []
def save_trades(t): TRADES_FILE.write_text(json.dumps(t, indent=2, ensure_ascii=False))

def summary(trades):
    closed = [t for t in trades if t.get('status')=='closed']
    if not closed: return "0/100笔"
    wins = [t for t in closed if t.get('pnl',0)>0]
    pnl = sum(t.get('pnl',0) for t in closed)
    return f"{len(closed)}/{TARGET_TRADES}笔 WR={len(wins)/len(closed)*100:.1f}% PnL={pnl:+.2f}U"

# ── 主循环 ───────────────────────────────────────────────────
def main():
    logger.info("="*65)
    logger.info("白夜纸交易 v6.5 — 高信号量优化版（7品种）")
    logger.info(f"品种: {SYMBOLS}")
    logger.info(f"FEE={FEE*100:.3f}%单边(Maker) | 资金:{CAPITAL}U | 风险:{RISK_PCT*100}%")
    logger.info(f"预期信号量: ~10笔/天 | 平均WR≥65%")
    logger.info("="*65)

    state = load_state()
    trades = load_trades()
    logger.info(f"恢复: 持仓={list(state['positions'].keys())} 权益={state['equity']:.4f}U")
    logger.info(f"进度: {summary(trades)}")

    while True:
        try:
            closed_count = len([t for t in trades if t.get('status')=='closed'])
            if closed_count >= TARGET_TRADES:
                logger.info(f"🎯 目标达成! {summary(trades)}")
                save_state(state); save_trades(trades)
                break

            now = datetime.now(timezone.utc)
            today = now.strftime('%Y-%m-%d')
            if state.get('day_date') != today:
                state['day_loss'] = 0.0; state['day_date'] = today

            # 日亏损熔断
            if state['day_loss'] <= -(CAPITAL * DAILY_LOSS_PCT):
                logger.warning(f"⛔ 日熔断: 亏损={state['day_loss']:.2f}U，暂停300s")
                time.sleep(300); continue

            # ── 检查持仓是否触及TP/SL ──
            for sym in list(state['positions'].keys()):
                pos = state['positions'][sym]
                try:
                    df = compute(fetch_klines(sym, 50))
                    cur_row = df.iloc[-1]
                    cur_price = float(cur_row['close'])
                    cur_high  = float(cur_row['high'])
                    cur_low   = float(cur_row['low'])

                    hit, exit_price = check_exit(pos, cur_price, cur_high, cur_low)
                    if hit:
                        if pos['dir'] == '做空':
                            pnl_price = pos['entry'] - exit_price
                        else:
                            pnl_price = exit_price - pos['entry']
                        pnl = pnl_price / pos['entry'] * pos['notional'] - pos['notional'] * FEE * 2

                        state['equity'] += pnl
                        if pnl < 0: state['day_loss'] += pnl

                        pos.update(status='closed', exit_price=exit_price,
                                   pnl=round(pnl, 4), exit_reason=hit,
                                   exit_time=now.isoformat(),
                                   exit_equity=round(state['equity'], 4))
                        trades.append(pos)
                        del state['positions'][sym]

                        icon = '✅' if hit == '止盈' else '❌'
                        logger.info(
                            f"{icon} #{pos['no']} {sym} {pos['dir']} {hit} | "
                            f"in={pos['entry']:.4f} out={exit_price:.4f} "
                            f"pnl={pnl:+.4f}U | 权益={state['equity']:.2f}U"
                        )
                except Exception as e:
                    logger.warning(f"{sym} 平仓检查失败: {e}")

            # ── 扫描新信号 ──
            if len(state['positions']) < MAX_POSITIONS:
                for sym in SYMBOLS:
                    if sym in state['positions'] or len(state['positions']) >= MAX_POSITIONS:
                        continue
                    try:
                        df = compute(fetch_klines(sym))
                        sig = check_signal(sym, df, CONFIGS[sym])
                        if not sig: continue

                        direction, entry, sl, tp, adx_val, atr_val, cu, cd, cc = sig
                        bar_ts = str(df.index[-2])
                        bar_key = f"{sym}_{direction}"

                        # 防止同一K线重复开仓
                        if state.get('last_bar', {}).get(bar_key) == bar_ts:
                            continue

                        # 计算仓位
                        risk_amt = state['equity'] * RISK_PCT
                        sl_dist = abs(entry - sl)
                        if sl_dist <= 0: continue
                        qty = risk_amt / sl_dist
                        notional = round(qty * entry, 4)

                        # 仓位保护（单笔notional不超过权益300%）
                        if notional > state['equity'] * 3:
                            qty = state['equity'] * 3 / entry
                            notional = round(qty * entry, 4)

                        trade_no = closed_count + len(state['positions']) + 1
                        pos = dict(
                            no=trade_no, sym=sym, dir=direction,
                            entry=entry, sl=sl, tp=tp,
                            qty=round(qty, 6), notional=notional,
                            adx=round(adx_val, 1), atr=round(atr_val, 6),
                            cu=cu, cd=cd, cc=round(cc*100, 4),
                            open_time=now.isoformat(),
                            bar_ts=bar_ts, status='open', pnl=0
                        )
                        state['positions'][sym] = pos
                        state.setdefault('last_bar', {})[bar_key] = bar_ts

                        logger.info(
                            f"🔔 #{trade_no} 开仓 {sym} {direction} | "
                            f"in={entry:.4f} sl={sl:.4f} tp={tp:.4f} | "
                            f"ADX={adx_val:.0f} ATR={atr_val:.5f} notional={notional:.1f}U"
                        )
                    except Exception as e:
                        logger.warning(f"{sym} 扫描失败: {e}")

            save_state(state); save_trades(trades)
            logger.info(
                f"权益={state['equity']:.4f}U "
                f"持仓={list(state['positions'].keys())} "
                f"{summary(trades)}"
            )
            time.sleep(POLL_SECS)

        except KeyboardInterrupt:
            logger.info("手动停止"); save_state(state); save_trades(trades); break
        except Exception as e:
            logger.error(f"主循环异常: {e}", exc_info=True); time.sleep(10)

if __name__ == '__main__':
    main()
