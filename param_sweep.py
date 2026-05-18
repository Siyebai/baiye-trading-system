#!/usr/bin/env python3
"""参数扫描工具 — SOLUSDT 单品种优化"""
import sys
sys.path.insert(0, '.')
import pandas as pd
import numpy as np
from offline_replay import compute_indicators, _real_cc

df = pd.read_csv('data/SOLUSDT_15m_180d.csv')
for c in ['open', 'high', 'low', 'close', 'vol']:
    df[c] = df[c].astype(float)
df = compute_indicators(df)

INIT, FEE = 150.0, 0.0002
TRAIL, T_BE, T_LK, T_DY, T_DD = True, 1.0, 1.5, 2.0, 1.0


def sweep(df, sc, tp_m, sl_m):
    eq, pk, max_dd = INIT, INIT, 0.0
    trades = []
    pos = None
    cd_bar = 0

    for idx in range(60, len(df)):
        row = df.iloc[idx - 1]
        cur = df.iloc[idx]
        h2, lo, op = float(cur['high']), float(cur['low']), float(cur['open'])

        # ── 退出检查 ──
        if pos is not None:
            pos['bars_held'] += 1
            atr_p = pos.get('atr', 0)
            e_p = pos['entry']
            s_p = pos['side']

            if TRAIL and atr_p > 0:
                if s_p == 'short':
                    fpa = (e_p - lo) / atr_p
                    if fpa >= T_BE:
                        ns = e_p
                        if fpa >= T_LK:
                            ns = min(ns, lo + 0.3 * atr_p)
                        if fpa >= T_DY:
                            ns = min(ns, lo + T_DD * atr_p)
                        if ns < pos['sl']:
                            pos['sl'] = ns
                else:
                    fpa = (h2 - e_p) / atr_p
                    if fpa >= T_BE:
                        ns = e_p
                        if fpa >= T_LK:
                            ns = max(ns, h2 - 0.3 * atr_p)
                        if fpa >= T_DY:
                            ns = max(ns, h2 - T_DD * atr_p)
                        if ns > pos['sl']:
                            pos['sl'] = ns

            sl_p, tp_p = pos['sl'], pos['tp']
            res, ex = None, None

            # 检查是否触发
            if s_p == 'short':
                if lo <= tp_p and h2 >= sl_p:
                    res, ex = ('SL', sl_p) if op >= e_p else ('TP', tp_p)
                elif lo <= tp_p:
                    res, ex = 'TP', tp_p
                elif h2 >= sl_p:
                    res, ex = 'SL', sl_p
            else:
                if h2 >= tp_p and lo <= sl_p:
                    res, ex = ('SL', sl_p) if op <= e_p else ('TP', tp_p)
                elif h2 >= tp_p:
                    res, ex = 'TP', tp_p
                elif lo <= sl_p:
                    res, ex = 'SL', sl_p

            # TIMEOUT
            if res is None and pos['bars_held'] >= 40:
                res, ex = 'TIMEOUT', (h2 + lo) / 2

            if res is not None:
                qty = pos['qty']
                raw = ((e_p - ex) * qty) if s_p == 'short' else ((ex - e_p) * qty)
                net = raw - (e_p + ex) * qty * FEE
                eq += net
                pk = max(eq, pk)
                dd = (pk - eq) / pk * 100
                if dd > max_dd:
                    max_dd = dd
                trades.append({'net_pnl': net, 'result': res, 'bars': pos['bars_held']})
                pos = None

        # ── 信号生成 ──
        if pos is not None:
            continue
        if idx < cd_bar:
            continue

        adx = float(row['adx']) if not np.isnan(row['adx']) else 0
        atr = float(row['atr']) if not np.isnan(row['atr']) else 0
        if adx > 20 or atr <= 0:  # 均值回归=震荡市(低ADX≤20)才交易
            continue

        cu = int(row['cu'])
        cd = int(row['cd'])
        entry = float(cur['close'])
        ema = float(row['ema200']) if not np.isnan(row['ema200']) else 0

        # SHORT 信号
        short_cc = _real_cc(df, idx - 1, cu) if cu >= sc else 0.0
        if cu >= sc and short_cc >= 0.001:
            sl = entry + sl_m * atr
            tp = entry - tp_m * atr
            rr = abs(tp - entry) / max(abs(entry - sl), 1e-9)
            if rr >= 0.5:
                sp = abs(entry - sl) / entry
                ru = INIT * 0.015 / max(sp, 0.001)
                sd = abs(entry - sl)
                if sd > 0:
                    qty = ru / sd
                    notional = qty * entry
                    if notional >= 5.0:
                        cd_bar = idx + 1
                        pos = {
                            'side': 'short', 'entry': entry,
                            'sl': sl, 'tp': tp,
                            'atr': atr, 'qty': qty, 'bars_held': 0
                        }
                        continue

        # LONG 信号
        long_cc = _real_cc(df, idx - 1, cd) if cd >= sc else 0.0
        if cd >= sc and long_cc <= -0.001 and entry > ema:
            sl = entry - sl_m * atr
            tp = entry + tp_m * atr
            rr = abs(tp - entry) / max(abs(entry - sl), 1e-9)
            if rr >= 0.5:
                sp = abs(entry - sl) / entry
                ru = INIT * 0.015 / max(sp, 0.001)
                sd = abs(entry - sl)
                if sd > 0:
                    qty = ru / sd
                    notional = qty * entry
                    if notional >= 5.0:
                        cd_bar = idx + 1
                        pos = {
                            'side': 'long', 'entry': entry,
                            'sl': sl, 'tp': tp,
                            'atr': atr, 'qty': qty, 'bars_held': 0
                        }

    if not trades:
        return None

    N = len(trades)
    wins = sum(1 for t in trades if t['net_pnl'] > 0)
    rets = [t['net_pnl'] for t in trades]
    total_ret = (eq - INIT) / INIT * 100
    tp_n = sum(1 for t in trades if t['result'] == 'TP')
    sl_n = sum(1 for t in trades if t['result'] == 'SL')
    to_n = sum(1 for t in trades if t['result'] == 'TIMEOUT')
    sharpe = np.mean(rets) / max(np.std(rets, ddof=1), 1e-9) if N >= 5 else 0
    w = [r for r in rets if r > 0]
    l = [r for r in rets if r <= 0]
    pf = sum(w) / abs(sum(l)) if l and sum(l) != 0 else 99.0
    return {
        'trades': N, 'wr%': round(wins / N * 100, 1),
        'pf': round(pf, 2), 'sharpe': round(sharpe, 3),
        'ret%': round(total_ret, 2), 'dd%': round(max_dd, 2),
        'tp': tp_n, 'sl': sl_n, 'to': to_n
    }


