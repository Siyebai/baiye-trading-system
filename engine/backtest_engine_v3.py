#!/usr/bin/env python3
"""
白夜系统 回测引擎 v3.0
基于 v2.1 升级，修复说明：
  v3.0 BUG-3修复: 回测结束时未平仓持仓静默丢弃 → 强制平仓(FORCE_CLOSE)
  v3.0 新增:  Walk-Forward验证函数（前240天训练/后120天样本外）
  v3.0 新增:  ADX动态TP（仅BNB启用，其他品种禁用）
  v3.0 新增:  BTC参数更新（WF验证稳健：sc=4,lc=5,ccp=0.0025,adx=22）

外部AI建议中已测试但不采纳的改动（数据不支持）：
  ✗ SHORT加EMA200过滤：SHORT信号减少68%，月均损失64%，不采用
  ✗ TP全品种大幅提升(0.8→1.5)：WR大幅下降，月均净损失，不采用
  ✗ Wilder RMA ADX(alpha=1/14)：与EMA14差异≈9%，实际对信号影响可忽略
  ✗ 成交量/ATR强度过滤：全品种测试净负效果（信号减少但收益下降更多）
  ✗ ADX动态TP(多品种)：仅BNB正效果(+4.2%月均)，LINK/ETH/SOL/POL全负

延续 v2.1 已验证有效的所有功能：
  ✓ pandas None→StringArray Bug修复（np.int8信号数组）
  ✓ cum_chg方向切换重置
  ✓ 开仓用下根K线open价
  ✓ TP/SL同帧双触用开盘价判断先后
  ✓ ATR/ADX零值保护
  ✓ 同向信号5根cooldown
  ✓ 连续SL冷却机制（2次SL→冷却16根）
  ✓ 月均收益按实际天数计算
"""
import numpy as np
import pandas as pd

FEE = 0.0009  # 0.09% 单边


# ── 指标计算（向量化）─────────────────────────────────
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    high, low, close = df['high'], df['low'], df['close']

    prev_close = close.shift(1).fillna(close.iloc[0])
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs()
    ], axis=1).max(axis=1)
    df['atr'] = tr.ewm(span=14, adjust=False).mean()
    df['atr'] = df['atr'].replace(0, np.nan).ffill().fillna(1.0)

    up = high.diff(); down = -low.diff()
    pdm = up.where((up > down) & (up > 0), 0.0)
    ndm = down.where((down > up) & (down > 0), 0.0)
    atr_e = df['atr']
    pdi = 100 * pdm.ewm(span=14, adjust=False).mean() / atr_e
    ndi = 100 * ndm.ewm(span=14, adjust=False).mean() / atr_e
    denom = (pdi + ndi).replace(0, np.nan)
    dx = 100 * (pdi - ndi).abs() / denom
    df['adx'] = dx.ewm(span=14, adjust=False).mean().fillna(0)

    df['ema200'] = close.ewm(span=200, adjust=False).mean()

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


# ── 信号生成 ──────────────────────────────────────────
def generate_signals(df: pd.DataFrame,
                     sc=6, lc=4, ccp=0.002, adx_th=20,
                     cooldown=5) -> np.ndarray:
    """返回 int8 数组: 1=LONG, -1=SHORT, 0=无信号"""
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


# ── 回测引擎 v3.0 ─────────────────────────────────────
def backtest_v3(df: pd.DataFrame, sigs: np.ndarray,
                tp_s=1.0, tp_l=0.8,
                capital=150.0, risk_pct=0.02,
                consec_sl_cooldown=True,
                consec_sl_threshold=2,
                cooldown_bars=16,
                adx_dynamic_tp=False) -> list:
    """
    v3.0新增：
    - BUG-3修复：回测结束时若有未平仓持仓，强制平仓（tag='FORCE_CLOSE'）
    - adx_dynamic_tp：ADX分级TP（仅BNB推荐启用）
        ADX≥40→TP×1.6，ADX≥30→TP×1.3，否则×1.0
    """
    atr_arr   = df['atr'].values
    open_arr  = df['open'].values
    high_arr  = df['high'].values
    low_arr   = df['low'].values
    close_arr = df['close'].values
    adx_arr   = df['adx'].values
    n = len(df)

    trades = []
    equity = capital
    pos = None
    consec_sl_count = 0
    cooldown_until  = -1

    for i in range(n):
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

        if pos is None and i + 1 < n and sigs[i] != 0:
            if consec_sl_cooldown and i < cooldown_until:
                continue
            price = open_arr[i + 1]
            atr   = atr_arr[i]
            if atr <= 0 or np.isnan(atr):
                continue

            # ADX动态TP（仅BNB）
            if adx_dynamic_tp:
                adx_v = adx_arr[i]
                if adx_v >= 40:   mult = 1.6
                elif adx_v >= 30: mult = 1.3
                else:             mult = 1.0
                tps = tp_s * mult; tpl = tp_l * mult
            else:
                tps = tp_s; tpl = tp_l

            if sigs[i] == -1:
                sl = price + 1.0 * atr
                tp = price - tps * atr
            else:
                sl = price - 1.0 * atr
                tp = price + tpl * atr

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

    # BUG-3修复：强制平仓未结束的持仓
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


