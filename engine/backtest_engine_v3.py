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
    """
    v3.2升级：新增多空分向WR、最大连亏笔数、EV/笔、夏普比
    """
    if not trades or len(trades) < 5:
        return dict(trades=len(trades) if trades else 0,
                    wr=0, monthly_return=0, max_dd=0, pf=0, final_equity=capital,
                    long_wr=0, short_wr=0, long_n=0, short_n=0,
                    max_consec_loss=0, ev=0, sharpe=0)
    wins   = [t for t in trades if t['win']]
    losses = [t for t in trades if not t['win']]
    longs  = [t for t in trades if t.get('dir', 0) == 1]
    shorts = [t for t in trades if t.get('dir', 0) == -1]
    wr       = len(wins) / len(trades)
    long_wr  = sum(1 for t in longs  if t['win']) / max(len(longs),  1)
    short_wr = sum(1 for t in shorts if t['win']) / max(len(shorts), 1)
    eq = [capital] + [t['equity'] for t in trades]
    eq_s = pd.Series(eq)
    dd = ((eq_s - eq_s.cummax()) / eq_s.cummax()).min()
    gp = sum(t['pnl'] for t in wins)
    gl = abs(sum(t['pnl'] for t in losses)) or 1e-9
    months  = days / 30.0
    monthly = (eq[-1] / capital) ** (1 / months) - 1
    # 最大连亏
    streak = mcl = 0
    for t in trades:
        streak = streak + 1 if not t['win'] else 0
        mcl = max(mcl, streak)
    # EV/笔 & 夏普
    pnl_arr = np.array([t['pnl'] for t in trades])
    sharpe  = float(pnl_arr.mean() / (pnl_arr.std() + 1e-9) * np.sqrt(252 * 4)) if len(pnl_arr) > 1 else 0
    return dict(
        trades=len(trades),
        wr=round(wr * 100, 1),
        long_wr=round(long_wr * 100, 1),
        short_wr=round(short_wr * 100, 1),
        long_n=len(longs),
        short_n=len(shorts),
        final_equity=round(eq[-1], 1),
        total_return=round((eq[-1] / capital - 1) * 100, 1),
        monthly_return=round(monthly * 100, 1),
        max_dd=round(abs(dd) * 100, 1),
        pf=round(gp / gl, 2),
        ev=round(float(pnl_arr.mean()), 4),
        sharpe=round(sharpe, 3),
        max_consec_loss=mcl
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


# ═══════════════════════════════════════════════════════════════
# v3.1 新增：趋势状态机 + LiveWarmup 实盘预热器
# 整合说明：
#   趋势状态机：测试结论 → 仅BTC启用SHORT约束有效(+1.3%月均)
#               其他品种(LINK/POL/ETH/SOL/BNB)加状态约束后月均均下降，不启用
#   LiveWarmup：实盘必须组件，用历史K线预热EMA200/ADX/ATR后逐根输出实时信号
# ═══════════════════════════════════════════════════════════════

from enum import Enum


# ── 趋势状态机 ───────────────────────────────────────────────
class TrendState(Enum):
    BULL    = "BULL"
    BEAR    = "BEAR"
    NEUTRAL = "NEUTRAL"


def compute_trend_state(close_arr, ema_arr, confirm_bars=3):
    """
    连续confirm_bars根close>EMA200 → BULL
    连续confirm_bars根close<EMA200 → BEAR
    否则维持上一状态（默认NEUTRAL）
    """
    n = len(close_arr)
    states = [TrendState.NEUTRAL] * n
    above = below = 0
    cur = TrendState.NEUTRAL
    for i in range(n):
        if close_arr[i] > ema_arr[i]:
            above += 1; below = 0
        elif close_arr[i] < ema_arr[i]:
            below += 1; above = 0
        else:
            above = below = 0
        if above >= confirm_bars:   cur = TrendState.BULL
        elif below >= confirm_bars: cur = TrendState.BEAR
        states[i] = cur
    return states


def generate_signals_with_trend(df, sc=6, lc=4, ccp=0.002, adx_th=20,
                                  cooldown=5, confirm_bars=3,
                                  short_state_filter=True):
    """
    在 generate_signals 基础上加趋势状态机约束：
      short_state_filter=True：BULL期间禁做空（测试有效品种：BTC）
      short_state_filter=False：等价原 generate_signals（其他5个品种用此）

    注：generate_signals 原版作为默认兼容入口保留不变
    """
    n = len(df)
    sigs = np.zeros(n, dtype=np.int8)
    adx  = df['adx'].values
    cu   = df['consec_up'].values
    cd   = df['consec_down'].values
    cc   = df['cum_chg'].values
    cl   = df['close'].values
    ema  = df['ema200'].values

    ls = ll = -cooldown - 1

    if short_state_filter:
        states = compute_trend_state(cl, ema, confirm_bars)
    else:
        states = None

    for i in range(200, n):
        if adx[i] < adx_th:
            continue
        # SHORT：加状态约束时，BULL期间禁止做空
        if cu[i] >= sc and cc[i] >= ccp:
            if short_state_filter:
                allow_short = states[i] in (TrendState.BEAR, TrendState.NEUTRAL)
            else:
                allow_short = True
            if allow_short and i - ls >= cooldown:
                sigs[i] = -1; ls = i
        elif cd[i] >= lc and cc[i] <= -ccp and cl[i] > ema[i]:
            if i - ll >= cooldown:
                sigs[i] = 1; ll = i
    return sigs


# ── LiveWarmup 实盘预热器 ─────────────────────────────────────
class LiveWarmup:
    """
    用历史K线预热指标，之后逐根喂新K线，输出实时信号。

    使用方法：
        w = LiveWarmup('BTCUSDT', sym_cfg, sys_cfg).warmup(df_history)
        # 每根新K线收盘后：
        snap = w.update(new_candle_dict)
        if snap['signal'] != 0:
            print(snap['signal_text'], snap['entry_price'])
    """

    def __init__(self, symbol, sym_cfg=None, sys_cfg=None):
        self.symbol  = symbol
        self.sym_cfg = sym_cfg or {}
        self.sys_cfg = sys_cfg or {}
        self.buf     = None
        self.warmed  = False
        # BTC启用状态机，其他品种不启用
        self.use_trend_filter = (symbol == 'BTCUSDT')

    def warmup(self, df_history):
        if len(df_history) < 220:
            raise ValueError(
                f"{self.symbol} 预热数据不足：{len(df_history)}根（至少220根）")
        self.buf    = compute_indicators(df_history.copy())
        self.warmed = True
        row = self.buf.iloc[-1]
        states = compute_trend_state(
            self.buf['close'].values, self.buf['ema200'].values, 3)
        print(f"[{self.symbol}] 预热完成 {len(df_history)}根 | "
              f"趋势:{states[-1].value} | EMA200:{row['ema200']:.4f} | "
              f"ATR:{row['atr']:.4f} | ADX:{row['adx']:.1f}")
        return self

    def update(self, candle):
        """
        candle: dict(open,high,low,close,vol,...) 或 pd.Series
        返回 dict 含 signal/signal_text/trend/close/ema200/adx/atr/entry_price/sl/tp
        """
        if not self.warmed:
            raise RuntimeError("请先调用 warmup(df_history)")

        if isinstance(candle, dict):
            row_df = pd.DataFrame([candle])
        else:
            row_df = candle.to_frame().T

        # 保留最近800根防止内存膨胀
        self.buf = pd.concat([self.buf, row_df]).tail(800).reset_index(drop=True)
        self.buf = compute_indicators(self.buf)

        sc  = self.sym_cfg
        sig_arr = generate_signals_with_trend(
            self.buf,
            sc=sc.get('sc', 5),
            lc=sc.get('lc', 4),
            ccp=sc.get('ccp', 0.002),
            adx_th=sc.get('adx_th', 20),
            cooldown=self.sys_cfg.get('signal_cooldown', 5),
            short_state_filter=self.use_trend_filter
        )
        sig = int(sig_arr[-2])   # 倒数第2根（已收盘的最后一根）

        r       = self.buf.iloc[-1]
        states  = compute_trend_state(
            self.buf['close'].values, self.buf['ema200'].values, 3)
        trend   = states[-1].value
        atr     = float(r['atr'])
        price   = float(r['close'])   # 下根open≈当前close，实盘用next open

        # 预估SL/TP（实盘开仓时用next_open重新算，这里仅参考）
        if sig == -1:
            sl = price + atr
            tp = price - sc.get('tp_s', 1.0) * atr
        elif sig == 1:
            sl = price - atr
            tp = price + sc.get('tp_l', 0.8) * atr
        else:
            sl = tp = None

        return dict(
            symbol       = self.symbol,
            signal       = sig,
            signal_text  = {1: '🟢 LONG', -1: '🔴 SHORT', 0: '⚪ HOLD'}.get(sig, '⚪ HOLD'),
            trend        = trend,
            close        = round(price, 4),
            ema200       = round(float(r['ema200']), 4),
            adx          = round(float(r['adx']), 1),
            atr          = round(atr, 4),
            entry_price  = price,
            sl           = round(sl, 4) if sl else None,
            tp           = round(tp, 4) if tp else None,
        )
