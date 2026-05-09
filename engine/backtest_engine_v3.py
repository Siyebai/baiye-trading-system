"""
白夜回测引擎 v3.0 — 手续费彻底修正版
======================================
核心修正（v2.x BUG #9，根本性错误）：
  旧：pnl = risk * (pnl_pct/sl_dist_pct - FEE*2)
       手续费 = risk * 2*FEE = 3U*0.18% = 0.0054U  [错！少算700倍]
  新：notional = risk / sl_dist_pct
       pnl = notional * pnl_pct - notional * FEE * 2
       手续费 = notional * 0.18%  [正确！按真实名义仓位]

核心约束（防止"结构性亏损"）：
  - 名义仓位上限 MAX_NOTIONAL_X * equity（默认3倍）
  - 最小止盈距 = FEE_COVER * 2 * FEE * price（默认3倍手续费覆盖）
    即：tp_atr * ATR / price >= FEE_COVER * 2 * FEE
        ATR / price >= 0.0054 / tp_atr

结论：150U资金在0.09%手续费下：
  - 3m/5m BTC/ETH/BNB 结构性不可行（ATR%太小）
  - 1h+ SOL/LINK/AVAX/ADA 有有限机会
  - 必须 TP ≥ 2.0×ATR 才能覆盖手续费
"""
import numpy as np
import pandas as pd
import requests
import time

# ── 全局常量 ────────────────────────────────────────────────
FEE            = 0.0009    # 单边手续费（BNB抵扣后）
MAX_NOTIONAL_X = 3.0       # 名义仓位上限倍数（equity的3倍）
MIN_FEE_COVER  = 2.5       # 止盈利润至少是手续费的2.5倍


# ── 指标计算（Wilder平滑，与实盘一致）────────────────────────
def _wilder(arr: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(arr), np.nan)
    idx = np.where(~np.isnan(arr))[0]
    if len(idx) < n:
        return out
    s = idx[0]
    out[s + n - 1] = np.nanmean(arr[s:s + n])
    for i in range(s + n, len(arr)):
        if not np.isnan(out[i - 1]):
            out[i] = (out[i - 1] * (n - 1) + arr[i]) / n
    return out


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    c, h, l = df['close'].values, df['high'].values, df['low'].values
    n = len(c)

    # ATR（Wilder 14）
    prev_c = np.roll(c, 1); prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    df['atr'] = _wilder(tr, 14)

    # EMA200
    df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()

    # ADX（Wilder 14）
    up = np.diff(h, prepend=h[0])
    dn = np.diff(l, prepend=l[0]) * -1
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr14 = _wilder(tr, 14)
    safe  = np.where(atr14 > 0, atr14, np.nan)
    pdi   = 100 * _wilder(pdm, 14) / safe
    ndi   = 100 * _wilder(ndm, 14) / safe
    denom = np.where((pdi + ndi) > 0, pdi + ndi, np.nan)
    dx    = 100 * np.abs(pdi - ndi) / denom
    df['adx'] = _wilder(dx, 14)

    # 连涨/连跌计数 + 累计变化
    cu = np.zeros(n, int)
    cd = np.zeros(n, int)
    cc = np.zeros(n)
    for i in range(1, n):
        if c[i] > c[i - 1]:
            cu[i] = cu[i - 1] + 1; cd[i] = 0
            cc[i] = cc[i - 1] + (c[i] - c[i - 1]) / c[i - 1] if cu[i] > 1 else (c[i] - c[i - 1]) / c[i - 1]
        elif c[i] < c[i - 1]:
            cd[i] = cd[i - 1] + 1; cu[i] = 0
            cc[i] = cc[i - 1] + (c[i] - c[i - 1]) / c[i - 1] if cd[i] > 1 else (c[i] - c[i - 1]) / c[i - 1]
        else:
            cu[i] = cu[i - 1]; cd[i] = cd[i - 1]; cc[i] = cc[i - 1]
    df['cu'] = cu; df['cd'] = cd; df['cc'] = cc
    return df


def generate_signals(df: pd.DataFrame,
                     sc: int = 5, lc: int = 4,
                     ccp: float = 0.002, adx_th: float = 20,
                     cooldown: int = 5,
                     long_disabled: bool = False) -> np.ndarray:
    """返回信号数组：-1=做空, 0=无, +1=做多"""
    n = len(df)
    sigs = np.zeros(n, dtype=int)
    last_sig_bar = -cooldown
    cu  = df['cu'].values; cd  = df['cd'].values
    cc  = df['cc'].values; adx = df['adx'].values
    c   = df['close'].values; ema = df['ema200'].values

    for i in range(n):
        if np.isnan(adx[i]) or adx[i] < adx_th:
            continue
        if i - last_sig_bar < cooldown:
            continue
        # 做空：连涨 + 累计涨幅
        if cu[i] >= sc and cc[i] >= ccp:
            sigs[i] = -1; last_sig_bar = i
        # 做多：连跌 + 累计跌幅 + 价格在EMA200上方
        elif (not long_disabled) and cd[i] >= lc and cc[i] <= -ccp and c[i] > ema[i]:
            sigs[i] = 1; last_sig_bar = i
    return sigs


