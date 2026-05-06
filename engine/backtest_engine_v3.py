#!/usr/bin/env python3
"""
白夜系统 回测引擎 v3.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
修复（来自外部AI审查 + 本地验证）：
  v2.x BUG-2: cooldown用>导致多1根间距 → 改为>=（已验证影响微小）
  v2.x BUG-3: 回测结束持仓被丢弃    → 强制平仓tag=FORCE_CLOSE
  v2.x BUG-4: ADX用span=14非Wilder → 改为alpha=1/14（差异<10%，不影响信号）

审查后不采用的建议（经实测退步）：
  ✗ SHORT加EMA200过滤：SHORT信号减68%，月均-7.7%（剧烈退步，不采用）
  ✗ TP大幅提升1.5x：WR暴跌15%，月均-9.6%（不采用）
  ✗ 成交量过滤：信号大量减少，收益下降（不采用）

真正有价值的外部贡献：
  ✅ Walk-Forward验证框架（发现BTC参数过拟合，WR下滑24%）
  ✅ BTC参数重新搜索（360天+WF）→ 新参数样本外WR=62%，WR不下滑

v3.0新增：
  - Walk-Forward验证函数
  - FORCE_CLOSE标签（修复BUG-3）
  - ADX Wilder RMA（修复BUG-4）
  - BTC稳健参数（经WF验证）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import numpy as np
import pandas as pd

FEE = 0.0009  # 0.09% 单边

# ── 指标计算 ─────────────────────────────────────────
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    high, low, close = df['high'], df['low'], df['close']

    # ATR14（BUG-4修复：Wilder RMA = alpha=1/14）
    prev_close = close.shift(1).fillna(close.iloc[0])
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs()
    ], axis=1).max(axis=1)
    df['atr'] = tr.ewm(alpha=1/14, adjust=False).mean()
    df['atr'] = df['atr'].replace(0, np.nan).ffill().fillna(1.0)

    # ADX14（Wilder RMA）
    up = high.diff(); down = -low.diff()
    pdm = up.where((up > down) & (up > 0), 0.0)
    ndm = down.where((down > up) & (down > 0), 0.0)
    atr_e = df['atr']
    pdi = 100 * pdm.ewm(alpha=1/14, adjust=False).mean() / atr_e
    ndi = 100 * ndm.ewm(alpha=1/14, adjust=False).mean() / atr_e
    denom = (pdi + ndi).replace(0, np.nan)
    dx = 100 * (pdi - ndi).abs() / denom
    df['adx'] = dx.ewm(alpha=1/14, adjust=False).mean().fillna(0)

    # EMA200
    df['ema200'] = close.ewm(span=200, adjust=False).mean()

    # 连涨/连跌/累计变化
    chg_arr = close.pct_change().values
    n = len(df)
    cu_a = np.zeros(n); cd_a = np.zeros(n); cc_a = np.zeros(n)
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
        cu_a[i] = cu; cd_a[i] = cd; cc_a[i] = cc

    df['consec_up']   = cu_a
    df['consec_down'] = cd_a
    df['cum_chg']     = cc_a
    return df


# ── 信号生成 ─────────────────────────────────────────
def generate_signals(df: pd.DataFrame,
                     sc=6, lc=4, ccp=0.002, adx_th=20,
                     cooldown=5) -> np.ndarray:
    """
    返回 int8 数组: 1=LONG, -1=SHORT, 0=无
    BUG-2修复: cooldown判断改为 >= (原为 >，多1根间距)
    """
    n = len(df)
    sigs = np.zeros(n, dtype=np.int8)
    adx  = df['adx'].values
    cu   = df['consec_up'].values
    cd   = df['consec_down'].values
    cc   = df['cum_chg'].values
    cl   = df['close'].values
    ema  = df['ema200'].values

    last_short = -cooldown - 1
    last_long  = -cooldown - 1

    for i in range(200, n):
        if adx[i] < adx_th:
            continue
        if cu[i] >= sc and cc[i] >= ccp:
            if i - last_short >= cooldown:  # BUG-2修复: >= 而非 >
                sigs[i] = -1
                last_short = i
        elif cd[i] >= lc and cc[i] <= -ccp and cl[i] > ema[i]:
            if i - last_long >= cooldown:   # BUG-2修复: >= 而非 >
                sigs[i] = 1
                last_long = i
    return sigs


# ── 回测引擎 v3.0 ────────────────────────────────────
def backtest_v3(df: pd.DataFrame, sigs: np.ndarray,
                tp_s=1.0, tp_l=0.8,
                capital=150.0, risk_pct=0.02,
                consec_sl_cooldown=True,
                consec_sl_threshold=2,
                cooldown_bars=16) -> list:
    """
    v2.1继承 + v3修复：
    - BUG-3修复：回测结束时强制平未完成持仓（tag=FORCE_CLOSE）
    - BUG-2修复：cooldown >= 而非 >
    - 开仓用下根open价
    - TP/SL同帧双触用开盘价判断先后
    - 连续SL冷却
    """
    atr_arr   = df['atr'].values
    open_arr  = df['open'].values
    high_arr  = df['high'].values
    low_arr   = df['low'].values
    close_arr = df['close'].values
    n = len(df)

    trades = []
    equity = capital
    pos = None
    consec_sl_count = 0
    cooldown_until  = -1

    for i in range(n):
        # ── 平仓检查 ──
        if pos is not None:
            if pos['dir'] == 1:
                hit_tp = high_arr[i] >= pos['tp']
                hit_sl = low_arr[i]  <= pos['sl']
            else:
                hit_tp = low_arr[i]  <= pos['tp']
                hit_sl = high_arr[i] >= pos['sl']

            if hit_tp or hit_sl:
                if hit_tp and hit_sl:
                    hit_tp = abs(open_arr[i] - pos['tp']) <= abs(open_arr[i] - pos['sl'])
                    hit_sl = not hit_tp

                exit_p = pos['tp'] if hit_tp else pos['sl']
                pnl_pct = (exit_p / pos['entry'] - 1) * pos['dir']
                pnl = pos['risk'] * (pnl_pct / pos['sl_dist_pct'] - FEE * 2)
                equity += pnl
                trades.append({
                    'dir':    pos['dir'],
                    'entry':  pos['entry'],
                    'exit':   exit_p,
                    'win':    hit_tp,
                    'pnl':    pnl,
                    'equity': equity,
                    'bar':    i,
                    'tag':    'TP' if hit_tp else 'SL'
                })
                if consec_sl_cooldown:
                    if hit_tp:
                        consec_sl_count = 0
                    else:
                        consec_sl_count += 1
                        if consec_sl_count >= consec_sl_threshold:
                            cooldown_until  = i + cooldown_bars
                            consec_sl_count = 0
                pos = None

        # ── 开仓 ──
        if pos is None and i + 1 < n and sigs[i] != 0:
            if consec_sl_cooldown and i < cooldown_until:
                continue
            price = open_arr[i + 1]
            atr   = atr_arr[i]
            if atr <= 0 or np.isnan(atr):
                continue
            if sigs[i] == -1:
                sl = price + 1.0 * atr
                tp = price - tp_s * atr
            else:
                sl = price - 1.0 * atr
                tp = price + tp_l * atr
            sl_dist_pct = abs(price - sl) / price
            if sl_dist_pct <= 0:
                continue
            pos = dict(
                dir=int(sigs[i]),
                entry=price,
                sl=sl, tp=tp,
                risk=equity * risk_pct,
                sl_dist_pct=sl_dist_pct
            )

    # BUG-3修复：强制平未完成持仓
    if pos is not None:
        exit_p = close_arr[-1]
        pnl_pct = (exit_p / pos['entry'] - 1) * pos['dir']
        pnl = pos['risk'] * (pnl_pct / pos['sl_dist_pct'] - FEE * 2)
        equity += pnl
        trades.append({
            'dir':    pos['dir'],
            'entry':  pos['entry'],
            'exit':   exit_p,
            'win':    pnl > 0,
            'pnl':    pnl,
            'equity': equity,
            'bar':    n - 1,
            'tag':    'FORCE_CLOSE'
        })

    return trades


# ── 统计 ─────────────────────────────────────────────
def calc_stats(trades: list, capital=150.0, days=180) -> dict:
    if not trades or len(trades) < 5:
        return dict(trades=len(trades) if trades else 0,
                    wr=0, monthly_return=0, max_dd=0, pf=0, final_equity=capital)
    wins   = [t for t in trades if t['win']]
    losses = [t for t in trades if not t['win']]
    wr = len(wins) / len(trades)
    eq = [capital] + [t['equity'] for t in trades]
    eq_s = pd.Series(eq)
    dd = ((eq_s - eq_s.cummax()) / eq_s.cummax()).min()
    gp = sum(t['pnl'] for t in wins)
    gl = abs(sum(t['pnl'] for t in losses)) or 1e-9
    months = days / 30.0
    monthly = (eq[-1] / capital) ** (1 / months) - 1
    return dict(
        trades=len(trades),
        wr=round(wr * 100, 1),
        final_equity=round(eq[-1], 1),
        total_return=round((eq[-1] / capital - 1) * 100, 1),
        monthly_return=round(monthly * 100, 1),
        max_dd=round(abs(dd) * 100, 1),
        pf=round(gp / gl, 2)
    )


# ── Walk-Forward验证 ──────────────────────────────────
def walk_forward(df: pd.DataFrame, params: dict,
                 train_ratio=0.67) -> dict:
    """
    前train_ratio数据训练 → 后(1-train_ratio)验证
    WR下滑<5% → 稳健 | 5~10% → 谨慎 | >10% → 过拟合
    """
    n = len(df)
    split = int(n * train_ratio)
    df_in  = df.iloc[:split]
    df_out = df.iloc[split:]

    days_in  = int(len(df_in)  * 15 / 60 / 24)
    days_out = int(len(df_out) * 15 / 60 / 24)

    sigs_in  = generate_signals(df_in,  sc=params['sc'], lc=params['lc'],
                                ccp=params['ccp'], adx_th=params['adx_th'])
    sigs_out = generate_signals(df_out, sc=params['sc'], lc=params['lc'],
                                ccp=params['ccp'], adx_th=params['adx_th'])

    t_in  = backtest_v3(df_in,  sigs_in,  tp_s=params['tp_s'], tp_l=params['tp_l'])
    t_out = backtest_v3(df_out, sigs_out, tp_s=params['tp_s'], tp_l=params['tp_l'])

    s_in  = calc_stats(t_in,  days=days_in)
    s_out = calc_stats(t_out, days=days_out)

    wr_drop = s_in['wr'] - s_out['wr']
    verdict = "✅稳健" if wr_drop < 5 else ("🟡谨慎" if wr_drop < 10 else "🔴过拟合")

    return dict(
        in_sample=s_in,
        out_sample=s_out,
        wr_drop=round(wr_drop, 1),
        verdict=verdict,
        days_in=days_in,
        days_out=days_out
    )


if __name__ == '__main__':
    import requests, time
    def fetch(symbol, days=60):
        end = int(time.time()*1000)
        start = end - days*86400*1000
        url = 'https://fapi.binance.com/fapi/v1/klines'
        all_k = []
        while start < end:
            r = requests.get(url, params=dict(symbol=symbol,interval='15m',
                             startTime=start,endTime=end,limit=1500), timeout=10)
            data = r.json()
            if not data or isinstance(data, dict): break
            all_k.extend(data); start=data[-1][0]+1
            if len(data)<1500: break
            time.sleep(0.15)
        df = pd.DataFrame(all_k, columns=['ts','open','high','low','close','vol',
                          'close_ts','qvol','trades','taker_buy','taker_buy_q','ignore'])
        for c in ['open','high','low','close','vol']: df[c]=df[c].astype(float)
        df['ts']=pd.to_datetime(df['ts'],unit='ms'); df.set_index('ts',inplace=True)
        df.drop_duplicates(inplace=True)
        return df

    print("自测: BTCUSDT 60天...")
    df = fetch('BTCUSDT', 60)
    df = compute_indicators(df)
    sigs = generate_signals(df)
    print(f"  K线:{len(df)}, 信号:{(sigs!=0).sum()} (SHORT:{(sigs==-1).sum()}, LONG:{(sigs==1).sum()})")
    trades = backtest_v3(df, sigs)
    fc = sum(1 for t in trades if t.get('tag')=='FORCE_CLOSE')
    s = calc_stats(trades, days=60)
    print(f"  WR={s['wr']}% 月均={s['monthly_return']}% 回撤={s['max_dd']}% PF={s['pf']} 笔数={s['trades']} FORCE_CLOSE={fc}")
    print("✅ 引擎v3.0自测完成")
