#!/usr/bin/env python3
"""深度测试：我们的参数 vs v6.1参数，全量WF验证 + 多维分析"""
import sys, pandas as pd, numpy as np
sys.path.insert(0, 'engine')
from white_night_v6_1 import Indicators, SignalEngine, BacktestEngine, Params, WalkForward, calc_stats
import inspect

# 检查 WalkForward.validate 签名
sig = inspect.signature(WalkForward.validate)
print("WalkForward.validate 参数:", list(sig.parameters.keys()))
print()

# 我们的原始最优参数（网格优化后，sl=1.0）
OUR_PARAMS = {
    'BTCUSDT':  {'sc':5,'lc':5,'ccp':0.003,'adx_th':20,'tp_s':1.0,'tp_l':1.0,'sl_atr':1.0,'long_disabled':False,'adx_dynamic_tp':False},
    'LINKUSDT': {'sc':7,'lc':4,'ccp':0.0025,'adx_th':15,'tp_s':0.8,'tp_l':0.7,'sl_atr':1.0,'long_disabled':False,'adx_dynamic_tp':False},
    'POLUSDT':  {'sc':5,'lc':4,'ccp':0.0015,'adx_th':25,'tp_s':1.0,'tp_l':0.7,'sl_atr':1.0,'long_disabled':False,'adx_dynamic_tp':False},
    'ETHUSDT':  {'sc':5,'lc':4,'ccp':0.0015,'adx_th':20,'tp_s':0.8,'tp_l':0.7,'sl_atr':1.0,'long_disabled':False,'adx_dynamic_tp':False},
    'SOLUSDT':  {'sc':5,'lc':4,'ccp':0.0015,'adx_th':25,'tp_s':0.8,'tp_l':0.8,'sl_atr':1.0,'long_disabled':False,'adx_dynamic_tp':False},
    'BNBUSDT':  {'sc':5,'lc':6,'ccp':0.0015,'adx_th':15,'tp_s':0.8,'tp_l':0.8,'sl_atr':1.0,'long_disabled':True,'adx_dynamic_tp':True},
}

symbols = ['BTCUSDT','LINKUSDT','POLUSDT','ETHUSDT','SOLUSDT','BNBUSDT']

print("=" * 80)
print("  【测试A】全量回测对比：我们的参数(sl=1.0) vs v6.1参数(sl=1.5)")
print("=" * 80)
print(f"  {'品种':10} {'WR_ours':>9} {'月均_ours':>10} {'PF_ours':>9} || {'WR_v61':>8} {'月均_v61':>9} {'PF_v61':>8}")
print("-" * 80)

for sym in symbols:
    df = pd.read_csv(f'data/{sym}_15m_180d.csv')
    df['ts'] = pd.to_datetime(df['ts'])
    df = df.set_index('ts').sort_index()
    df.columns = [c.lower() for c in df.columns]
    df = Indicators.compute(df)
    days = (df.index[-1] - df.index[0]).days

    # 我们的参数
    p = OUR_PARAMS[sym]
    sigs = SignalEngine.generate_core(df, sc=p['sc'], lc=p['lc'], ccp=p['ccp'], adx_th=p['adx_th'], long_disabled=p['long_disabled'])
    t1, _ = BacktestEngine.run(df, sigs, tp_s=p['tp_s'], tp_l=p['tp_l'], sl_atr=p['sl_atr'], adx_dynamic_tp=p['adx_dynamic_tp'])
    s1 = calc_stats(t1, days=days)

    # v6.1参数
    pv = Params.get(sym, '15m')
    sigsv = SignalEngine.generate_core(df, sc=pv['sc'], lc=pv['lc'], ccp=pv['ccp'], adx_th=pv['adx_th'], long_disabled=pv.get('long_disabled',False))
    t2, _ = BacktestEngine.run(df, sigsv, tp_s=pv['tp_s'], tp_l=pv['tp_l'], sl_atr=pv['sl_atr'], adx_dynamic_tp=pv.get('adx_dynamic_tp',False))
    s2 = calc_stats(t2, days=days)

    mark = ">" if s1.monthly > s2.monthly else " "
    print(f"  {sym:10} {s1.wr:>8.1f}% {s1.monthly:>+9.1f}% {s1.pf:>8.2f} || {s2.wr:>7.1f}% {s2.monthly:>+8.1f}% {s2.pf:>7.2f} {mark}")

print()
print("=" * 80)
print("  【测试B】Walk-Forward 验证 — 我们的参数 (70/30分割)")
print("=" * 80)
print(f"  {'品种':10} {'样本内WR':>9} {'样本外WR':>9} {'WR降幅':>9} {'结论':>10}")
print("-" * 80)

