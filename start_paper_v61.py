#!/usr/bin/env python3
"""
白夜纸交易 v6.1 — Phase5 实时纸交易
使用v6.1最优参数：ATR 1.5× SL + 快速TP
目标：≥100笔积累验证
"""
import json, time, requests, numpy as np, pandas as pd, warnings
from pathlib import Path
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
import logging

warnings.filterwarnings("ignore")

# ── 配置 ──────────────────────────────────────────────────────────────
CAPITAL       = 150.0
RISK_PCT      = 0.02
FEE           = 0.0009    # 0.09% 单边
MAX_POSITIONS = 3
POLL_SECS     = 60
LOG_FILE      = Path("logs/paper_v61.log")
STATE_FILE    = Path("logs/paper_v61_state.json")
TRADES_FILE   = Path("paper_trades_v61.json")
LOG_FILE.parent.mkdir(exist_ok=True)

# 日志
_h = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=2, encoding='utf-8')
_h.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
logger = logging.getLogger('paper_v61')
logger.addHandler(_h)
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)

# ── v6.1 最优参数 ──────────────────────────────────────────────────────
CONFIGS = {
    "BTCUSDT":  dict(sc=4, lc=5, ccp=0.002,  adx_th=22, tp_s=0.6, tp_l=0.5, sl_atr=1.5, long_disabled=False),
    "ETHUSDT":  dict(sc=5, lc=4, ccp=0.0015, adx_th=20, tp_s=0.6, tp_l=0.5, sl_atr=1.5, long_disabled=False),
    "SOLUSDT":  dict(sc=5, lc=4, ccp=0.0015, adx_th=25, tp_s=0.8, tp_l=0.7, sl_atr=1.5, long_disabled=False),
    "BNBUSDT":  dict(sc=5, lc=6, ccp=0.0015, adx_th=15, tp_s=0.8, tp_l=0.7, sl_atr=1.5, long_disabled=True,  adx_dynamic_tp=True),
    "LINKUSDT": dict(sc=7, lc=4, ccp=0.0025, adx_th=15, tp_s=0.6, tp_l=0.5, sl_atr=1.5, long_disabled=False),
    "POLUSDT":  dict(sc=5, lc=4, ccp=0.0015, adx_th=25, tp_s=0.8, tp_l=0.7, sl_atr=1.5, long_disabled=True),
}
SYMBOLS = list(CONFIGS.keys())

# ── 指标计算（Wilder's平滑，与v2.2引擎一致）──────────────────────────
def _wilder(arr, n):
    out = np.full(len(arr), np.nan)
    idx = np.where(~np.isnan(arr))[0]
    if len(idx) < n:
        return out
    start = idx[0]
    out[start+n-1] = np.nanmean(arr[start:start+n])
    for i in range(start+n, len(arr)):
        if not np.isnan(out[i-1]):
            out[i] = (out[i-1]*(n-1) + arr[i]) / n
    return out

def compute_indicators(df):
    df = df.copy()
    c, h, l = df['close'].values, df['high'].values, df['low'].values
    n = len(c)
    # ATR (Wilder)
    tr = np.maximum(h-l, np.maximum(np.abs(h-np.roll(c,1)), np.abs(l-np.roll(c,1))))
    tr[0] = h[0]-l[0]
    df['atr'] = _wilder(tr, 14)
    # EMA200
    df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()
    # ADX (Wilder)
    up = np.diff(h, prepend=h[0])
    dn = np.diff(l, prepend=l[0]) * -1
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr14 = _wilder(tr, 14)
    pdi = 100 * _wilder(pdm, 14) / np.where(atr14>0, atr14, np.nan)
    ndi = 100 * _wilder(ndm, 14) / np.where(atr14>0, atr14, np.nan)
    dx = 100 * np.abs(pdi-ndi) / np.where((pdi+ndi)>0, pdi+ndi, np.nan)
    df['adx'] = _wilder(dx, 14)
    # 连涨连跌
    consec_up = np.zeros(n, dtype=int)
    consec_dn = np.zeros(n, dtype=int)
    cum_chg = np.zeros(n)
    for i in range(1, n):
        if c[i] > c[i-1]:
            consec_up[i] = consec_up[i-1] + 1
            consec_dn[i] = 0
            if consec_up[i] == 1:
                cum_chg[i] = (c[i]-c[i-1])/c[i-1]
            else:
                cum_chg[i] = cum_chg[i-1] + (c[i]-c[i-1])/c[i-1]
        elif c[i] < c[i-1]:
            consec_dn[i] = consec_dn[i-1] + 1
            consec_up[i] = 0
            if consec_dn[i] == 1:
                cum_chg[i] = (c[i]-c[i-1])/c[i-1]
            else:
                cum_chg[i] = cum_chg[i-1] + (c[i]-c[i-1])/c[i-1]
        else:
            consec_up[i] = consec_up[i-1]
            consec_dn[i] = consec_dn[i-1]
            cum_chg[i] = cum_chg[i-1]
    df['consec_up'] = consec_up
    df['consec_dn'] = consec_dn
    df['cum_chg'] = cum_chg
    return df

