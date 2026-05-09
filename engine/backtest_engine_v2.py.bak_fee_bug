#!/usr/bin/env python3
"""
白夜系统 回测引擎 v2.2
修复记录：
  v1.x Bug1: pandas None→StringArray nan bool=True，导致每根K线开仓 [已修复]
  v1.x Bug2: cum_chg方向切换未重置，累计值跨方向叠加 [已修复]
  v2.0 改进1: 开仓改用下根K线open价（更接近实盘）
  v2.0 改进2: TP/SL同帧双触时用开盘价判断优先方向（消除0.9%乐观偏差）
  v2.0 改进3: ATR=0/nan 安全保护
  v2.0 改进4: 月均收益按实际交易天数计算
  v2.0 改进5: 最小信号间距过滤（同向信号5根内不重复开仓）
  v2.1 新增:  连续SL冷却机制（同品种连续2次SL→冷却16根K线≈4小时）
              解决单边趋势行情中反转信号连续止损问题
  v2.2 Bug修复: ATR/ADX从EWM(alpha=2/15)改为Wilder's(alpha=1/14)
              与signal_engine.py/live_scanner.py保持一致，消除~21%的ATR差异
"""
import numpy as np
import pandas as pd

FEE = 0.0009  # 0.09% 单边

# ── 指标计算（向量化，速度快）────────────────────────
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    high, low, close = df['high'], df['low'], df['close']

    # ATR14（Wilder's平滑，与实盘信号引擎保持一致）
    # Fix v2.2: 从EWM(span=14, alpha=2/15)改为Wilder's(alpha=1/14)
    # 原因: signal_engine.py和live_scanner.py均使用Wilder's; EWM alpha=0.133 vs Wilder=0.071
    #       导致ATR值差异约20%，SL/TP距离不一致
    prev_close = close.shift(1).fillna(close.iloc[0])
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs()
    ], axis=1).max(axis=1)
    n_atr = 14
    alpha_w = 1.0 / n_atr  # Wilder's alpha
    atr_vals = np.zeros(len(tr))
    tr_arr = tr.values
    atr_vals[:n_atr] = tr_arr[:n_atr].mean()
    for _i in range(n_atr, len(tr_arr)):
        atr_vals[_i] = atr_vals[_i-1] * (1 - alpha_w) + tr_arr[_i] * alpha_w
    df['atr'] = atr_vals
    # ATR安全保护
    df['atr'] = df['atr'].replace(0, np.nan).ffill().fillna(1.0)

    # ADX14（Wilder's平滑，与实盘信号引擎保持一致）
    # Fix v2.2: 从EWM改为Wilder's，保持回测与实盘ADX计算一致
    up = high.diff().fillna(0).values
    down = (-low.diff()).fillna(0).values
    pdm_arr = np.where((up > down) & (up > 0), up, 0.0)
    ndm_arr = np.where((down > up) & (down > 0), down, 0.0)
    atr_e_arr = df['atr'].values
    pdm_arr[0] = ndm_arr[0] = 0.0
    pdi14 = np.zeros(len(atr_e_arr))
    ndi14 = np.zeros(len(atr_e_arr))
    pdi14[:n_atr] = pdm_arr[:n_atr].mean()
    ndi14[:n_atr] = ndm_arr[:n_atr].mean()
    for _i in range(n_atr, len(atr_e_arr)):
        pdi14[_i] = pdi14[_i-1] * (1 - alpha_w) + pdm_arr[_i] * alpha_w
        ndi14[_i] = ndi14[_i-1] * (1 - alpha_w) + ndm_arr[_i] * alpha_w
    with np.errstate(divide='ignore', invalid='ignore'):
        pdi = np.where(atr_e_arr > 0, 100 * pdi14 / atr_e_arr, 0.0)
        ndi = np.where(atr_e_arr > 0, 100 * ndi14 / atr_e_arr, 0.0)
        dx = np.where((pdi + ndi) > 0, 100 * np.abs(pdi - ndi) / (pdi + ndi), 0.0)
    adx_vals = np.zeros(len(dx))
    adx_vals[:n_atr] = dx[:n_atr].mean()
    for _i in range(n_atr, len(dx)):
        adx_vals[_i] = adx_vals[_i-1] * (1 - alpha_w) + dx[_i] * alpha_w
    df['adx'] = adx_vals

    # EMA200
    df['ema200'] = close.ewm(span=200, adjust=False).mean()

    # 连涨/连跌/累计变化（numpy循环，正确重置）
    chg_arr = close.pct_change().values
    n = len(df)
    cu_a = np.zeros(n); cd_a = np.zeros(n); cc_a = np.zeros(n)
    cu = cd = 0; cc = 0.0
    for i in range(1, n):
        c = chg_arr[i]
        if np.isnan(c):
            continue
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


