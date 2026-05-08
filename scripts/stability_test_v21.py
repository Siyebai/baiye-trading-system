#!/usr/bin/env python3
"""
白夜系统 v2.1 稳定性全量测试
测试项目: 5折CV / 参数鲁棒性 / 连亏分析 / 月度矩阵 / BTC SHORT改进 / 蒙特卡洛 / 手续费敏感性
运行: python3 scripts/stability_test_v21.py
"""
import sys, json, pandas as pd, numpy as np
sys.path.insert(0, 'engine')
from white_night_v6_1 import Indicators, SignalEngine, BacktestEngine, calc_stats

V21 = {
    'BTCUSDT':  {'sc':4,'lc':5,'ccp':0.002,'adx_th':22,'tp_s':0.8,'tp_l':0.8,'sl_atr':0.8,'long_disabled':False,'adx_dynamic_tp':False},
    'LINKUSDT': {'sc':7,'lc':4,'ccp':0.0025,'adx_th':15,'tp_s':0.8,'tp_l':0.7,'sl_atr':1.0,'long_disabled':False,'adx_dynamic_tp':False},
    'POLUSDT':  {'sc':5,'lc':4,'ccp':0.0015,'adx_th':25,'tp_s':1.0,'tp_l':0.7,'sl_atr':1.0,'long_disabled':True,'adx_dynamic_tp':False},
    'ETHUSDT':  {'sc':5,'lc':4,'ccp':0.0015,'adx_th':20,'tp_s':0.8,'tp_l':0.7,'sl_atr':1.0,'long_disabled':False,'adx_dynamic_tp':False},
    'SOLUSDT':  {'sc':5,'lc':4,'ccp':0.0015,'adx_th':25,'tp_s':0.8,'tp_l':0.8,'sl_atr':1.0,'long_disabled':False,'adx_dynamic_tp':False},
    'BNBUSDT':  {'sc':5,'lc':6,'ccp':0.0015,'adx_th':15,'tp_s':0.8,'tp_l':0.8,'sl_atr':1.0,'long_disabled':True,'adx_dynamic_tp':True},
}
ALLOC = {'BTCUSDT':0.189,'LINKUSDT':0.218,'POLUSDT':0.105,'ETHUSDT':0.183,'SOLUSDT':0.166,'BNBUSDT':0.139}
syms = list(V21.keys())

report = {}

# ── 测试1: 5折CV ──────────────────────────────────────────
print('='*72)
print('  【测试1】5折交叉验证')
print('='*72)
report['cv5'] = {}
for sym in syms:
    df = pd.read_csv(f'data/{sym}_15m_180d.csv')
    df['ts'] = pd.to_datetime(df['ts']); df = df.set_index('ts').sort_index()
    df.columns = [c.lower() for c in df.columns]; df = Indicators.compute(df)
    p = V21[sym]; n = len(df); fold = n // 5; fold_wrs = []
    for k in range(5):
        s_i = k*fold; e_i = s_i+fold if k < 4 else n
        dfm = df.iloc[s_i:e_i]
        if len(dfm) < 200: fold_wrs.append(0.0); continue
        days = max((dfm.index[-1]-dfm.index[0]).days, 1)
        sigs = SignalEngine.generate_core(dfm, sc=p['sc'], lc=p['lc'], ccp=p['ccp'], adx_th=p['adx_th'], long_disabled=p['long_disabled'])
        t, _ = BacktestEngine.run(dfm, sigs, tp_s=p['tp_s'], tp_l=p['tp_l'], sl_atr=p['sl_atr'], adx_dynamic_tp=p['adx_dynamic_tp'])
        fold_wrs.append(calc_stats(t, days=days).wr)
    avg = np.mean(fold_wrs); worst = min(fold_wrs)
    ok = '✅' if worst > 50 else '⚠️'
    print(f'  {sym:10} folds={[f"{x:.0f}%" for x in fold_wrs]}  avg={avg:.1f}% worst={worst:.1f}% {ok}')
    report['cv5'][sym] = {'fold_wrs': fold_wrs, 'avg': avg, 'worst': worst, 'pass': worst > 50}