# ═══════════════════════ 网格搜索 ═══════════════════════
results = []
for sc in [1, 2, 3, 4]:
    for tp_m in [1.0, 1.5, 2.0, 2.5, 3.0]:
        for sl_m in [1.5, 2.0, 2.5, 3.0]:
            r = sweep(df, sc=sc, tp_m=tp_m, sl_m=sl_m)
            if r:
                results.append({**r, 'sc': sc, 'tp_m': tp_m, 'sl_m': sl_m})

results.sort(key=lambda x: (x['wr%'], x['sharpe']), reverse=True)

print(f"{'Rank':>4s} {'sc':>3s} {'tp_m':>5s} {'sl_m':>5s} "
      f"{'trades':>6s} {'WR%':>7s} {'PF':>6s} {'Sharpe':>7s} "
      f"{'Ret%':>7s} {'DD%':>6s} {'TP/SL/TO':>10s}")
print('─' * 85)
for i, r in enumerate(results[:25]):
    print(f"{i+1:4d} {r['sc']:3d} {r['tp_m']:5.1f} {r['sl_m']:5.1f} "
          f"{r['trades']:6d} {r['wr%']:6.1f}% {r['pf']:5.2f} "
          f"{r['sharpe']:7.3f} {r['ret%']:+6.2f}% {r['dd%']:6.2f}% "
          f"{r['tp']:3d}/{r['sl']:3d}/{r['to']:3d}")

# 最佳参数
print(f"\n✅ 最佳参数 (WR优先): sc={results[0]['sc']}, "
      f"TP={results[0]['tp_m']}xATR, SL={results[0]['sl_m']}xATR")
print(f"   交易:{results[0]['trades']}笔 WR:{results[0]['wr%']}% "
      f"PF:{results[0]['pf']} Sharpe:{results[0]['sharpe']} "
      f"收益:{results[0]['ret%']:+.2f}% DD:{results[0]['dd%']}%")