# ── 信号生成（np.int8数组，避免pandas None/nan bug）──
def generate_signals(df: pd.DataFrame,
                     sc=6, lc=4, ccp=0.002, adx_th=20,
                     cooldown=5) -> np.ndarray:
    """
    返回 int8 数组: 1=LONG, -1=SHORT, 0=无
    cooldown: 同方向信号最小间距（根），避免同一波行情重复开仓
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
            if i - last_short > cooldown:
                sigs[i] = -1
                last_short = i
        elif cd[i] >= lc and cc[i] <= -ccp and cl[i] > ema[i]:
            if i - last_long > cooldown:
                sigs[i] = 1
                last_long = i
    return sigs


# ── 回测引擎 v2.0 ────────────────────────────────────
def backtest_v2(df: pd.DataFrame, sigs: np.ndarray,
                tp_s=1.0, tp_l=0.8,
                capital=150.0, risk_pct=0.02,
                consec_sl_cooldown=True,
                consec_sl_threshold=2,
                cooldown_bars=16) -> list:
    """
    改进：
    - 开仓用下根K线open价（+1根）
    - TP/SL同帧双触时：用开盘价离哪个更近判断先触顺序
    - [v2.1新增] consec_sl_cooldown: 连续SL冷却
        同一品种连续 consec_sl_threshold 次SL → 冷却 cooldown_bars 根K线（默认16根≈4h）
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
    consec_sl_count = 0      # 连续SL计数
    cooldown_until  = -1     # 冷却到第几根bar

    for i in range(n):
        # ── 平仓检查 ──
        if pos is not None:
            if pos['dir'] == 1:   # LONG
                hit_tp = high_arr[i] >= pos['tp']
                hit_sl = low_arr[i]  <= pos['sl']
            else:                  # SHORT
                hit_tp = low_arr[i]  <= pos['tp']
                hit_sl = high_arr[i] >= pos['sl']

            if hit_tp or hit_sl:
                if hit_tp and hit_sl:
                    # 同帧双触：用开盘价判断先后
                    if pos['dir'] == 1:
                        # LONG: open离SL更近则SL先触
                        hit_tp = abs(open_arr[i] - pos['tp']) <= abs(open_arr[i] - pos['sl'])
                    else:
                        # SHORT: open离TP更近则TP先触
                        hit_tp = abs(open_arr[i] - pos['tp']) <= abs(open_arr[i] - pos['sl'])
                    hit_sl = not hit_tp

                exit_p = pos['tp'] if hit_tp else pos['sl']
                pnl_pct = (exit_p / pos['entry'] - 1) * pos['dir']
                pnl = pos['risk'] * (pnl_pct / pos['sl_dist_pct'] - FEE * 2)
                equity += pnl
                trades.append({
                    'dir':  pos['dir'],
                    'entry': pos['entry'],
                    'exit':  exit_p,
                    'win':   hit_tp,
                    'pnl':   pnl,
                    'equity': equity,
                    'bar':   i
                })
                # v2.1: 连续SL冷却
                if consec_sl_cooldown:
                    if hit_tp:
                        consec_sl_count = 0           # TP命中重置计数
                    else:
                        consec_sl_count += 1
                        if consec_sl_count >= consec_sl_threshold:
                            cooldown_until  = i + cooldown_bars
                            consec_sl_count = 0       # 进入冷却，重置计数
                pos = None

        # ── 开仓（用下根open，所以信号在i，开仓在i+1）──
        if pos is None and i + 1 < n and sigs[i] != 0:
            # v2.1: 冷却期内跳过开仓
            if consec_sl_cooldown and i < cooldown_until:
                continue
            price = open_arr[i + 1]   # 下根开盘价开仓
            atr   = atr_arr[i]         # 用信号根ATR定SL/TP
            if atr <= 0 or np.isnan(atr):
                continue
            if sigs[i] == -1:          # SHORT
                sl = price + 1.0 * atr
                tp = price - tp_s * atr
            else:                       # LONG
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
    # 月均：按实际天数
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


if __name__ == '__main__':
    # 快速自测
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
    sc = int((sigs!= 0).sum())
    print(f"  K线: {len(df)}, 信号: {sc} (SHORT:{(sigs==-1).sum()}, LONG:{(sigs==1).sum()})")
    trades = backtest_v2(df, sigs)
    s = calc_stats(trades, days=60)
    print(f"  结果: WR={s['wr']}% 月均={s['monthly_return']}% 回撤={s['max_dd']}% PF={s['pf']} 笔数={s['trades']}")
    print("✅ 引擎v2.2自测完成")
