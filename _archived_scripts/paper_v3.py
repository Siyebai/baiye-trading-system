#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
白夜多周期纸交易 v3.0 — 手续费彻底修正
使用 backtest_engine_v3.py（正确手续费公式）
周期: 1h + 4h（经验证可行）
品种: BTC/ETH/SOL/BNB/LINK/LTC（1h） + AVAX/ADA（4h）
"""
import json, time, requests, numpy as np, pandas as pd, warnings
from pathlib import Path
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
import logging, sys

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).parent))
from engine.backtest_engine_v3 import compute_indicators, generate_signals, FEE

# ── 配置 ──────────────────────────────────────────────────────
CAPITAL    = 150.0
RISK_PCT   = 0.02
MAX_NOTIONAL_X = 3.0
MIN_FEE_COVER  = 2.5
MAX_POSITIONS  = 5
REPORT_EVERY   = 300  # 秒

LOG_FILE    = Path("logs/paper_v3.log")
STATE_FILE  = Path("logs/paper_v3_state.json")
TRADES_FILE = Path("trades_paper_v3.json")
LOG_FILE.parent.mkdir(exist_ok=True)

_h = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=3, encoding='utf-8')
_h.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
_c = logging.StreamHandler()
_c.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
log = logging.getLogger('paper_v3')
log.addHandler(_h); log.addHandler(_c)
log.setLevel(logging.INFO)

# ── 经验证的可行参数（v3引擎，手续费正确）──────────────────
# (sc, lc, ccp, adx_th, tp_s, tp_l, sl_atr, long_disabled)
CONFIGS = {
    "1h": {
        "poll_secs": 180,   # 3分钟轮询
        "kline_limit": 500,
        "symbols": {
            "BTCUSDT":  (4, 3, 0.005, 20, 3.0, 2.5, 1.5, False),
            "ETHUSDT":  (4, 3, 0.004, 18, 2.0, 1.8, 1.5, False),
            "SOLUSDT":  (5, 4, 0.003, 20, 2.0, 1.8, 1.5, False),
            "BNBUSDT":  (4, 3, 0.005, 20, 3.0, 2.5, 1.5, True),
            "LINKUSDT": (4, 4, 0.003, 20, 2.0, 1.8, 1.5, False),
            "LTCUSDT":  (6, 3, 0.005, 15, 3.0, 2.5, 1.5, False),
        }
    },
    "4h": {
        "poll_secs": 600,   # 10分钟轮询
        "kline_limit": 300,
        "symbols": {
            "AVAXUSDT": (4, 3, 0.003, 18, 2.5, 2.0, 1.5, False),
            "ADAUSDT":  (4, 4, 0.003, 15, 2.0, 1.8, 1.5, False),
        }
    },
}

# ── 指标 ──────────────────────────────────────────────────────
def fetch(sym, tf, limit):
    url = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={tf}&limit={limit}"
    r = requests.get(url, timeout=15); r.raise_for_status()
    df = pd.DataFrame(r.json(), columns=['ts','open','high','low','close','volume','ct','qv','t','tb','tq','ig'])
    for c in ['open','high','low','close','volume']: df[c]=df[c].astype(float)
    df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
    return df.set_index('ts').sort_index()[['open','high','low','close','volume']]

def check_signal(df, sc, lc, ccp, adx_th, tp_s, tp_l, sl_atr, long_dis, equity):
    if len(df) < 220: return None
    row = df.iloc[-2]   # 已闭合K线
    adx = row['adx']; atr = row['atr']
    if np.isnan(adx) or np.isnan(atr) or atr <= 0: return None
    if adx < adx_th: return None

    entry = float(df.iloc[-1]['open'])  # 当前K线开盘价
    direction = None; sl = None; tp = None

    # 做空
    if row['cu'] >= sc and row['cc'] >= ccp:
        sl = entry + sl_atr * atr
        tp = entry - tp_s * atr
        direction = '做空'
    # 做多
    elif (not long_dis) and row['cd'] >= lc and row['cc'] <= -ccp and row['close'] > row['ema200']:
        sl = entry - sl_atr * atr
        tp = entry + tp_l * atr
        direction = '做多'

    if direction is None: return None

    sl_dist_pct = abs(entry - sl) / entry
    tp_dist_pct = abs(entry - tp) / entry
    if sl_dist_pct <= 0: return None

    # ✅ 止盈可行性：TP利润必须覆盖手续费MIN_FEE_COVER倍
    if tp_dist_pct < MIN_FEE_COVER * 2 * FEE:
        log.debug(f"  {direction} TP利润{tp_dist_pct*100:.4f}% < 手续费阈值{MIN_FEE_COVER*2*FEE*100:.4f}%，跳过")
        return None

    # ✅ 名义仓位（含上限）
    risk = equity * RISK_PCT
    notional = min(risk / sl_dist_pct, equity * MAX_NOTIONAL_X)

    return dict(
        dir=direction, entry=entry, sl=sl, tp=tp,
        atr=float(atr), adx=float(adx),
        notional=round(notional, 4),
        sl_dist_pct=sl_dist_pct, tp_dist_pct=tp_dist_pct,
    )

# ── 状态管理 ───────────────────────────────────────────────────
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

# ── 报告 ───────────────────────────────────────────────────────
def print_report(state, trades):
    closed = [t for t in trades if t.get('status') == 'closed']
    wins   = [t for t in closed if t.get('pnl', 0) > 0]
    pnl    = sum(t.get('pnl', 0) for t in closed)
    now    = datetime.now(timezone.utc).strftime('%m-%d %H:%M UTC')
    log.info("=" * 68)
    log.info(f"  白夜纸交易 v3.0  [{now}]")
    log.info("=" * 68)
    log.info(f"  权益: {state['equity']:.4f}U  盈亏: {state['equity']-CAPITAL:+.4f}U  ({(state['equity']/CAPITAL-1)*100:+.1f}%)")
    if closed:
        wr = len(wins)/len(closed)*100
        log.info(f"  已关闭: {len(closed)}笔  胜率: {wr:.1f}% ({len(wins)}/{len(closed)})  总盈亏: {pnl:+.4f}U")
        log.info("-" * 68)
        log.info(f"  {'周期':4s} {'品种':10s} {'方向':4s} {'结果':6s} {'盈亏':>9s} {'名义仓':>8s}  时间")
        for t in closed[-20:]:
            tag = '✅止盈' if t.get('pnl', 0) > 0 else '❌止损'
            ts  = t.get('open_time', '')[:16].replace('T', ' ')
            log.info(f"  {t['tf']:4s} {t['sym']:10s} {t['dir']:4s} {tag:6s} {t['pnl']:+9.4f}U {t.get('notional',0):8.1f}U  {ts}")
    else:
        log.info("  已关闭: 0笔（等待信号）")

    if state['positions']:
        log.info("-" * 68)
        log.info("  当前持仓:")
        for key, pos in state['positions'].items():
            try:
                r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={pos['sym']}", timeout=5).json()
                cur = float(r['price'])
                entry = pos['entry']; tp = pos['tp']; sl = pos['sl']
                if pos['dir'] == '做空':
                    unreal = (entry - cur) / entry * pos['notional']
                    prog   = (entry - cur) / (entry - tp) * 100 if (entry - tp) != 0 else 0
                else:
                    unreal = (cur - entry) / entry * pos['notional']
                    prog   = (cur - entry) / (tp - entry) * 100 if (tp - entry) != 0 else 0
                fee_est = pos['notional'] * FEE * 2
                log.info(f"  [{pos['tf']}] {pos['sym']:10s} {pos['dir']:4s} | 入={entry:.4f} 现={cur:.4f}")
                log.info(f"       TP={tp:.4f} SL={sl:.4f} | 名义={pos['notional']:.1f}U 费≈{fee_est:.4f}U")
                log.info(f"       未实现={unreal:+.4f}U | TP进度={prog:.1f}% | ADX={pos['adx']:.1f}")
            except:
                log.info(f"  [{pos['tf']}] {pos['sym']} {pos['dir']} (价格获取失败)")
    log.info("=" * 68)

# ── 单周期扫描 ─────────────────────────────────────────────────
def scan_tf(tf, state, trades, now):
    cfg = CONFIGS[tf]
    for sym, params in cfg['symbols'].items():
        sc, lc, ccp, adx_th, tp_s, tp_l, sl_atr, long_dis = params
        pos_key = f"{tf}:{sym}"

        # 已有持仓：检查TP/SL
        if pos_key in state['positions']:
            pos = state['positions'][pos_key]
            try:
                r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={sym}", timeout=5).json()
                cur = float(r['price'])
                entry = pos['entry']; tp = pos['tp']; sl = pos['sl']

                hit = None
                if pos['dir'] == '做空':
                    if cur <= tp: hit = '止盈'
                    elif cur >= sl: hit = '止损'
                    exit_p = tp if hit == '止盈' else sl if hit else cur
                    pnl_pct = (entry - exit_p) / entry
                else:
                    if cur >= tp: hit = '止盈'
                    elif cur <= sl: hit = '止损'
                    exit_p = tp if hit == '止盈' else sl if hit else cur
                    pnl_pct = (exit_p - entry) / entry

                if hit:
                    notional = pos['notional']
                    # ✅ 正确手续费：按名义仓位
                    pnl = notional * pnl_pct - notional * FEE * 2
                    state['equity'] += pnl
                    closed_pos = dict(**pos, status='closed', exit=exit_p,
                                     pnl=round(pnl, 4), exit_reason=hit,
                                     close_time=now.isoformat(),
                                     fee=round(notional * FEE * 2, 4))
                    trades.append(closed_pos)
                    del state['positions'][pos_key]
                    icon = '✅' if hit == '止盈' else '❌'
                    log.info(f"{icon} [{tf}] {sym} {pos['dir']} {hit} | 入={entry:.4f} 出={exit_p:.4f} | 名义={notional:.1f}U 盈亏={pnl:+.4f}U | 权益={state['equity']:.4f}U")
            except Exception as e:
                log.warning(f"[{tf}] {sym} 持仓检查失败: {e}")
            continue

        # 扫描新信号
        if len(state['positions']) >= MAX_POSITIONS:
            continue
        try:
            df = compute_indicators(fetch(sym, tf, cfg['kline_limit']))
            sig = check_signal(df, sc, lc, ccp, adx_th, tp_s, tp_l, sl_atr, long_dis, state['equity'])
            if not sig: continue

            # 去重：同根K线不重复开仓
            bar_ts  = str(df.index[-2])
            bar_key = f"{tf}:{sym}:{sig['dir']}"
            if state.get('last_bar', {}).get(bar_key) == bar_ts:
                continue

            pos = dict(
                tf=tf, sym=sym, status='open',
                dir=sig['dir'], entry=sig['entry'],
                sl=sig['sl'], tp=sig['tp'],
                notional=sig['notional'],
                adx=sig['adx'], atr=sig['atr'],
                open_time=now.isoformat(), pnl=0,
                fee_est=round(sig['notional'] * FEE * 2, 4),
                tp_dist_pct=round(sig['tp_dist_pct'] * 100, 4),
            )
            state['positions'][pos_key] = pos
            state.setdefault('last_bar', {})[bar_key] = bar_ts

            fee = sig['notional'] * FEE * 2
            tp_profit = sig['notional'] * sig['tp_dist_pct']
            log.info(f"🔔 [{tf}] {sym} {sig['dir']} 开仓 | 入={sig['entry']:.4f} TP={sig['tp']:.4f} SL={sig['sl']:.4f}")
            log.info(f"    名义={sig['notional']:.1f}U 费≈{fee:.4f}U TP利润≈{tp_profit:.4f}U ADX={sig['adx']:.1f}")

        except Exception as e:
            log.warning(f"[{tf}] {sym} 扫描失败: {e}")

# ── 主循环 ─────────────────────────────────────────────────────
def main():
    log.info("=" * 68)
    log.info("  白夜多周期纸交易 v3.0 启动（手续费修正版）")
    log.info("  1h: BTC/ETH/SOL/BNB/LINK/LTC")
    log.info("  4h: AVAX/ADA")
    log.info(f"  资金={CAPITAL}U | 风险={RISK_PCT*100:.0f}% | 最大持仓={MAX_POSITIONS}")
    log.info(f"  TP最低覆盖手续费{MIN_FEE_COVER}倍 | 名义仓位上限={MAX_NOTIONAL_X}x")
    log.info("=" * 68)

    state  = load_state()
    trades = load_trades()
    last_report = 0
    last_scan   = {tf: 0 for tf in CONFIGS}
    poll_secs   = {tf: CONFIGS[tf]['poll_secs'] for tf in CONFIGS}

    while True:
        try:
            now    = datetime.now(timezone.utc)
            now_ts = now.timestamp()

            for tf in CONFIGS:
                if now_ts - last_scan[tf] >= poll_secs[tf]:
                    scan_tf(tf, state, trades, now)
                    last_scan[tf] = now_ts
                    save_state(state)
                    save_trades(trades)

            if now_ts - last_report >= REPORT_EVERY:
                print_report(state, trades)
                last_report = now_ts

            time.sleep(15)

        except KeyboardInterrupt:
            log.info("引擎停止")
            print_report(state, trades)
            break
        except Exception as e:
            log.error(f"主循环错误: {e}", exc_info=True)
            time.sleep(30)

if __name__ == '__main__':
    main()
