#!/usr/bin/env python3
"""
白夜系统 — Phase5 纸交易引擎
用途：实时监控信号，模拟开平仓，记录每笔交易，验证≥100笔真实市场表现
配置：读取 config/optimal_params.json
日志：logs/paper_trades.json + logs/paper_summary.json
"""
import requests, json, time, numpy as np, pandas as pd
from datetime import datetime, timezone
import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine.backtest_engine_v2 import compute_indicators, generate_signals

# ── 配置加载 ──────────────────────────────────────────
with open('config/optimal_params.json') as f:
    CFG = json.load(f)

SYS_CFG  = CFG['system']
SYMBOLS  = {k: v for k, v in CFG['symbols'].items() if v['enabled']}
RISK_CFG = CFG['risk_control']

CAPITAL_INIT = SYS_CFG['capital']        # 150U
RISK_PCT     = SYS_CFG['risk_per_trade'] # 0.02
FEE          = SYS_CFG['fee_rate']       # 0.0009
COOLDOWN     = SYS_CFG['signal_cooldown']# 5根

LOG_FILE     = 'logs/paper_trades.json'
SUMMARY_FILE = 'logs/paper_summary.json'
STATE_FILE   = 'logs/paper_state.json'

os.makedirs('logs', exist_ok=True)

# ── 状态管理 ──────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        'equity': CAPITAL_INIT,
        'trades': [],
        'open_positions': {},
        'last_signals': {},   # {sym: last_signal_bar_index}
        'start_time': datetime.now(timezone.utc).isoformat(),
        'bars_processed': {}  # {sym: count}
    }

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def load_trades():
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, Exception) as e:
            print(f"  ⚠️ paper_trades.json读取失败({e})，备份并重置")
            import shutil
            shutil.copy(LOG_FILE, LOG_FILE + '.bak')
            return []
    return []

def save_trades(trades):
    with open(LOG_FILE, 'w') as f:
        json.dump(trades, f, indent=2, ensure_ascii=False)

# ── 数据拉取（最近200根K线） ──────────────────────────
def fetch_recent(symbol, limit=250):
    url = 'https://fapi.binance.com/fapi/v1/klines'
    r = requests.get(url, params=dict(symbol=symbol, interval='15m', limit=limit), timeout=10)
    data = r.json()
    if not data or isinstance(data, dict):
        return None
    df = pd.DataFrame(data, columns=[
        'ts','open','high','low','close','vol','close_ts',
        'qvol','trades','taker_buy','taker_buy_q','ignore'])
    for c in ['open','high','low','close','vol']:
        df[c] = df[c].astype(float)
    df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    df.set_index('ts', inplace=True)
    df.drop_duplicates(inplace=True)
    return df

# ── 信号检测（只看最新一根） ─────────────────────────
def check_latest_signal(sym, params):
    df = fetch_recent(sym, 250)
    if df is None or len(df) < 210:
        return None, None, None
    df = compute_indicators(df)
    sigs = generate_signals(df,
        sc=params['sc'], lc=params['lc'],
        ccp=params['ccp'], adx_th=params['adx_th'],
        cooldown=COOLDOWN)
    # 检查最新完成的K线（倒数第2根，最后一根可能未完成）
    sig_idx = -2
    if sigs[sig_idx] != 0:
        return int(sigs[sig_idx]), df.iloc[sig_idx], df
    return None, None, df