def fetch_klines(sym, interval='15m', limit=250):
    url = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={interval}&limit={limit}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    raw = r.json()
    df = pd.DataFrame(raw, columns=['ts','open','high','low','close','volume',
                                     'close_time','quote_vol','trades','taker_base','taker_quote','ignore'])
    for col in ['open','high','low','close','volume']:
        df[col] = df[col].astype(float)
    df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
    df = df.set_index('ts').sort_index()
    return df[['open','high','low','close','volume']]

# ── 状态管理 ──────────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"positions": {}, "equity": CAPITAL, "last_bar": {}, "cooldown": {}}

def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2))

def load_trades():
    if TRADES_FILE.exists():
        return json.loads(TRADES_FILE.read_text())
    return []

def save_trades(trades):
    TRADES_FILE.write_text(json.dumps(trades, indent=2))

def stats(trades):
    closed = [t for t in trades if t['status'] == 'closed']
    if not closed: return "无已关闭交易"
    wins = [t for t in closed if t['pnl'] > 0]
    wr = len(wins)/len(closed)*100
    pnl = sum(t['pnl'] for t in closed)
    return f"共{len(closed)}笔 WR={wr:.1f}% 总PnL={pnl:+.3f}U"

# ── 信号检测 ──────────────────────────────────────────────────────────
def check_signal(sym, df, cfg):
    """检查最新K线信号，返回 (direction, entry, sl, tp) 或 None"""
    if len(df) < 210:
        return None
    # 用倒数第2根（已完成K线），第-1根为当前未完成K线
    i = -2
    row = df.iloc[i]
    adx = row['adx']
    if np.isnan(adx) or adx < cfg['adx_th']:
        return None
    atr = row['atr']
    if np.isnan(atr) or atr <= 0:
        return None
    close = row['close']
    ema200 = row['ema200']
    # 下一根K线开盘价作为入场价（取最新K线open）
    entry = df.iloc[-1]['open']
    
    # SHORT信号
    if row['consec_up'] >= cfg['sc'] and row['cum_chg'] >= cfg['ccp']:
        sl = entry + cfg['sl_atr'] * atr
        # ADX动态TP
        tp_mult = cfg['tp_s']
        if cfg.get('adx_dynamic_tp') and adx >= 40:
            tp_mult *= 1.6
        elif cfg.get('adx_dynamic_tp') and adx >= 30:
            tp_mult *= 1.3
        tp = entry - tp_mult * atr
        return ('SHORT', entry, sl, tp, adx, atr)
    
    # LONG信号
    if not cfg.get('long_disabled') and row['consec_dn'] >= cfg['lc'] and row['cum_chg'] <= -cfg['ccp'] and close > ema200:
        sl = entry - cfg['sl_atr'] * atr
        tp = entry + cfg['tp_l'] * atr
        return ('LONG', entry, sl, tp, adx, atr)
    
    return None

