#!/usr/bin/env python3
"""
扩展品种回测 — 白夜系统 v1.0
策略：SHORT(S4_MomReversal) + LONG(MomReversal)
品种：XRP, DOGE, ADA, LINK, AVAX, POL
时间框架：15m | 资金：150U | 风险2%=3U | 手续费0.09%单边
"""
import requests, json, time, numpy as np, pandas as pd

# ── 数据下载 ──────────────────────────────────────────
def fetch_klines(symbol, interval='15m', days=180):
    end = int(time.time() * 1000)
    start = end - days * 86400 * 1000
    url = 'https://fapi.binance.com/fapi/v1/klines'
    all_k = []
    while start < end:
        params = dict(symbol=symbol, interval=interval,
                      startTime=start, endTime=end, limit=1500)
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if not data or isinstance(data, dict): break
        all_k.extend(data)
        start = data[-1][0] + 1
        if len(data) < 1500: break
        time.sleep(0.2)
    df = pd.DataFrame(all_k, columns=[
        'ts','open','high','low','close','vol',
        'close_ts','qvol','trades','taker_buy','taker_buy_q','ignore'])
    for c in ['open','high','low','close','vol']:
        df[c] = df[c].astype(float)
    df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    df.set_index('ts', inplace=True)
    df.drop_duplicates(inplace=True)
    return df

# ── 指标计算 ──────────────────────────────────────────
def compute_indicators(df):
    df = df.copy()
    high, low, close = df['high'], df['low'], df['close']
    # ATR14
    tr = pd.concat([high - low,
                    (high - close.shift()).abs(),
                    (low  - close.shift()).abs()], axis=1).max(axis=1)
    df['atr'] = tr.ewm(span=14, adjust=False).mean()
    # ADX14
    up = high.diff(); down = -low.diff()
    pdm = up.where((up > down) & (up > 0), 0.0)
    ndm = down.where((down > up) & (down > 0), 0.0)
    atr_ema = df['atr']
    pdi = 100 * pdm.ewm(span=14, adjust=False).mean() / atr_ema
    ndi = 100 * ndm.ewm(span=14, adjust=False).mean() / atr_ema
    dx  = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    df['adx'] = dx.ewm(span=14, adjust=False).mean()
    # EMA200
    df['ema200'] = close.ewm(span=200, adjust=False).mean()
    # 连涨/连跌 + 累计涨跌幅（同向重置时从0计）
    chg_arr = close.pct_change().values
    n = len(df)
    cu_arr = np.zeros(n); cd_arr = np.zeros(n); cc_arr = np.zeros(n)
    cu = cd = 0; cc = 0.0
    for i in range(1, n):
        c = chg_arr[i]
        if np.isnan(c): continue
        if c > 0:
            cu += 1; cd = 0
            cc = c if cu == 1 else cc + c
        elif c < 0:
            cd += 1; cu = 0
            cc = c if cd == 1 else cc + c
        else:
            cu = cd = 0; cc = 0.0
        cu_arr[i] = cu; cd_arr[i] = cd; cc_arr[i] = cc
    df['consec_up']   = cu_arr
    df['consec_down'] = cd_arr
    df['cum_chg']     = cc_arr
    return df

# ── 信号生成（返回int数组：1=LONG, -1=SHORT, 0=无）──
def get_signals(df):
    n = len(df)
    sigs = np.zeros(n, dtype=np.int8)
    adx  = df['adx'].values
    cu   = df['consec_up'].values
    cd   = df['consec_down'].values
    cc   = df['cum_chg'].values
    cl   = df['close'].values
    ema  = df['ema200'].values
    for i in range(200, n):
        if adx[i] < 20: continue
        if cu[i] >= 6 and cc[i] >= 0.002:
            sigs[i] = -1  # SHORT
        elif cd[i] >= 4 and cc[i] <= -0.002 and cl[i] > ema[i]:
            sigs[i] = 1   # LONG
    return sigs

# ── 回测引擎 ──────────────────────────────────────────
FEE = 0.0009  # 0.09% 单边