# ── 纸交易执行 ────────────────────────────────────────
def paper_trade_once(state, all_trades):
    now = datetime.now(timezone.utc).isoformat()
    equity = state['equity']
    positions = state['open_positions']
    new_signals = []

    # 1. 检查已有持仓是否触发TP/SL
    closed = []
    for sym, pos in positions.items():
        df = fetch_recent(sym, 5)
        if df is None: continue
        latest = df.iloc[-2]  # 最近完成的K线
        hit_tp = hit_sl = False
        if pos['dir'] == 1:   # LONG
            hit_tp = latest['high'] >= pos['tp']
            hit_sl = latest['low']  <= pos['sl']
        else:                  # SHORT
            hit_tp = latest['low']  <= pos['tp']
            hit_sl = latest['high'] >= pos['sl']

        if hit_tp or hit_sl:
            # 同帧双触：开盘价判断
            if hit_tp and hit_sl:
                op = latest['open']
                hit_tp = abs(op - pos['tp']) <= abs(op - pos['sl'])
                hit_sl = not hit_tp

            exit_p = pos['tp'] if hit_tp else pos['sl']
            pnl_pct = (exit_p / pos['entry'] - 1) * pos['dir']
            pnl = pos['risk'] * (pnl_pct / pos['sl_dist_pct'] - FEE * 2)
            equity += pnl

            trade = {
                'id': len(all_trades) + 1,
                'sym': sym,
                'dir': 'LONG' if pos['dir'] == 1 else 'SHORT',
                'entry': pos['entry'],
                'exit': exit_p,
                'tp': pos['tp'],
                'sl': pos['sl'],
                'win': hit_tp,
                'pnl': round(pnl, 4),
                'equity': round(equity, 2),
                'open_time': pos['open_time'],
                'close_time': now,
                'reason': 'TP' if hit_tp else 'SL'
            }
            all_trades.append(trade)
            closed.append(sym)
            emoji = "✅" if hit_tp else "❌"
            print(f"  {emoji} [{sym}] {trade['dir']} 平仓 {trade['reason']} | PnL={pnl:+.3f}U | 权益={equity:.2f}U")

    for sym in closed:
        del positions[sym]

    # 2. 检查新信号（最多持仓3个品种）
    if len(positions) < RISK_CFG['max_concurrent_positions']:
        for sym, params in SYMBOLS.items():
            if sym in positions: continue
            if len(positions) >= RISK_CFG['max_concurrent_positions']: break

            sig, sig_row, df = check_latest_signal(sym, params)
            if sig is None: continue

            # 用下一根K线（当前最新未完成K线）的open价开仓
            entry = df.iloc[-1]['open']  # 最新K线开盘价（近似实盘）
            atr = sig_row['atr']
            if atr <= 0: continue

            pos_scale = params.get('position_scale', 1.0)
            risk_amt = equity * RISK_PCT * pos_scale

            if sig == -1:  # SHORT
                sl = entry + 1.0 * atr
                tp = entry - params['tp_s'] * atr
            else:           # LONG
                sl = entry - 1.0 * atr
                tp = entry + params['tp_l'] * atr

            sl_dist_pct = abs(entry - sl) / entry
            if sl_dist_pct <= 0: continue

            positions[sym] = {
                'dir': sig,
                'entry': entry,
                'sl': sl,
                'tp': tp,
                'risk': risk_amt,
                'sl_dist_pct': sl_dist_pct,
                'open_time': now
            }
            dir_str = 'LONG' if sig == 1 else 'SHORT'
            new_signals.append(sym)
            print(f"  📍 [{sym}] 新开仓 {dir_str} @ {entry:.4f} | TP={tp:.4f} SL={sl:.4f} | 风险={risk_amt:.2f}U")

    state['equity'] = equity
    state['open_positions'] = positions
    return state, all_trades

# ── 统计摘要 ──────────────────────────────────────────
def print_summary(trades, equity):
    if not trades:
        print("  暂无已完成交易")
        return
    wins = [t for t in trades if t['win']]
    losses = [t for t in trades if not t['win']]
    wr = len(wins) / len(trades) * 100
    total_pnl = sum(t['pnl'] for t in trades)
    gp = sum(t['pnl'] for t in wins)
    gl = abs(sum(t['pnl'] for t in losses)) or 1e-9
    pf = gp / gl

    print(f"\n  📊 纸交易统计 | 共{len(trades)}笔")
    print(f"  WR={wr:.1f}% | PF={pf:.2f} | 总PnL={total_pnl:+.2f}U")
    print(f"  当前权益={equity:.2f}U | 目标达成: {'✅' if wr>=58 else '⏳'}")
    print(f"  进度: {len(trades)}/100笔 ({'✅达标' if len(trades)>=100 else f'剩余{100-len(trades)}笔'})")

    # 写摘要文件
    summary = {
        'updated': datetime.now(timezone.utc).isoformat(),
        'total_trades': len(trades),
        'wins': len(wins),
        'losses': len(losses),
        'wr_pct': round(wr, 1),
        'pf': round(pf, 2),
        'total_pnl': round(total_pnl, 2),
        'equity': round(equity, 2),
        'phase5_complete': len(trades) >= 100 and wr >= 58
    }
    with open(SUMMARY_FILE, 'w') as f:
        json.dump(summary, f, indent=2)

# ── 主循环 ────────────────────────────────────────────
def main():
    print(f"\n{'='*50}")
    print(f"🚀 白夜系统 Phase5 纸交易引擎启动")
    print(f"   时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"   品种: {list(SYMBOLS.keys())}")
    print(f"   资金: {CAPITAL_INIT}U | 风险: {RISK_PCT*100}%/笔")
    print(f"   目标: ≥100笔 + WR≥58%")
    print(f"{'='*50}\n")

    state = load_state()
    all_trades = load_trades()

    print(f"  已有交易: {len(all_trades)}笔 | 当前权益: {state['equity']:.2f}U")
    if state['open_positions']:
        print(f"  持仓中: {list(state['open_positions'].keys())}")

    # 执行一轮扫描
    print(f"\n  🔍 扫描信号...")
    state, all_trades = paper_trade_once(state, all_trades)

    save_state(state)
    save_trades(all_trades)
    print_summary(all_trades, state['equity'])

    print(f"\n  ✅ 本轮扫描完成 | {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")
    print(f"  下次运行: 15分钟后（cron: */15 * * * * python3 paper_trading.py）\n")

if __name__ == '__main__':
    main()
