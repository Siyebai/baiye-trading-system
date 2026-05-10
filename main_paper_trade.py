#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
白夜纸交易 v6.8 — 稳定守护版（1m实时数据）
修复:
  - cc在方向切换时正确重置
  - 每轮扫描全品种，输出指标状态
  - 信号扫描与状态输出用同一次fetch结果
  - 捕获SIGTERM防止意外退出
"""
import json, time, requests, numpy as np, pandas as pd, warnings, sys, signal
from pathlib import Path
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
import logging
warnings.filterwarnings("ignore")

CAPITAL        = 150.0
RISK_PCT       = 0.02
FEE            = 0.0004
MAX_POSITIONS  = 4
POLL_SECS      = 20
TARGET_TRADES  = 10
DAILY_LOSS_PCT = 0.06
INTERVAL       = "1m"

LOG_FILE    = Path("logs/paper_v68.log")
STATE_FILE  = Path("logs/paper_v68_state.json")
TRADES_FILE = Path("logs/paper_v68_trades.json")
LOG_FILE.parent.mkdir(exist_ok=True)

_h = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=2, encoding='utf-8')
_h.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
logger = logging.getLogger('paper_v68')
logger.addHandler(_h); logger.addHandler(_sh)
logger.setLevel(logging.INFO)

# ── 捕获SIGTERM，优雅退出（不被守护进程误杀）──────────────────
_running = True
def _handle_term(sig, frame):
    global _running
    logger.warning(f"收到信号 {sig}，执行优雅退出...")
    _running = False

signal.signal(signal.SIGTERM, _handle_term)
signal.signal(signal.SIGINT, _handle_term)

CONFIGS = {
    "ETHUSDT":  dict(sc=3, lc=3, ccp=0.0003, adx_th=10, tp_s=0.8, sl_atr=1.0, long_disabled=False),
    "SOLUSDT":  dict(sc=3, lc=3, ccp=0.0003, adx_th=10, tp_s=0.8, sl_atr=1.0, long_disabled=False),
    "LINKUSDT": dict(sc=3, lc=3, ccp=0.0004, adx_th=10, tp_s=0.8, sl_atr=1.0, long_disabled=False),
    "BNBUSDT":  dict(sc=3, lc=3, ccp=0.0003, adx_th=10, tp_s=0.8, sl_atr=1.0, long_disabled=False),
    "DOTUSDT":  dict(sc=3, lc=3, ccp=0.0003, adx_th=10, tp_s=0.8, sl_atr=1.0, long_disabled=False),
    "ADAUSDT":  dict(sc=3, lc=3, ccp=0.0003, adx_th=10, tp_s=0.8, sl_atr=1.0, long_disabled=False),
}
SYMBOLS = list(CONFIGS.keys())

def _wilder(series, n):
    out = np.full(len(series), np.nan)
    if len(series) < n: return out
    out[n-1] = series[:n].mean()
    for i in range(n, len(series)):
        out[i] = out[i-1] * (n-1)/n + series[i] / n
    return out

def compute(df):
    df = df.copy()
    hl = (df['high'] - df['low']).values
    prev_close = df['close'].shift(1).fillna(df['close'].iloc[0]).values
    tr = np.maximum(hl, np.maximum(np.abs(df['high'].values - prev_close),
                                    np.abs(df['low'].values - prev_close)))
    df['atr'] = _wilder(tr, 14)
    pm = np.clip(np.diff(df['high'].values, prepend=df['high'].values[0]), 0, None)
    nm = np.clip(-np.diff(df['low'].values, prepend=df['low'].values[0]), 0, None)
    pdm = np.where(pm > nm, pm, 0.0)
    ndm = np.where(nm > pm, nm, 0.0)
    tr_w = _wilder(tr, 14)
    pdi = np.where(tr_w > 0, _wilder(pdm, 14) / tr_w * 100, 0.0)
    ndi = np.where(tr_w > 0, _wilder(ndm, 14) / tr_w * 100, 0.0)
    dx = np.where((pdi+ndi) > 0, np.abs(pdi-ndi) / (pdi+ndi) * 100, 0.0)
    df['adx'] = _wilder(dx, 14)
    df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()
    closes = df['close'].values
    cu = np.zeros(len(df), dtype=int)
    cd = np.zeros(len(df), dtype=int)
    cc = np.zeros(len(df), dtype=float)
    for i in range(1, len(df)):
        chg = (closes[i] - closes[i-1]) / closes[i-1]
        if closes[i] > closes[i-1]:
            cu[i] = cu[i-1] + 1; cd[i] = 0
            cc[i] = chg if cd[i-1] > 0 else cc[i-1] + chg
        elif closes[i] < closes[i-1]:
            cd[i] = cd[i-1] + 1; cu[i] = 0
            cc[i] = chg if cu[i-1] > 0 else cc[i-1] + chg
        else:
            cu[i]=cu[i-1]; cd[i]=cd[i-1]; cc[i]=cc[i-1]
    df['cu']=cu; df['cd']=cd; df['cc']=cc
    return df

def fetch_klines(sym, limit=300):
    r = requests.get("https://api.binance.com/api/v3/klines",
        params={'symbol':sym,'interval':INTERVAL,'limit':limit}, timeout=10)
    r.raise_for_status()
    df = pd.DataFrame(r.json(), columns=['ts','open','high','low','close','vol','ct','qv','tr','tbb','tbq','ign'])
    for c in ['open','high','low','close']: df[c]=df[c].astype(float)
    return df

def check_signal(sym, df, cfg):
    if len(df) < 220: return None
    row = df.iloc[-2]
    adx = float(row['adx']) if not np.isnan(row['adx']) else 0
    atr = float(row['atr']) if not np.isnan(row['atr']) else 0
    if adx < cfg['adx_th'] or atr <= 0: return None
    entry = float(df.iloc[-1]['close'])
    cu = int(row['cu']); cd = int(row['cd']); cc = float(row['cc'])
    ema200 = float(row['ema200']) if not np.isnan(row['ema200']) else 0
    if cu >= cfg['sc'] and cc >= cfg['ccp']:
        return ('做空', entry, entry + cfg['sl_atr']*atr, entry - cfg['tp_s']*atr, adx, atr, cu, cd, cc)
    if not cfg['long_disabled'] and cd >= cfg['lc'] and cc <= -cfg['ccp'] and entry > ema200:
        return ('做多', entry, entry - cfg['sl_atr']*atr, entry + cfg['tp_s']*atr, adx, atr, cu, cd, cc)
    return None

def check_exit(pos, cur_high, cur_low):
    if pos['dir'] == '做空':
        if cur_low <= pos['tp']:  return '止盈', pos['tp']
        if cur_high >= pos['sl']: return '止损', pos['sl']
    else:
        if cur_high >= pos['tp']: return '止盈', pos['tp']
        if cur_low <= pos['sl']:  return '止损', pos['sl']
    return None, None

def load_state():
    if STATE_FILE.exists(): return json.loads(STATE_FILE.read_text())
    return {"positions":{}, "equity":CAPITAL, "day_loss":0.0,
            "day_date":"", "total_trades":0, "wins":0, "losses":0, "total_pnl":0.0}

def save_state(s): STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2))
def load_trades():
    if TRADES_FILE.exists(): return json.loads(TRADES_FILE.read_text())
    return []
def save_trades(t): TRADES_FILE.write_text(json.dumps(t, ensure_ascii=False, indent=2))

def main():
    global _running
    state = load_state()
    trades = load_trades()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if state.get('day_date') != today:
        state['day_loss'] = 0.0; state['day_date'] = today
    logger.info("="*70)
    logger.info("白夜纸交易 v6.8 — 稳定版 | 币安1m实时数据 | 信号修复")
    logger.info(f"品种: {SYMBOLS}")
    logger.info(f"资金={CAPITAL}U 风险={RISK_PCT*100}% 目标≥{TARGET_TRADES}笔")
    logger.info("="*70)

    cooldown = {}; last_bar = {}; poll_count = 0

    while _running:
        try:
            poll_count += 1
            now = datetime.now(timezone.utc)
            today = now.strftime('%Y-%m-%d')
            if state.get('day_date') != today:
                state['day_loss'] = 0.0; state['day_date'] = today

            if state['day_loss'] >= state['equity'] * DAILY_LOSS_PCT:
                logger.warning("⛔ 日熔断! 停止开仓")
                time.sleep(60); continue

            # 每品种: fetch一次，同时用于退出检查和信号扫描
            scan_results = {}
            for sym in SYMBOLS:
                try:
                    df = compute(fetch_klines(sym, 300))
                    scan_results[sym] = df
                except Exception as e:
                    logger.warning(f"fetch {sym} err: {e}")

            # 检查持仓退出（用刚抓的数据）
            for sym in list(state['positions'].keys()):
                pos = state['positions'][sym]
                df = scan_results.get(sym)
                if df is None: continue
                cur_bar = df.iloc[-1]
                cur_high = float(cur_bar['high']); cur_low = float(cur_bar['low'])
                result, exit_price = check_exit(pos, cur_high, cur_low)
                if result:
                    qty = pos['qty']
                    raw_pnl = (pos['entry'] - exit_price) * qty if pos['dir']=='做空' else (exit_price - pos['entry']) * qty
                    fee_cost = (pos['entry'] + exit_price) * qty * FEE
                    net_pnl = raw_pnl - fee_cost
                    state['equity'] += net_pnl
                    if net_pnl > 0: state['wins'] = state.get('wins',0) + 1
                    else: state['losses'] = state.get('losses',0) + 1; state['day_loss'] += abs(net_pnl)
                    state['total_pnl'] = state.get('total_pnl',0.0) + net_pnl
                    state['total_trades'] = state.get('total_trades',0) + 1
                    del state['positions'][sym]
                    trade_no = state['total_trades']
                    trades.append({
                        "no": trade_no, "sym": sym, "dir": pos['dir'],
                        "entry": pos['entry'], "exit": exit_price,
                        "qty": round(qty,6), "result": result,
                        "net_pnl": round(net_pnl,4),
                        "open_time": pos['open_time'],
                        "close_time": now.isoformat(),
                        "equity_after": round(state['equity'],4),
                        "adx": pos.get('adx'), "atr": pos.get('atr'),
                    })
                    save_trades(trades); save_state(state)
                    wins=state.get('wins',0); total=wins+state.get('losses',0)
                    wr=wins/total*100 if total>0 else 0
                    emoji = "✅" if net_pnl > 0 else "❌"
                    logger.info(
                        f"{emoji} #{trade_no} 平仓 {sym} {pos['dir']} {result} "
                        f"入={pos['entry']:.5g} 出={exit_price:.5g} "
                        f"PnL={net_pnl:+.4f}U | WR={wr:.1f}% 权益={state['equity']:.4f}U"
                    )

            # 扫描新信号
            if len(state['positions']) < MAX_POSITIONS:
                for sym in SYMBOLS:
                    if sym in state['positions']: continue
                    if poll_count - cooldown.get(sym,0) < 5: continue
                    df = scan_results.get(sym)
                    if df is None: continue
                    bar_ts = int(df.iloc[-2]['ts'])
                    if last_bar.get(sym) == bar_ts: continue
                    sig = check_signal(sym, df, CONFIGS[sym])
                    if sig:
                        last_bar[sym] = bar_ts
                        cooldown[sym] = poll_count
                        direction, entry, sl, tp, adx_val, atr_val, cu, cd, cc = sig
                        risk_u = state['equity'] * RISK_PCT
                        qty = risk_u / atr_val if atr_val > 0 else 0
                        notional = qty * entry
                        if notional < 0.5 or notional > state['equity'] * 0.5: continue
                        state['positions'][sym] = {
                            "dir": direction, "entry": entry, "sl": sl, "tp": tp,
                            "qty": qty, "notional": round(notional,2),
                            "adx": round(adx_val,1), "atr": round(atr_val,6),
                            "open_time": now.isoformat(), "bar_ts": bar_ts,
                        }
                        save_state(state)
                        completed = state.get('wins',0)+state.get('losses',0)
                        logger.info(
                            f"🔔 #{completed+len(state['positions'])} 开仓 {sym} {direction} | "
                            f"入={entry:.5g} TP={tp:.5g} SL={sl:.5g} | "
                            f"ADX={adx_val:.1f} cu={cu} cd={cd} cc={cc*100:.3f}% 名义={notional:.1f}U"
                        )

            # 每轮输出全品种状态摘要
            now_str = now.strftime('%H:%M:%S')
            wins=state.get('wins',0); losses=state.get('losses',0); total=wins+losses
            wr=wins/total*100 if total>0 else 0
            pnl=state.get('total_pnl',0.0)
            open_pos = [f"{s}({v['dir']}@{v['entry']:.4g})" for s,v in state['positions'].items()]
            logger.info(
                f"[{now_str}] 完成={total}/{TARGET_TRADES} WR={wr:.0f}% PnL={pnl:+.3f}U "
                f"权益={state['equity']:.2f}U | 持仓={open_pos if open_pos else '无'}"
            )
            # 无持仓时显示信号距离
            if not state['positions']:
                for sym in SYMBOLS:
                    df = scan_results.get(sym)
                    if df is None: continue
                    row = df.iloc[-2]
                    adx = float(row['adx']) if not np.isnan(row['adx']) else 0
                    cu=int(row['cu']); cd=int(row['cd']); cc=float(row['cc'])*100
                    cfg = CONFIGS[sym]
                    short_ok = "✅" if cu>=cfg['sc'] and cc/100>=cfg['ccp'] else f"cu{cu}/{cfg['sc']}|cc{cc:.2f}%/{cfg['ccp']*100:.2f}%"
                    long_ok  = "✅" if cd>=cfg['lc'] and cc/100<=-cfg['ccp'] else f"cd{cd}/{cfg['lc']}|cc{cc:.2f}%/{-cfg['ccp']*100:.2f}%"
                    logger.info(f"  {sym}: ADX={adx:.0f} | SHORT=[{short_ok}] LONG=[{long_ok}]")

            time.sleep(POLL_SECS)

        except Exception as e:
            logger.error(f"主循环异常: {e}", exc_info=True)
            time.sleep(30)

    logger.info("引擎退出，保存最终状态")
    save_state(state)
    # 打印最终汇总
    wins=state.get('wins',0); losses=state.get('losses',0); total=wins+losses
    wr=wins/total*100 if total>0 else 0
    pnl=state.get('total_pnl',0.0)
    logger.info(f"最终汇总: 完成={total}笔 WR={wr:.1f}% 总PnL={pnl:+.4f}U 权益={state['equity']:.4f}U")

if __name__ == "__main__":
    main()