wf_results = {}
for sym in symbols:
    df = pd.read_csv(f'data/{sym}_15m_180d.csv')
    df['ts'] = pd.to_datetime(df['ts'])
    df = df.set_index('ts').sort_index()
    df.columns = [c.lower() for c in df.columns]
    df = Indicators.compute(df)
    
    p = OUR_PARAMS[sym]
    # 手动WF：前70%训练，后30%测试
    n = len(df)
    split = int(n * 0.7)
    df_train = df.iloc[:split]
    df_test  = df.iloc[split:]
    days_train = (df_train.index[-1] - df_train.index[0]).days
    days_test  = (df_test.index[-1]  - df_test.index[0]).days

    sigs_tr = SignalEngine.generate_core(df_train, sc=p['sc'], lc=p['lc'], ccp=p['ccp'], adx_th=p['adx_th'], long_disabled=p['long_disabled'])
    t_tr, _ = BacktestEngine.run(df_train, sigs_tr, tp_s=p['tp_s'], tp_l=p['tp_l'], sl_atr=p['sl_atr'], adx_dynamic_tp=p['adx_dynamic_tp'])
    s_tr = calc_stats(t_tr, days=days_train)

    sigs_te = SignalEngine.generate_core(df_test, sc=p['sc'], lc=p['lc'], ccp=p['ccp'], adx_th=p['adx_th'], long_disabled=p['long_disabled'])
    t_te, _ = BacktestEngine.run(df_test, sigs_te, tp_s=p['tp_s'], tp_l=p['tp_l'], sl_atr=p['sl_atr'], adx_dynamic_tp=p['adx_dynamic_tp'])
    s_te = calc_stats(t_te, days=days_test)

    drop = s_te.wr - s_tr.wr
    overfit = drop < -10
    verdict = "🔴过拟合" if overfit else "✅稳健"
    wf_results[sym] = {'train_wr': s_tr.wr, 'test_wr': s_te.wr, 'drop': drop, 'overfit': overfit,
                       'train_monthly': s_tr.monthly, 'test_monthly': s_te.monthly}
    print(f"  {sym:10} {s_tr.wr:>8.1f}% {s_te.wr:>8.1f}% {drop:>+8.1f}% {verdict:>10}")

print()
print("=" * 80)
print("  【测试C】参数敏感性分析 — BTC sl_atr 扫描 (0.5~2.0)")
print("=" * 80)
print(f"  {'sl_atr':>8} {'WR':>7} {'月均%':>8} {'PF':>6} {'DD%':>6} {'交易数':>6}")
print("-" * 50)

df_btc = pd.read_csv('data/BTCUSDT_15m_180d.csv')
df_btc['ts'] = pd.to_datetime(df_btc['ts'])
df_btc = df_btc.set_index('ts').sort_index()
df_btc.columns = [c.lower() for c in df_btc.columns]
df_btc = Indicators.compute(df_btc)
days_btc = (df_btc.index[-1] - df_btc.index[0]).days
p0 = OUR_PARAMS['BTCUSDT']
sigs0 = SignalEngine.generate_core(df_btc, sc=p0['sc'], lc=p0['lc'], ccp=p0['ccp'], adx_th=p0['adx_th'])

for sl in [0.5, 0.8, 1.0, 1.2, 1.5, 1.8, 2.0]:
    t, _ = BacktestEngine.run(df_btc, sigs0, tp_s=p0['tp_s'], tp_l=p0['tp_l'], sl_atr=sl)
    s = calc_stats(t, days=days_btc)
    mark = " <<我们" if sl == 1.0 else (" <<v6.1" if sl == 1.5 else "")
    print(f"  {sl:>8.1f} {s.wr:>6.1f}% {s.monthly:>+7.1f}% {s.pf:>5.2f} {s.max_dd:>5.1f}% {s.trades:>5}{mark}")

print()
print("=" * 80)
print("  【测试D】TP 扫描 — BTC (固定sl=1.0)")
print("=" * 80)
print(f"  {'tp_s/tp_l':>12} {'WR':>7} {'月均%':>8} {'PF':>6} {'DD%':>6}")
print("-" * 50)

for tp_s, tp_l in [(0.5,0.5),(0.6,0.5),(0.7,0.6),(0.8,0.7),(0.8,0.8),(1.0,0.8),(1.0,1.0),(1.2,1.0),(1.5,1.2)]:
    t, _ = BacktestEngine.run(df_btc, sigs0, tp_s=tp_s, tp_l=tp_l, sl_atr=1.0)
    s = calc_stats(t, days=days_btc)
    mark = " <<我们" if (tp_s==1.0 and tp_l==1.0) else (" <<v6.1" if (tp_s==0.6 and tp_l==0.5) else "")
    print(f"  {tp_s}/{tp_l:>4} {' ':>5} {s.wr:>6.1f}% {s.monthly:>+7.1f}% {s.pf:>5.2f} {s.max_dd:>5.1f}%{mark}")