def backtest_v3(df: pd.DataFrame,
                sigs: np.ndarray,
                tp_s: float = 2.0,
                tp_l: float = 1.8,
                sl_atr: float = 1.5,
                capital: float = 150.0,
                risk_pct: float = 0.02,
                max_notional_x: float = MAX_NOTIONAL_X,
                min_fee_cover: float = MIN_FEE_COVER,
                consec_sl_cooldown: bool = True,
                consec_sl_threshold: int = 2,
                cooldown_bars: int = 16) -> list:
    """
    v3.0回测：手续费按名义仓位正确计算
    
    止盈可行性检查：
      tp_atr * ATR / price >= min_fee_cover * 2 * FEE
    名义仓位上限：
      notional = min(risk/sl_dist_pct, capital*max_notional_x)
    """
    n = len(df)
    atr_arr   = df['atr'].values
    open_arr  = df['open'].values
    high_arr  = df['high'].values
    low_arr   = df['low'].values
    close_arr = df['close'].values

    trades = []
    equity = capital
    pos    = None
    consec_sl_count = 0
    cooldown_until  = -1

    for i in range(n):
        # ── 平仓检查 ──────────────────────────────────────────
        if pos is not None:
            if pos['dir'] == 1:   # LONG
                hit_tp = high_arr[i] >= pos['tp']
                hit_sl = low_arr[i]  <= pos['sl']
            else:                  # SHORT
                hit_tp = low_arr[i]  <= pos['tp']
                hit_sl = high_arr[i] >= pos['sl']

            if hit_tp or hit_sl:
                # 同帧双触：用开盘价判近
                if hit_tp and hit_sl:
                    hit_tp = abs(open_arr[i] - pos['tp']) <= abs(open_arr[i] - pos['sl'])
                    hit_sl = not hit_tp

                exit_p  = pos['tp'] if hit_tp else pos['sl']
                pnl_pct = (exit_p / pos['entry'] - 1) * pos['dir']

                # ✅ v3.0 正确手续费公式
                notional = pos['notional']
                pnl      = notional * pnl_pct - notional * FEE * 2
                equity  += pnl

                trades.append({
                    'dir':      pos['dir'],
                    'entry':    pos['entry'],
                    'exit':     exit_p,
                    'win':      hit_tp,
                    'pnl':      pnl,
                    'equity':   equity,
                    'bar':      i,
                    'notional': notional,
                    'fee':      notional * FEE * 2,
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

        # ── 开仓 ──────────────────────────────────────────────
        if pos is None and i + 1 < n and sigs[i] != 0:
            if consec_sl_cooldown and i < cooldown_until:
                continue

            price = open_arr[i + 1]
            atr   = atr_arr[i]
            if atr <= 0 or np.isnan(atr):
                continue

            if sigs[i] == -1:   # SHORT
                sl = price + sl_atr * atr
                tp = price - tp_s * atr
            else:                # LONG
                sl = price - sl_atr * atr
                tp = price + tp_l * atr

            sl_dist_pct = abs(price - sl) / price
            tp_dist_pct = abs(price - tp) / price
            if sl_dist_pct <= 0:
                continue

            # ✅ 止盈可行性检查：TP收益 >= min_fee_cover × 手续费
            # fee_ratio = tp_dist_pct / (2*FEE)
            if tp_dist_pct < min_fee_cover * 2 * FEE:
                continue   # ATR太小或TP倍数不够，跳过

            # ✅ 名义仓位计算（含上限保护）
            risk     = equity * risk_pct
            raw_notional = risk / sl_dist_pct
            notional = min(raw_notional, equity * max_notional_x)

            pos = dict(
                dir         = int(sigs[i]),
                entry       = price,
                sl          = sl,
                tp          = tp,
                notional    = notional,
                sl_dist_pct = sl_dist_pct,
            )

    return trades


def calc_stats(trades: list, capital: float = 150.0, days: int = 180) -> dict:
    if not trades or len(trades) < 3:
        return dict(trades=len(trades) if trades else 0,
                    wr=0, monthly_return=0, max_dd=0, pf=0,
                    final_equity=capital, total_return=0)

    wins   = [t for t in trades if t['win']]
    losses = [t for t in trades if not t['win']]
    wr     = len(wins) / len(trades) * 100
    eq     = [capital] + [t['equity'] for t in trades]

    # 最大回撤
    peak   = capital; max_dd = 0
    for e in eq:
        peak   = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak * 100)

    # 月均复合收益
    months = max(days / 30.44, 0.5)
    final  = eq[-1]
    monthly = (final / capital) ** (1 / months) - 1

    # 盈利因子
    gross_w = sum(t['pnl'] for t in wins)
    gross_l = abs(sum(t['pnl'] for t in losses)) if losses else 1e-9
    pf      = gross_w / gross_l if gross_l > 0 else 999

    return dict(
        trades        = len(trades),
        wr            = round(wr, 1),
        final_equity  = round(final, 4),
        total_return  = round((final / capital - 1) * 100, 2),
        monthly_return= round(monthly * 100, 2),
        max_dd        = round(max_dd, 2),
        pf            = round(pf, 3),
    )


# ── Binance K线拉取（带重试）────────────────────────────────
def fetch_klines(symbol: str, interval: str, limit: int = 1000,
                 use_futures: bool = False) -> pd.DataFrame:
    base = 'https://fapi.binance.com/fapi/v1' if use_futures else 'https://api.binance.com/api/v3'
    url  = f'{base}/klines'
    for attempt in range(3):
        try:
            r = requests.get(url, params=dict(symbol=symbol, interval=interval, limit=limit), timeout=15)
            r.raise_for_status()
            raw = r.json()
            df  = pd.DataFrame(raw, columns=['ts','open','high','low','close','volume',
                                             'ct','qv','t','tb','tq','ig'])
            for col in ['open','high','low','close','volume']:
                df[col] = df[col].astype(float)
            df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
            return df.set_index('ts').sort_index()[['open','high','low','close','volume']]
        except Exception as e:
            if attempt < 2:
                time.sleep(1)
            else:
                raise
