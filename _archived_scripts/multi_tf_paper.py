#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
白夜多周期纸交易引擎 v1.0
- 周期: 3m / 5m / 15m / 30m 同时运行
- 品种: 8个（BTC/ETH/SOL/BNB/LINK/LTC/AVAX/ADA）
- 信号: 连涨/连跌动量反转 + ADX过滤
- 出场: 止盈/止损（Wilder ATR）
- 报告: 每5分钟打印一次完整状态
"""
import json, time, requests, numpy as np, pandas as pd, warnings
from pathlib import Path
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
import logging
warnings.filterwarnings("ignore")

# ── 配置 ─────────────────────────────────────────────────────────
CAPITAL       = 150.0
RISK_PCT      = 0.02
FEE           = 0.0009
MAX_POS_TOTAL = 6        # 全部周期同时最多6个持仓
MAX_NOTIONAL  = 200.0    # 单笔名义仓位上限200U（防止BTC/高价币过度杠杆）
MIN_TP_FEE_RATIO = 3.0   # 止盈收益至少是手续费的3倍，否则不开单
REPORT_EVERY  = 300      # 秒：每5分钟打印报告
LOG_FILE      = Path("logs/multi_tf.log")
STATE_FILE    = Path("logs/multi_tf_state.json")
TRADES_FILE   = Path("trades_multi_tf.json")
LOG_FILE.parent.mkdir(exist_ok=True)

_h = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=3, encoding='utf-8')
_h.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
_c = logging.StreamHandler()
_c.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
logger = logging.getLogger('multi_tf')
logger.addHandler(_h); logger.addHandler(_c)
logger.setLevel(logging.INFO)

# ── 多周期参数配置 ────────────────────────────────────────────────
# 格式: (sc, lc, ccp, adx_th, tp_s, tp_l, sl_atr, long_disabled)
TF_CONFIGS = {
    "3m": {
        "poll_secs": 30,
        "kline_limit": 300,
        "symbols": {
            "BTCUSDT":  (5, 5, 0.0008, 15, 0.8, 0.8, 1.5, False),
            "ETHUSDT":  (4, 4, 0.0008, 12, 0.8, 0.8, 1.5, False),
            "SOLUSDT":  (4, 4, 0.001,  12, 0.8, 0.8, 1.5, False),
            "BNBUSDT":  (5, 5, 0.0008, 15, 0.8, 0.8, 1.5, True),
            "LTCUSDT":  (4, 4, 0.001,  12, 0.8, 0.8, 1.5, False),
            "AVAXUSDT": (4, 5, 0.001,  12, 0.8, 0.8, 1.5, False),
        }
    },
    "5m": {
        "poll_secs": 60,
        "kline_limit": 300,
        "symbols": {
            "BTCUSDT":  (5, 4, 0.001,  18, 0.8, 0.7, 1.5, False),
            "ETHUSDT":  (4, 5, 0.001,  15, 0.8, 0.7, 1.5, False),
            "SOLUSDT":  (4, 4, 0.0015, 15, 0.8, 0.8, 1.5, False),
            "BNBUSDT":  (5, 5, 0.001,  15, 0.8, 0.8, 1.5, True),
            "LINKUSDT": (5, 4, 0.0015, 15, 0.8, 0.7, 1.5, False),
            "ADAUSDT":  (4, 5, 0.001,  15, 0.8, 0.7, 1.5, False),
        }
    },
    "15m": {
        "poll_secs": 60,
        "kline_limit": 300,
        "symbols": {
            "BTCUSDT":  (7, 5, 0.002,  22, 0.6, 0.5, 1.5, False),
            "ETHUSDT":  (6, 6, 0.001,  20, 0.5, 0.5, 1.5, False),
            "SOLUSDT":  (6, 4, 0.0015, 28, 0.5, 0.5, 1.5, False),
            "BNBUSDT":  (6, 6, 0.001,  15, 0.6, 0.8, 1.5, True),
            "LINKUSDT": (7, 4, 0.002,  18, 0.7, 0.6, 1.5, False),
            "LTCUSDT":  (5, 5, 0.003,  25, 0.5, 0.5, 1.5, False),
        }
    },
    "30m": {
        "poll_secs": 120,
        "kline_limit": 200,
        "symbols": {
            "BTCUSDT":  (5, 5, 0.003,  20, 0.7, 0.6, 1.5, False),
            "ETHUSDT":  (4, 5, 0.003,  18, 0.7, 0.6, 1.5, False),
            "SOLUSDT":  (5, 4, 0.002,  25, 0.7, 0.7, 1.5, False),
            "BNBUSDT":  (5, 5, 0.002,  15, 0.7, 0.8, 1.5, True),
            "AVAXUSDT": (4, 5, 0.002,  20, 0.7, 0.6, 1.5, False),
        }
    },
}

# ── 指标（Wilder平滑，与回测引擎一致）───────────────────────────
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
    up = np.diff(h, prepend=h[0]); dn = np.diff(l, prepend=l[0])*-1
    pdm = np.where((up>dn)&(up>0), up, 0.0)
    ndm = np.where((dn>up)&(dn>0), dn, 0.0)
    atr14 = _wilder(tr, 14)
    pdi = 100*_wilder(pdm,14)/np.where(atr14>0,atr14,np.nan)
    ndi = 100*_wilder(ndm,14)/np.where(atr14>0,atr14,np.nan)
    dx  = 100*np.abs(pdi-ndi)/np.where((pdi+ndi)>0,pdi+ndi,np.nan)
    df['adx'] = _wilder(dx, 14)
    cu=np.zeros(n,int); cd=np.zeros(n,int); cc=np.zeros(n)
    for i in range(1,n):
        if c[i]>c[i-1]:
            cu[i]=cu[i-1]+1; cd[i]=0
            cc[i]=cc[i-1]+(c[i]-c[i-1])/c[i-1] if cu[i]>1 else (c[i]-c[i-1])/c[i-1]
        elif c[i]<c[i-1]:
            cd[i]=cd[i-1]+1; cu[i]=0
            cc[i]=cc[i-1]+(c[i]-c[i-1])/c[i-1] if cd[i]>1 else (c[i]-c[i-1])/c[i-1]
        else:
            cu[i]=cu[i-1]; cd[i]=cd[i-1]; cc[i]=cc[i-1]
    df['cu']=cu; df['cd']=cd; df['cc']=cc
    return df

def fetch(sym, tf, limit):
    url = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={tf}&limit={limit}"
    r = requests.get(url, timeout=10); r.raise_for_status()
    df = pd.DataFrame(r.json(), columns=['ts','open','high','low','close','volume','ct','qv','t','tb','tq','ig'])
    for c in ['open','high','low','close','volume']: df[c]=df[c].astype(float)
    df['ts']=pd.to_datetime(df['ts'],unit='ms',utc=True)
    return df.set_index('ts').sort_index()[['open','high','low','close','volume']]

def check_signal(df, sc, lc, ccp, adx_th, tp_s, tp_l, sl_atr, long_disabled):
    if len(df) < 220: return None
    row = df.iloc[-2]
    adx = row['adx']; atr = row['atr']
    if np.isnan(adx) or np.isnan(atr) or atr<=0: return None
    if adx < adx_th: return None
    entry = float(df.iloc[-1]['open'])
    # 做空信号
    if row['cu'] >= sc and row['cc'] >= ccp:
        sl = entry + sl_atr * atr
        tp = entry - tp_s * atr
        return ('做空', entry, sl, tp, adx, atr)
    # 做多信号
    if not long_disabled and row['cd'] >= lc and row['cc'] <= -ccp and row['close'] > row['ema200']:
        sl = entry - sl_atr * atr
        tp = entry + tp_l * atr
        return ('做多', entry, sl, tp, adx, atr)
    return None

# ── 状态管理 ─────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"equity": CAPITAL, "positions": {}, "last_bar": {}}

def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False))

def load_trades():
    if TRADES_FILE.exists():
        return json.loads(TRADES_FILE.read_text())
    return []

def save_trades(t):
    TRADES_FILE.write_text(json.dumps(t, indent=2, ensure_ascii=False))

def print_report(state, trades, tf_stats):
    closed = [t for t in trades if t.get('status')=='closed']
    wins   = [t for t in closed if t.get('pnl',0)>0]
    pnl    = sum(t.get('pnl',0) for t in closed)
    now    = datetime.now(timezone.utc).strftime('%m-%d %H:%M UTC')
    logger.info("="*65)
    logger.info(f"  白夜多周期纸交易报告  [{now}]")
    logger.info("="*65)
    logger.info(f"  账户权益: {state['equity']:.4f}U  |  起始: {CAPITAL}U  |  盈亏: {state['equity']-CAPITAL:+.4f}U")
    logger.info(f"  已关闭: {len(closed)}笔  |  胜率: {len(wins)/len(closed)*100:.1f}% ({len(wins)}/{len(closed)})  |  总盈亏: {pnl:+.4f}U" if closed else f"  已关闭: 0笔（等待信号）")
    logger.info(f"  当前持仓: {len(state['positions'])}个  |  最大允许: {MAX_POS_TOTAL}个")
    logger.info("-"*65)
    if closed:
        logger.info(f"  {'周期':4s} {'品种':10s} {'方向':4s} {'结果':6s} {'盈亏':>9s} {'时间'}")
        for t in closed[-15:]:
            tag = '✅止盈' if t.get('pnl',0)>0 else '❌止损'
            ts  = t.get('open_time','')[:16].replace('T',' ')
            logger.info(f"  {t['tf']:4s} {t['sym']:10s} {t['dir']:4s} {tag:6s} {t['pnl']:+9.4f}U  {ts}")
    if state['positions']:
        logger.info("-"*65)
        logger.info("  当前持仓:")
        for key, pos in state['positions'].items():
            try:
                r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={pos['sym']}", timeout=5).json()
                cur = float(r['price'])
                entry=pos['entry']; tp=pos['tp']; sl=pos['sl']
                if pos['dir']=='做空':
                    unreal=(entry-cur)/entry*pos['notional']
                    prog=(entry-cur)/(entry-tp)*100 if (entry-tp)!=0 else 0
                else:
                    unreal=(cur-entry)/entry*pos['notional']
                    prog=(cur-entry)/(tp-entry)*100 if (tp-entry)!=0 else 0
                logger.info(f"  [{pos['tf']}] {pos['sym']:10s} {pos['dir']:4s} | 入={entry:.4f} 现={cur:.4f} | TP={tp:.4f} SL={sl:.4f}")
                logger.info(f"       未实现={unreal:+.4f}U | TP进度={prog:.1f}% | ADX={pos['adx']:.1f}")
            except:
                logger.info(f"  [{pos['tf']}] {pos['sym']} {pos['dir']} (价格获取失败)")
    logger.info("="*65)

# ── 单周期扫描 ────────────────────────────────────────────────────
def scan_tf(tf, state, trades):
    cfg = TF_CONFIGS[tf]
    limit = cfg['kline_limit']
    opened = 0; closed_n = 0
    now = datetime.now(timezone.utc)

    for sym, params in cfg['symbols'].items():
        sc,lc,ccp,adx_th,tp_s,tp_l,sl_atr,long_dis = params
        pos_key = f"{tf}:{sym}"

        # 检查已有持仓
        if pos_key in state['positions']:
            pos = state['positions'][pos_key]
            try:
                df = compute(fetch(sym, tf, min(limit,100)))
                cur = float(df.iloc[-1]['close'])
                entry=pos['entry']; tp=pos['tp']; sl=pos['sl']
                hit = None
                if pos['dir']=='做空':
                    if cur <= tp: hit='止盈'
                    elif cur >= sl: hit='止损'
                    exit_p = tp if hit=='止盈' else sl if hit else cur
                    pnl_calc = (entry-exit_p)/entry*pos['notional'] - pos['notional']*FEE*2
                else:
                    if cur >= tp: hit='止盈'
                    elif cur <= sl: hit='止损'
                    exit_p = tp if hit=='止盈' else sl if hit else cur
                    pnl_calc = (exit_p-entry)/entry*pos['notional'] - pos['notional']*FEE*2
                if hit:
                    state['equity'] += pnl_calc
                    pos.update(status='closed', exit=exit_p, pnl=round(pnl_calc,4), exit_reason=hit)
                    trades.append(pos)
                    del state['positions'][pos_key]
                    icon = '✅' if hit=='止盈' else '❌'
                    logger.info(f"{icon} [{tf}] {sym} {pos['dir']} {hit} | 入={entry:.4f} 出={exit_p:.4f} | 盈亏={pnl_calc:+.4f}U | 权益={state['equity']:.4f}U")
                    closed_n += 1
            except Exception as e:
                logger.warning(f"[{tf}] {sym} 持仓检查失败: {e}")
            continue

        # 扫描新信号（仓位未满）
        if len(state['positions']) >= MAX_POS_TOTAL:
            continue
        try:
            df = compute(fetch(sym, tf, limit))
            sig = check_signal(df, sc, lc, ccp, adx_th, tp_s, tp_l, sl_atr, long_dis)
            if not sig: continue
            direction, entry, sl, tp, adx_val, atr_val = sig
            # K线去重：同一根K线不重复开仓
            bar_ts = str(df.index[-2])
            bar_key = f"{tf}:{sym}:{direction}"
            if state.get('last_bar', {}).get(bar_key) == bar_ts:
                continue
            # 计算仓位（含名义仓位上限保护）
            risk_amt = state['equity'] * RISK_PCT
            sl_dist  = abs(entry - sl)
            if sl_dist <= 0: continue
            qty      = risk_amt / sl_dist
            notional = qty * entry
            # 名义仓位超限时等比缩减
            if notional > MAX_NOTIONAL:
                qty      = MAX_NOTIONAL / entry
                notional = MAX_NOTIONAL
            # 止盈收益必须覆盖手续费3倍
            tp_dist  = abs(entry - tp)
            tp_profit = tp_dist / entry * notional
            fee_cost  = notional * FEE * 2
            if tp_profit < fee_cost * MIN_TP_FEE_RATIO:
                logger.debug(f'[{tf}] {sym} 止盈收益{tp_profit:.3f}U < 手续费{fee_cost:.3f}Ux3，跳过')
                continue
            pos = dict(
                tf=tf, sym=sym, dir=direction, entry=entry, sl=sl, tp=tp,
                qty=round(qty,6), notional=round(notional,4),
                adx=round(adx_val,2), atr=round(atr_val,8),
                open_time=now.isoformat(), status='open', pnl=0
            )
            state['positions'][pos_key] = pos
            state.setdefault('last_bar',{})[bar_key] = bar_ts
            logger.info(f"🔔 [{tf}] {sym} {direction} | 入={entry:.4f} TP={tp:.4f} SL={sl:.4f} | ADX={adx_val:.1f} 名义={notional:.1f}U")
            opened += 1
        except Exception as e:
            logger.warning(f"[{tf}] {sym} 扫描失败: {e}")

    return opened, closed_n

# ── 主循环 ────────────────────────────────────────────────────────
def main():
    logger.info("="*65)
    logger.info("  白夜多周期纸交易引擎 v1.0 启动")
    logger.info(f"  周期: 3m / 5m / 15m / 30m")
    logger.info(f"  品种: BTC ETH SOL BNB LINK LTC AVAX ADA")
    logger.info(f"  资金: {CAPITAL}U | 单笔风险: {RISK_PCT*100:.0f}% | 最大持仓: {MAX_POS_TOTAL}个")
    logger.info("="*65)

    state  = load_state()
    trades = load_trades()
    last_report = 0
    last_scan   = {tf: 0 for tf in TF_CONFIGS}
    tf_stats    = {tf: {'opened':0,'closed':0} for tf in TF_CONFIGS}

    # 扫描间隔（秒）
    poll = {'3m':30, '5m':60, '15m':60, '30m':120}

    while True:
        try:
            now_ts = time.time()

            for tf in TF_CONFIGS:
                if now_ts - last_scan[tf] >= poll[tf]:
                    o, c = scan_tf(tf, state, trades)
                    tf_stats[tf]['opened'] += o
                    tf_stats[tf]['closed'] += c
                    last_scan[tf] = now_ts
                    save_state(state)
                    save_trades(trades)

            # 每5分钟完整报告
            if now_ts - last_report >= REPORT_EVERY:
                print_report(state, trades, tf_stats)
                last_report = now_ts

            time.sleep(10)

        except KeyboardInterrupt:
            logger.info("引擎已停止")
            print_report(state, trades, tf_stats)
            break
        except Exception as e:
            logger.error(f"主循环错误: {e}", exc_info=True)
            time.sleep(15)

if __name__ == '__main__':
    main()