# ── 主循环 ──────────────────────────────────────────────────────────
def main():
    logger.info("=" * 60)
    logger.info("白夜纸交易 v6.1 启动 — Phase5")
    logger.info(f"品种: {SYMBOLS}")
    logger.info(f"资金: {CAPITAL}U | 单笔风险: {RISK_PCT*100}%")
    logger.info("=" * 60)
    
    state = load_state()
    trades = load_trades()
    
    logger.info(f"恢复状态: 持仓={len(state['positions'])} 权益={state['equity']:.2f}U")
    logger.info(f"历史交易: {stats(trades)}")
    
    while True:
        try:
            now = datetime.now(timezone.utc)
            logger.info(f"\n[{now.strftime('%m-%d %H:%M')}] 扫描 {SYMBOLS}")
            
            # 更新持仓TP/SL状态
            for sym in list(state['positions'].keys()):
                pos = state['positions'][sym]
                try:
                    df_live = fetch_klines(sym)
                    df_live = compute_indicators(df_live)
                    cur_price = float(df_live.iloc[-1]['close'])
                    # 检查TP/SL
                    if pos['dir'] == 'SHORT':
                        if cur_price <= pos['tp']:
                            pnl = (pos['entry'] - cur_price) / pos['entry'] * pos['notional'] - pos['notional'] * FEE * 2
                            state['equity'] += pnl
                            pos['status'] = 'closed'; pos['exit'] = cur_price; pos['pnl'] = round(pnl, 4); pos['exit_reason'] = 'tp'
                            trades.append(pos)
                            del state['positions'][sym]
                            logger.info(f"✅ {sym} SHORT TP命中 entry={pos['entry']:.4f} exit={cur_price:.4f} PnL={pnl:+.3f}U")
                        elif cur_price >= pos['sl']:
                            pnl = (pos['entry'] - cur_price) / pos['entry'] * pos['notional'] - pos['notional'] * FEE * 2
                            state['equity'] += pnl
                            pos['status'] = 'closed'; pos['exit'] = cur_price; pos['pnl'] = round(pnl, 4); pos['exit_reason'] = 'sl'
                            trades.append(pos)
                            del state['positions'][sym]
                            logger.info(f"❌ {sym} SHORT SL止损 entry={pos['entry']:.4f} exit={cur_price:.4f} PnL={pnl:+.3f}U")
                    else:  # LONG
                        if cur_price >= pos['tp']:
                            pnl = (cur_price - pos['entry']) / pos['entry'] * pos['notional'] - pos['notional'] * FEE * 2
                            state['equity'] += pnl
                            pos['status'] = 'closed'; pos['exit'] = cur_price; pos['pnl'] = round(pnl, 4); pos['exit_reason'] = 'tp'
                            trades.append(pos)
                            del state['positions'][sym]
                            logger.info(f"✅ {sym} LONG TP命中 entry={pos['entry']:.4f} exit={cur_price:.4f} PnL={pnl:+.3f}U")
                        elif cur_price <= pos['sl']:
                            pnl = (cur_price - pos['entry']) / pos['entry'] * pos['notional'] - pos['notional'] * FEE * 2
                            state['equity'] += pnl
                            pos['status'] = 'closed'; pos['exit'] = cur_price; pos['pnl'] = round(pnl, 4); pos['exit_reason'] = 'sl'
                            trades.append(pos)
                            del state['positions'][sym]
                            logger.info(f"❌ {sym} LONG SL止损 entry={pos['entry']:.4f} exit={cur_price:.4f} PnL={pnl:+.3f}U")
                except Exception as e:
                    logger.warning(f"{sym} 持仓检查失败: {e}")
            
            # 扫描新信号
            if len(state['positions']) < MAX_POSITIONS:
                for sym in SYMBOLS:
                    if sym in state['positions']:
                        continue
                    if len(state['positions']) >= MAX_POSITIONS:
                        break
                    # 冷却期检查（同方向5根K线内不重复）
                    try:
                        df = fetch_klines(sym)
                        df = compute_indicators(df)
                        cfg = CONFIGS[sym]
                        sig = check_signal(sym, df, cfg)
                        if sig:
                            direction, entry, sl, tp, adx_val, atr_val = sig
                            # 检查bar去重
                            bar_ts = str(df.index[-2])
                            last_key = f"{sym}_{direction}"
                            if state.get('last_bar', {}).get(last_key) == bar_ts:
                                continue
                            # 计算仓位
                            risk_amt = state['equity'] * RISK_PCT
                            sl_dist = abs(entry - sl)
                            if sl_dist <= 0:
                                continue
                            qty = risk_amt / sl_dist
                            notional = qty * entry
                            pos = {
                                'sym': sym, 'dir': direction, 'entry': entry, 'sl': sl, 'tp': tp,
                                'qty': round(qty, 6), 'notional': round(notional, 4),
                                'adx': round(adx_val, 2), 'atr': round(atr_val, 6),
                                'open_time': now.isoformat(), 'status': 'open', 'pnl': 0
                            }
                            state['positions'][sym] = pos
                            state.setdefault('last_bar', {})[last_key] = bar_ts
                            logger.info(f"🔔 开仓 {sym} {direction} entry={entry:.4f} SL={sl:.4f} TP={tp:.4f} notional={notional:.2f}U ADX={adx_val:.1f}")
                    except Exception as e:
                        logger.warning(f"{sym} 扫描失败: {e}")
            
            save_state(state)
            save_trades(trades)
            logger.info(f"状态保存 权益={state['equity']:.2f}U 持仓={list(state['positions'].keys())} {stats(trades)}")
            time.sleep(POLL_SECS)
            
        except KeyboardInterrupt:
            logger.info("纸交易已停止")
            break
        except Exception as e:
            logger.error(f"主循环错误: {e}", exc_info=True)
            time.sleep(10)

if __name__ == '__main__':
    main()