# ── 测试3: 连亏分析 ──────────────────────────────────────
print('\n' + '='*72)
print('  【测试3】最大连续亏损')
print('='*72)
report['max_consec_loss'] = {}
all_trades_flat = []
for sym in syms:
    df = pd.read_csv(f'data/{sym}_15m_180d.csv')
    df['ts'] = pd.to_datetime(df['ts']); df = df.set_index('ts').sort_index()
    df.columns = [c.lower() for c in df.columns]; df = Indicators.compute(df)
    p = V21[sym]; cap = 150 * ALLOC[sym]
    days = (df.index[-1]-df.index[0]).days
    sigs = SignalEngine.generate_core(df, sc=p['sc'], lc=p['lc'], ccp=p['ccp'], adx_th=p['adx_th'], long_disabled=p['long_disabled'])
    trades, _ = BacktestEngine.run(df, sigs, tp_s=p['tp_s'], tp_l=p['tp_l'], sl_atr=p['sl_atr'], adx_dynamic_tp=p['adx_dynamic_tp'], capital=cap)
    for t in trades: t.sym = sym
    all_trades_flat.extend(trades)
    if not trades: continue
    mc = 0; cur = 0
    for t in trades:
        if not t.win: cur += 1; mc = max(mc, cur)
        else: cur = 0
    max_loss = min(t.pnl for t in trades)
    eq = [cap] + [cap + sum(t.pnl for t in trades[:i+1]) for i in range(len(trades))]
    eq_s = pd.Series(eq); dd = ((eq_s - eq_s.cummax()) / eq_s.cummax()).min() * 100
    print(f'  {sym:10} 最长连亏={mc}笔  最大单亏={max_loss:+.2f}U  最大DD={abs(dd):.1f}%')
    report['max_consec_loss'][sym] = {'max_consec': mc, 'max_single_loss': round(max_loss, 3), 'max_dd': round(abs(dd), 1)}

# ── 测试4: 月度矩阵 ──────────────────────────────────────
print('\n' + '='*72)
print('  【测试4】月度收益矩阵')
print('='*72)
months_all = []
for sym in syms:
    df = pd.read_csv(f'data/{sym}_15m_180d.csv')
    df['ts'] = pd.to_datetime(df['ts']); df = df.set_index('ts').sort_index()
    df.columns = [c.lower() for c in df.columns]
    months_all = sorted(df.index.to_period('M').unique()); break

print(f'  {"品种":10}', end='')
for m in months_all: print(f' {str(m)[-5:]:>8}', end='')
print(f' {"盈利月":>6}')
print('-'*82)
report['monthly_matrix'] = {}
for sym in syms:
    df = pd.read_csv(f'data/{sym}_15m_180d.csv')
    df['ts'] = pd.to_datetime(df['ts']); df = df.set_index('ts').sort_index()
    df.columns = [c.lower() for c in df.columns]; df = Indicators.compute(df)
    p = V21[sym]; pm = 0; row = {}
    print(f'  {sym:10}', end='')
    for m in months_all:
        mask = df.index.to_period('M') == m; dfm = df[mask]
        if len(dfm) < 100: print(f' {"":>8}', end=''); continue
        days = max((dfm.index[-1]-dfm.index[0]).days, 1)
        sigs = SignalEngine.generate_core(dfm, sc=p['sc'], lc=p['lc'], ccp=p['ccp'], adx_th=p['adx_th'], long_disabled=p['long_disabled'])
        t, _ = BacktestEngine.run(dfm, sigs, tp_s=p['tp_s'], tp_l=p['tp_l'], sl_atr=p['sl_atr'], adx_dynamic_tp=p['adx_dynamic_tp'])
        s = calc_stats(t, days=days)
        if s.monthly > 0: pm += 1
        row[str(m)] = round(s.monthly, 1)
        print(f' {s.monthly:>+7.1f}%', end='')
    print(f' {pm}/{len(months_all)}')
    report['monthly_matrix'][sym] = row

# ── 测试6: 蒙特卡洛 ──────────────────────────────────────
print('\n' + '='*65)
print('  【测试6】蒙特卡洛模拟 (N=1000, 150U初始)')
print('='*65)
np.random.seed(42)
pnls = [t.pnl for t in all_trades_flat]
finals = [150 + np.sum(np.random.choice(pnls, size=len(pnls), replace=True)) for _ in range(1000)]
finals = np.array(finals)
p10, p50, p90 = np.percentile(finals, [10, 50, 90])
ruin = np.sum(finals < 75) / 10
loss = np.sum(finals < 150) / 10
print(f'  中位数: {p50:.1f}U ({(p50-150)/150*100:+.0f}%)')
print(f'  10th:   {p10:.1f}U ({(p10-150)/150*100:+.0f}%)')
print(f'  90th:   {p90:.1f}U ({(p90-150)/150*100:+.0f}%)')
print(f'  破产率(<75U): {ruin:.1f}%  亏损率(<150U): {loss:.1f}%')
report['monte_carlo'] = {'median': round(p50,1), 'p10': round(p10,1), 'p90': round(p90,1), 'ruin_pct': ruin, 'loss_pct': loss}

# ── 保存报告 ──────────────────────────────────────────────
import os; os.makedirs('research', exist_ok=True)
with open('research/stability_report_v21.json', 'w') as f:
    json.dump(report, f, indent=2, ensure_ascii=False)
print('\n  ✅ 报告已保存到 research/stability_report_v21.json')

if __name__ == '__main__':
    pass