print()
print("=" * 80)
print("  【测试E】月度稳定性 — 我们的参数 vs v6.1 (BTC 6个月分解)")
print("=" * 80)

df_btc2 = pd.read_csv('data/BTCUSDT_15m_180d.csv')
df_btc2['ts'] = pd.to_datetime(df_btc2['ts'])
df_btc2 = df_btc2.set_index('ts').sort_index()
df_btc2.columns = [c.lower() for c in df_btc2.columns]
df_btc2 = Indicators.compute(df_btc2)

months = sorted(df_btc2.index.to_period('M').unique())
print(f"  {'月份':>10} {'我们月均%':>10} {'v6.1月均%':>10} {'我们WR':>8} {'v6.1WR':>8}")
print("-" * 55)

for m in months:
    mask = df_btc2.index.to_period('M') == m
    df_m = df_btc2[mask]
    if len(df_m) < 100: continue
    days_m = max((df_m.index[-1]-df_m.index[0]).days, 1)
    
    sigs_m = SignalEngine.generate_core(df_m, sc=5, lc=5, ccp=0.003, adx_th=20)
    t_our, _ = BacktestEngine.run(df_m, sigs_m, tp_s=1.0, tp_l=1.0, sl_atr=1.0)
    s_our = calc_stats(t_our, days=days_m)
    
    pv = Params.get('BTCUSDT','15m')
    sigs_v = SignalEngine.generate_core(df_m, sc=pv['sc'], lc=pv['lc'], ccp=pv['ccp'], adx_th=pv['adx_th'])
    t_v61, _ = BacktestEngine.run(df_m, sigs_v, tp_s=pv['tp_s'], tp_l=pv['tp_l'], sl_atr=pv['sl_atr'])
    s_v61 = calc_stats(t_v61, days=days_m)
    
    win = "◀" if s_our.monthly > s_v61.monthly else " "
    print(f"  {str(m):>10} {s_our.monthly:>+9.1f}% {s_v61.monthly:>+9.1f}% {s_our.wr:>7.1f}% {s_v61.wr:>7.1f}% {win}")

print()
print("=" * 80)
print("  【测试F】6品种组合总收益对比（150U本金，复利）")
print("=" * 80)

# 资金分配（夏普加权）
ALLOC = {'BTCUSDT':0.189,'LINKUSDT':0.218,'POLUSDT':0.105,'ETHUSDT':0.183,'SOLUSDT':0.166,'BNBUSDT':0.139}
total_our = 150.0
total_v61 = 150.0

for sym in symbols:
    cap = 150.0 * ALLOC[sym]
    df = pd.read_csv(f'data/{sym}_15m_180d.csv')
    df['ts'] = pd.to_datetime(df['ts'])
    df = df.set_index('ts').sort_index()
    df.columns = [c.lower() for c in df.columns]
    df = Indicators.compute(df)
    days = (df.index[-1] - df.index[0]).days

    p = OUR_PARAMS[sym]
    sigs = SignalEngine.generate_core(df, sc=p['sc'], lc=p['lc'], ccp=p['ccp'], adx_th=p['adx_th'], long_disabled=p['long_disabled'])
    t1, eq1 = BacktestEngine.run(df, sigs, tp_s=p['tp_s'], tp_l=p['tp_l'], sl_atr=p['sl_atr'], capital=cap)
    
    pv = Params.get(sym,'15m')
    sigsv = SignalEngine.generate_core(df, sc=pv['sc'], lc=pv['lc'], ccp=pv['ccp'], adx_th=pv['adx_th'], long_disabled=pv.get('long_disabled',False))
    t2, eq2 = BacktestEngine.run(df, sigsv, tp_s=pv['tp_s'], tp_l=pv['tp_l'], sl_atr=pv['sl_atr'], capital=cap)
    
    pnl1 = eq1[-1] - cap if len(eq1) > 0 else 0
    pnl2 = eq2[-1] - cap if len(eq2) > 0 else 0
    total_our += pnl1
    total_v61 += pnl2

print(f"  150U 初始资金，6品种按夏普加权分配，180天复利:")
print(f"  我们的参数: 终值 {total_our:.1f}U  (+{(total_our-150)/150*100:.1f}%)")
print(f"  v6.1参数:   终值 {total_v61:.1f}U  (+{(total_v61-150)/150*100:.1f}%)")
print()
print("  ✅ 测试完成")
