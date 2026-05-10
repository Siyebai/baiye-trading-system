#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
白夜纸交易 v6.6 — 1m高频版（实时数据深度测试）
目标: 今天完成≥10笔完整闭环
时间框架: 1m (接入币安真实实时数据)

参数（1m时间框架专用，回测EV>0验证）:
品种    | sc | lc | adx | ccp    | TP   | SL
ETHUSDT |  3 |  3 |  12 | 0.0004 | 0.8x | 1.0x
SOLUSDT |  3 |  3 |  12 | 0.0005 | 0.8x | 1.0x
LINKUSDT|  3 |  3 |  12 | 0.0005 | 0.8x | 1.0x
BNBUSDT |  3 |  3 |  12 | 0.0004 | 0.8x | 1.0x
DOTUSDT |  3 |  3 |  12 | 0.0005 | 0.8x | 1.0x
ADAUSDT |  3 |  3 |  12 | 0.0004 | 0.8x | 1.0x

预期: ~20-40笔/天信号 → 当天10+笔闭环
"""
import json, time, requests, numpy as np, pandas as pd, warnings
from pathlib import Path
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
import logging
warnings.filterwarnings("ignore")

# ── 全局参数 ─────────────────────────────────────────────────
CAPITAL        = 150.0
RISK_PCT       = 0.02       # 每笔风险2%=3U
FEE            = 0.0004     # 往返手续费0.04%（maker估算）
MAX_POSITIONS  = 6          # 最多6个持仓
POLL_SECS      = 10         # 每10秒扫描一次（1m级别）
TARGET_TRADES  = 20         # 今天目标20笔
DAILY_LOSS_PCT = 0.06       # 日熔断6%
INTERVAL       = "1m"       # 1分钟时间框架

LOG_FILE    = Path("logs/paper_v66.log")
STATE_FILE  = Path("logs/paper_v66_state.json")
TRADES_FILE = Path("logs/paper_v66_trades.json")
LOG_FILE.parent.mkdir(exist_ok=True)

_h = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=2, encoding='utf-8')
_h.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
logger = logging.getLogger('paper_v66')
logger.addHandler(_h); logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)

# ── 品种配置（1m专用，sc/lc较小，ccp较小）───────────────────
CONFIGS = {
    "ETHUSDT":  dict(sc=3, lc=3, ccp=0.0004, adx_th=12, tp_s=0.8, sl_atr=1.0, long_disabled=False),
    "SOLUSDT":  dict(sc=3, lc=3, ccp=0.0005, adx_th=12, tp_s=0.8, sl_atr=1.0, long_disabled=False),
    "LINKUSDT": dict(sc=3, lc=3, ccp=0.0005, adx_th=12, tp_s=0.8, sl_atr=1.0, long_disabled=False),
    "BNBUSDT":  dict(sc=3, lc=3, ccp=0.0004, adx_th=12, tp_s=0.8, sl_atr=1.0, long_disabled=False),
    "DOTUSDT":  dict(sc=3, lc=3, ccp=0.0005, adx_th=12, tp_s=0.8, sl_atr=1.0, long_disabled=False),
    "ADAUSDT":  dict(sc=3, lc=3, ccp=0.0004, adx_th=12, tp_s=0.8, sl_atr=1.0, long_disabled=False),
}
SYMBOLS = list(CONFIGS.keys())

# ── 技术指标（Wilder平滑，与回测一致）───────────────────────
def _wilder(series, n):
    out = np.full(len(series), np.nan)
    if len(series) < n: return out
    out[n-1] = series[:n].mean()
    for i in range(n, len(series)):
        out[i] = out[i-1] * (n-1)/n + series[i] / n
    return out

def compute(df):
    df = df.copy()
    # ATR（Wilder）
    hl = (df['high'] - df['low']).values
    hc = abs(df['high'] - df['close'].shift(1).fillna(df['close'].iloc[0])).values
    lc_ = abs(df['low'] - df['close'].shift(1).fillna(df['close'].iloc[0])).values
    tr = np.maximum(hl, np.maximum(hc, lc_))
    df['atr'] = _wilder(tr, 14)

    # ADX（Wilder）
    pm = df['high'].diff().clip(lower=0).values
    nm = (-df['low'].diff()).clip(lower=0).values
    pdm = np.where(pm > nm, pm, 0.0)
    ndm = np.where(nm > pm, nm, 0.0)
    tr_w = _wilder(tr, 14)
    pdi = np.where(tr_w > 0, _wilder(pdm, 14) / tr_w * 100, 0)
    ndi = np.where(tr_w > 0, _wilder(ndm, 14) / tr_w * 100, 0)
    dx = np.where((pdi+ndi)>0, abs(pdi-ndi)/(pdi+ndi)*100, 0)
    df['adx'] = _wilder(dx, 14)

    # EMA200
    df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()

    # 连涨/跌计数和累计涨跌
    cu = np.zeros(len(df), dtype=int)
    cd = np.zeros(len(df), dtype=int)
    cc = np.zeros(len(df))
    for i in range(1, len(df)):
        if df['close'].iloc[i] > df['close'].iloc[i-1]:
            cu[i] = cu[i-1] + 1; cd[i] = 0
            cc[i] = cc[i-1] + (df['close'].iloc[i] - df['close'].iloc[i-1]) / df['close'].iloc[i-1]
        elif df['close'].iloc[i] < df['close'].iloc[i-1]:
            cd[i] = cd[i-1] + 1; cu[i] = 0
            cc[i] = cc[i-1] + (df['close'].iloc[i] - df['close'].iloc[i-1]) / df['close'].iloc[i-1]
        else:
            cu[i] = cu[i-1]; cd[i] = cd[i-1]; cc[i] = cc[i-1]
    df['cu'] = cu; df['cd'] = cd; df['cc'] = cc
    return df

def fetch_klines(sym, limit=300):
    r = requests.get("https://api.binance.com/api/v3/klines",
        params={'symbol':sym,'interval':INTERVAL,'limit':limit}, timeout=10)
    r.raise_for_status()
    df = pd.DataFrame(r.json(), columns=['ts','open','high','low','close','vol','ct','qv','tr','tbb','tbq','ign'])
    for c in ['open','high','low','close']: df[c]=df[c].astype(float)
    return df

def check_signal(sym, df, cfg):
    """[-2]已完成K线判信号，[-1]收盘价入场"""
    if len(df) < 210: return None
    row = df.iloc[-2]
    adx = float(row['adx']) if not np.isnan(row['adx']) else 0
    atr = float(row['atr']) if not np.isnan(row['atr']) else 0
    if adx < cfg['adx_th'] or atr <= 0: return None

    entry = float(df.iloc[-1]['close'])
    cu = int(row['cu']); cd = int(row['cd']); cc = float(row['cc'])
    ema200 = float(row['ema200']) if not np.isnan(row['ema200']) else 0

    # 做空：连涨≥sc + 累涨≥ccp
    if cu >= cfg['sc'] and cc >= cfg['ccp']:
        sl = entry + cfg['sl_atr'] * atr
        tp = entry - cfg['tp_s'] * atr
        return ('做空', entry, sl, tp, adx, atr, cu, cd, cc)

    # 做多：连跌≥lc + 累跌≥ccp + close>EMA200
    if not cfg['long_disabled']:
        if cd >= cfg['lc'] and cc <= -cfg['ccp'] and entry > ema200:
            sl = entry - cfg['sl_atr'] * atr
            tp = entry + cfg['tp_s'] * atr
            return ('做多', entry, sl, tp, adx, atr, cu, cd, cc)
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
    return {"positions":{}, "equity":CAPITAL, "last_bar":{},
            "day_loss":0.0, "day_date":"", "total_trades":0,
            "wins":0, "losses":0, "total_pnl":0.0}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))

def load_trades():
    if TRADES_FILE.exists(): return json.loads(TRADES_FILE.read_text())
    return []

def save_trades(trades):
    TRADES_FILE.write_text(json.dumps(trades, ensure_ascii=False, indent=2))

def fmt_summary(state, closed_trades):
    wins = state.get('wins',0); losses = state.get('losses',0)
    total = wins + losses
    wr = wins/total*100 if total > 0 else 0
    pnl = state.get('total_pnl', 0.0)
    equity = state['equity']
    open_pos = len(state['positions'])
    return (f"已完成={total}笔 WR={wr:.1f}% PnL={pnl:+.2f}U "
            f"权益={equity:.2f}U({(equity-CAPITAL)/CAPITAL*100:+.1f}%) "
            f"持仓={open_pos}个")

def main():
    state = load_state()
    trades = load_trades()
    
    # 今日重置日亏损
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if state.get('day_date') != today:
        state['day_loss'] = 0.0
        state['day_date'] = today

    logger.info("="*65)
    logger.info(f"白夜纸交易 v6.6 — 1m高频实时数据版")
    logger.info(f"品种: {SYMBOLS}")
    logger.info(f"时间框架: {INTERVAL} | FEE={FEE*100:.3f}%单边 | 资金:{CAPITAL}U | 风险:{RISK_PCT*100}%")
    logger.info(f"目标: 今天≥{TARGET_TRADES}笔完整闭环 | 最多{MAX_POSITIONS}个持仓")
    logger.info("="*65)
    logger.info(f"恢复: 持仓={list(state['positions'].keys())} 权益={state['equity']:.4f}U")
    logger.info(f"进度: {state.get('wins',0)+state.get('losses',0)}/{TARGET_TRADES}笔")

    cooldown = {}  # 同品种5根K线冷却
    last_scan = {}

    poll_count = 0
    while True:
        try:
            poll_count += 1
            now = datetime.now(timezone.utc)
            today = now.strftime('%Y-%m-%d')
            if state.get('day_date') != today:
                state['day_loss'] = 0.0
                state['day_date'] = today

            # 日熔断检查
            if state['day_loss'] >= state['equity'] * DAILY_LOSS_PCT:
                logger.warning(f"⛔ 日熔断触发！日亏={state['day_loss']:.2f}U 暂停开仓")
                time.sleep(60)
                continue

            # 检查持仓退出
            for sym in list(state['positions'].keys()):
                pos = state['positions'][sym]
                try:
                    r = requests.get("https://api.binance.com/api/v3/klines",
                        params={'symbol':sym,'interval':INTERVAL,'limit':2}, timeout=5)
                    bar = r.json()[-1]
                    cur_high = float(bar[2]); cur_low = float(bar[3])
                    cur_price = float(bar[4])
                except Exception as e:
                    logger.warning(f"获取{sym}价格失败: {e}")
                    continue

                result, exit_price = check_exit(pos, cur_high, cur_low)
                if result:
                    # 计算PnL
                    qty = pos['qty']
                    if pos['dir'] == '做空':
                        raw_pnl = (pos['entry'] - exit_price) * qty
                    else:
                        raw_pnl = (exit_price - pos['entry']) * qty
                    fee_cost = (pos['entry'] + exit_price) * qty * FEE
                    net_pnl = raw_pnl - fee_cost

                    state['equity'] += net_pnl
                    if net_pnl > 0:
                        state['wins'] = state.get('wins', 0) + 1
                    else:
                        state['losses'] = state.get('losses', 0) + 1
                        state['day_loss'] += abs(net_pnl)
                    state['total_pnl'] = state.get('total_pnl', 0.0) + net_pnl
                    state['total_trades'] = state.get('total_trades', 0) + 1
                    del state['positions'][sym]

                    # 记录交易
                    trade_no = state['total_trades']
                    closed = {
                        "no": trade_no, "sym": sym, "dir": pos['dir'],
                        "entry": pos['entry'], "exit": exit_price,
                        "qty": qty, "result": result,
                        "net_pnl": round(net_pnl, 4),
                        "open_time": pos['open_time'],
                        "close_time": now.isoformat(),
                        "equity_after": round(state['equity'], 4),
                        "adx": pos.get('adx'), "atr": pos.get('atr'),
                    }
                    trades.append(closed)
                    save_trades(trades)
                    save_state(state)

                    wins = state.get('wins',0); total = wins + state.get('losses',0)
                    wr = wins/total*100 if total > 0 else 0
                    emoji = "✅" if net_pnl > 0 else "❌"
                    logger.info(
                        f"{emoji} #{trade_no} 平仓 {sym} {pos['dir']} {result} "
                        f"入={pos['entry']:.5g} 出={exit_price:.5g} "
                        f"PnL={net_pnl:+.3f}U | "
                        f"累计WR={wr:.1f}% 权益={state['equity']:.2f}U"
                    )

            # 扫描新信号（每N轮，或每个品种间隔控制）
            if len(state['positions']) < MAX_POSITIONS:
                for sym in SYMBOLS:
                    if sym in state['positions']:
                        continue
                    # 冷却检查（连续信号保护）
                    last_cd = cooldown.get(sym, 0)
                    if poll_count - last_cd < 5:
                        continue

                    try:
                        df = compute(fetch_klines(sym, 300))
                    except Exception as e:
                        logger.warning(f"fetch {sym} err: {e}")
                        continue

                    # 防止同根K线重复触发
                    bar_ts = int(df.iloc[-2]['ts'])
                    if last_scan.get(sym) == bar_ts:
                        continue

                    sig = check_signal(sym, df, CONFIGS[sym])
                    if sig:
                        last_scan[sym] = bar_ts
                        cooldown[sym] = poll_count
                        direction, entry, sl, tp, adx_val, atr_val, cu, cd, cc = sig

                        # 仓位计算
                        risk_u = state['equity'] * RISK_PCT
                        atr_usdt = atr_val
                        qty = risk_u / atr_usdt if atr_usdt > 0 else 0
                        notional = qty * entry
                        if notional < 1 or notional > state['equity'] * 0.5:
                            continue  # 跳过不合理仓位

                        state['positions'][sym] = {
                            "dir": direction, "entry": entry, "sl": sl, "tp": tp,
                            "qty": qty, "notional": round(notional, 2),
                            "adx": round(adx_val, 1), "atr": round(atr_val, 6),
                            "cu": cu, "cd": cd, "cc": round(cc, 5),
                            "open_time": now.isoformat(), "bar_ts": bar_ts,
                        }
                        save_state(state)
                        completed = state.get('wins',0) + state.get('losses',0)
                        logger.info(
                            f"🔔 #{completed+len(state['positions'])} 开仓 {sym} {direction} | "
                            f"入={entry:.5g} SL={sl:.5g} TP={tp:.5g} | "
                            f"ADX={adx_val:.0f} ATR={atr_val:.5f} 名义={notional:.1f}U"
                        )

            # 每30轮打印一次状态（约5分钟）
            if poll_count % 30 == 0:
                completed = state.get('wins',0) + state.get('losses',0)
                open_pos = [f"{s}({v['dir']})" for s,v in state['positions'].items()]
                logger.info(
                    f"📊 {fmt_summary(state, trades)} | "
                    f"持仓={open_pos}"
                )

            time.sleep(POLL_SECS)

        except KeyboardInterrupt:
            logger.info("用户中断，保存状态退出")
            save_state(state)
            break
        except Exception as e:
            logger.error(f"主循环异常: {e}", exc_info=True)
            time.sleep(30)

if __name__ == "__main__":
    main()