def backtest(df, sigs, capital=150.0, risk_pct=0.02):
    atr_arr   = df['atr'].values
    high_arr  = df['high'].values
    low_arr   = df['low'].values
    close_arr = df['close'].values
    trades = []
    equity = capital
    pos = None

    for i in range(len(df)):
        # 检查平仓
        if pos is not None:
            if pos['dir'] == 1:   # LONG
                hit_tp = high_arr[i] >= pos['tp']
                hit_sl = low_arr[i]  <= pos['sl']
            else:                  # SHORT
                hit_tp = low_arr[i]  <= pos['tp']
                hit_sl = high_arr[i] >= pos['sl']

            if hit_tp or hit_sl:
                exit_p = pos['tp'] if hit_tp else pos['sl']
                pnl_pct = (exit_p / pos['entry'] - 1) * pos['dir']
                pnl = pos['risk'] * (pnl_pct / pos['sl_dist_pct'] - FEE * 2)
                equity += pnl
                trades.append({'win': hit_tp, 'pnl': pnl, 'equity': equity, 'dir': pos['dir']})
                pos = None

        # 开仓
        if pos is None and sigs[i] != 0:
            price = close_arr[i]
            atr   = atr_arr[i]
            if atr <= 0: continue
            if sigs[i] == -1:  # SHORT
                sl = price + 1.0 * atr
                tp = price - 1.0 * atr
            else:               # LONG
                sl = price - 1.0 * atr
                tp = price + 0.8 * atr
            sl_dist_pct = abs(price - sl) / price
            if sl_dist_pct <= 0: continue
            pos = dict(dir=int(sigs[i]), entry=price, sl=sl, tp=tp,
                       risk=equity * risk_pct, sl_dist_pct=sl_dist_pct)

    return trades

# ── 统计 ──────────────────────────────────────────────
def stats(trades, capital=150.0):
    if not trades: return {'trades': 0, 'wr': 0, 'monthly_return': 0, 'max_dd': 0, 'pf': 0, 'final_equity': capital}
    wins   = [t for t in trades if t['win']]
    losses = [t for t in trades if not t['win']]
    wr = len(wins) / len(trades)
    eq = [capital] + [t['equity'] for t in trades]
    eq_s = pd.Series(eq)
    dd = ((eq_s - eq_s.cummax()) / eq_s.cummax()).min()
    gp = sum(t['pnl'] for t in wins)
    gl = abs(sum(t['pnl'] for t in losses)) or 1e-9
    monthly = (eq[-1] / capital) ** (1/6) - 1
    return dict(
        trades=len(trades),
        wr=round(wr*100, 1),
        final_equity=round(eq[-1], 1),
        total_return=round((eq[-1]/capital - 1)*100, 1),
        monthly_return=round(monthly*100, 1),
        max_dd=round(abs(dd)*100, 1),
        pf=round(gp/gl, 2)
    )

# ── 主流程 ──────────────────────────────────────────────
SYMBOLS = ['XRPUSDT','DOGEUSDT','ADAUSDT','LINKUSDT','AVAXUSDT','POLUSDT']

results = {}
for sym in SYMBOLS:
    print(f"\n{'='*40}\n📥 {sym} 下载中...")
    try:
        df   = fetch_klines(sym, '15m', 180)
        print(f"   {len(df)} 根K线")
        df   = compute_indicators(df)
        sigs = get_signals(df)
        sc   = int((sigs != 0).sum())
        sh   = int((sigs == -1).sum())
        lo   = int((sigs ==  1).sum())
        print(f"   信号: {sc}个 (SHORT:{sh}, LONG:{lo})")
        trades = backtest(df, sigs)
        s = stats(trades)
        results[sym] = s
        print(f"   ✅ WR={s['wr']}% | 月均={s['monthly_return']}% | 回撤={s['max_dd']}% | PF={s['pf']} | 笔数={s['trades']} | 终值={s['final_equity']}U")
    except Exception as e:
        import traceback; traceback.print_exc()
        results[sym] = {'error': str(e)}

print("\n\n" + "="*55)
print("📊 扩展品种回测汇总（15m, 180天, 150U本金）")
print("="*55)
print(f"{'品种':<12} {'WR%':>6} {'月均%':>7} {'回撤%':>6} {'PF':>5} {'笔数':>5} {'终值U':>8}")
print("-"*55)
for sym, s in results.items():
    if 'error' in s:
        print(f"{sym:<12} ❌ {s['error'][:35]}")
    else:
        print(f"{sym:<12} {s['wr']:>5.1f}% {s['monthly_return']:>6.1f}% {s['max_dd']:>5.1f}% {s['pf']:>5.2f} {s['trades']:>5} {s['final_equity']:>7.1f}U")

with open('expand_backtest_results.json', 'w') as f:
    json.dump(results, f, indent=2, ensure_ascii=False)
print("\n✅ 结果保存: expand_backtest_results.json")