# v2.1 兼容别名（旧代码无缝使用）
def backtest_v2(df, sigs, tp_s=1.0, tp_l=0.8, capital=150.0, risk_pct=0.02,
                consec_sl_cooldown=True, consec_sl_threshold=2, cooldown_bars=16):
    return backtest_v3(df, sigs, tp_s=tp_s, tp_l=tp_l, capital=capital,
                       risk_pct=risk_pct, consec_sl_cooldown=consec_sl_cooldown,
                       consec_sl_threshold=consec_sl_threshold,
                       cooldown_bars=cooldown_bars, adx_dynamic_tp=False)


# ── Walk-Forward 验证 ─────────────────────────────────
def walk_forward_validate(df: pd.DataFrame, symbol: str,
                          sc, lc, ccp, adx_th, tp_s, tp_l,
                          capital=150.0, train_ratio=0.67,
                          adx_dynamic_tp=False) -> dict:
    """
    前 train_ratio 比例为训练集，剩余为样本外验证集
    WR下滑 < 5%  → ✅稳健
    WR下滑 5~10% → 🟡谨慎
    WR下滑 > 10% → 🔴过拟合，不建议上实盘
    """
    n = len(df)
    split = int(n * train_ratio)
    df_in  = df.iloc[:split].reset_index(drop=True)
    df_out = df.iloc[split:].reset_index(drop=True)
    d_in  = len(df_in)  * 15 // 60 // 24
    d_out = len(df_out) * 15 // 60 // 24

    sigs_in  = generate_signals(df_in,  sc=sc, lc=lc, ccp=ccp, adx_th=adx_th)
    t_in     = backtest_v3(df_in, sigs_in, tp_s=tp_s, tp_l=tp_l,
                           capital=capital, adx_dynamic_tp=adx_dynamic_tp)
    s_in     = calc_stats(t_in, capital=capital, days=d_in)

    sigs_out = generate_signals(df_out, sc=sc, lc=lc, ccp=ccp, adx_th=adx_th)
    t_out    = backtest_v3(df_out, sigs_out, tp_s=tp_s, tp_l=tp_l,
                           capital=capital, adx_dynamic_tp=adx_dynamic_tp)
    s_out    = calc_stats(t_out, capital=capital, days=d_out)

    drop = s_in['wr'] - s_out['wr']
    if drop < 5:   verdict = "✅稳健"
    elif drop < 10: verdict = "🟡谨慎"
    else:           verdict = "🔴过拟合"

    return dict(symbol=symbol,
                in_sample=s_in, out_sample=s_out,
                wr_drop=round(drop, 1), verdict=verdict,
                in_days=d_in, out_days=d_out)


# ── 统计 ──────────────────────────────────────────────
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


if __name__ == '__main__':
    import requests, time
    def fetch(symbol, days=60):
        end = int(time.time()*1000); start = end - days*86400*1000
        url = 'https://fapi.binance.com/fapi/v1/klines'; all_k = []
        while start < end:
            r = requests.get(url, params=dict(symbol=symbol, interval='15m',
                             startTime=start, endTime=end, limit=1500), timeout=10)
            data = r.json()
            if not data or isinstance(data, dict): break
            all_k.extend(data); start = data[-1][0] + 1
            if len(data) < 1500: break
            time.sleep(0.15)
        df = pd.DataFrame(all_k, columns=['ts','open','high','low','close','vol',
                          'close_ts','qvol','trades','taker_buy','taker_buy_q','ignore'])
        for c in ['open','high','low','close','vol']: df[c] = df[c].astype(float)
        df['ts'] = pd.to_datetime(df['ts'], unit='ms'); df.set_index('ts', inplace=True)
        df.drop_duplicates(inplace=True)
        return df

    print("自测 v3.0: BTCUSDT 60天...")
    df = fetch('BTCUSDT', 60)
    df = compute_indicators(df)
    sigs = generate_signals(df)
    trades = backtest_v3(df, sigs)
    s = calc_stats(trades, days=60)
    fc = [t for t in trades if t.get('tag') == 'FORCE_CLOSE']
    print(f"  K线:{len(df)} 信号:{(sigs!=0).sum()} SHORT:{(sigs==-1).sum()} LONG:{(sigs==1).sum()}")
    print(f"  WR={s['wr']}% 月均={s['monthly_return']}% DD={s['max_dd']}% PF={s['pf']} 笔={s['trades']}")
    print(f"  FORCE_CLOSE笔数: {len(fc)}")
    print("✅ 引擎 v3.0 自测完成")
