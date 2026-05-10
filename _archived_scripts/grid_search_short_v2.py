#!/usr/bin/env python3
"""
Short-term strategy grid search optimizer v2
5m MACD cross + 15m direction resonance + RSI filter, TP/SL via ATR7
Optimized: pre-compute indicators per MACD combo, numba JIT for inner loop
"""
import json
import time
import numpy as np
from itertools import product
from pathlib import Path
import warnings
warnings.filterwarnings('ignore')

import numba
from numba import njit, prange

START_TIME = time.time()
MAX_SECONDS = 560  # 9.3 min safety margin

DATA_DIR = Path("data/realtime")
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "LINKUSDT", "POLUSDT"]

# Grid parameters
MACD_FAST_LIST  = [3, 5, 7]
MACD_SLOW_LIST  = [9, 13, 17]
TP_MULT_LIST    = [0.8, 1.0, 1.2]
SL_MULT_LIST    = [0.8, 1.0, 1.2]
RSI_RANGES      = [(38, 65), (40, 68), (45, 72)]

# Backtest config
CAPITAL    = 150.0
RISK_PER   = 3.0
FEE_RATE   = 0.0009
ATR_PERIOD = 7
RSI_PERIOD = 14
COOLDOWN   = 5


def load_ohlcv(path):
    with open(path) as f:
        data = json.load(f)
    arr = np.array([[float(r[0]), float(r[1]), float(r[2]),
                     float(r[3]), float(r[4])] for r in data], dtype=np.float64)
    return arr  # columns: timestamp, open, high, low, close


@njit(cache=True)
def ema_numba(arr, period):
    alpha = 2.0 / (period + 1)
    out = np.empty(len(arr))
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
    return out


@njit(cache=True)
def compute_macd_numba(close, fast, slow, signal):
    ema_fast = ema_numba(close, fast)
    ema_slow = ema_numba(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema_numba(macd_line, signal)
    hist = macd_line - signal_line
    return hist


@njit(cache=True)
def compute_rsi_numba(close, period):
    n = len(close)
    delta = np.empty(n)
    delta[0] = 0.0
    for i in range(1, n):
        delta[i] = close[i] - close[i-1]
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = ema_numba(gain, period)
    avg_loss = ema_numba(loss, period)
    rsi = np.empty(n)
    for i in range(n):
        if avg_loss[i] == 0:
            rsi[i] = 100.0
        else:
            rs = avg_gain[i] / avg_loss[i]
            rsi[i] = 100.0 - (100.0 / (1.0 + rs))
    return rsi


@njit(cache=True)
def compute_atr_numba(high, low, close, period):
    n = len(close)
    tr = np.empty(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        hl  = high[i] - low[i]
        hpc = abs(high[i] - close[i-1])
        lpc = abs(low[i]  - close[i-1])
        tr[i] = max(hl, max(hpc, lpc))
    return ema_numba(tr, period)


@njit(cache=True)
def backtest_core(
    close_5m, high_5m, low_5m,
    macd_hist, rsi_5m, bull_15m, atr_5m,
    tp_mult, sl_mult,
    rsi_long_min, rsi_long_max,
    risk_per, fee_rate, cooldown
):
    n = len(close_5m)
    pnl_arr = np.empty(n // 2, dtype=np.float64)
    trade_count = 0
    next_allowed = 0

    for i in range(1, n - 1):
        if i < next_allowed:
            continue

        # MACD cross
        prev_hist = macd_hist[i-1]
        curr_hist = macd_hist[i]
        is_long_cross  = (prev_hist < 0) and (curr_hist >= 0)
        is_short_cross = (prev_hist > 0) and (curr_hist <= 0)

        if not (is_long_cross or is_short_cross):
            continue

        rsi_val = rsi_5m[i]
        bull = bull_15m[i]

        # RSI mirror for shorts: invert window
        rsi_short_min = 100.0 - rsi_long_max
        rsi_short_max = 100.0 - rsi_long_min

        if is_long_cross:
            if bull != 1:
                continue
            if rsi_val < rsi_long_min or rsi_val > rsi_long_max:
                continue
            direction = 1
        else:
            if bull != 0:
                continue
            if rsi_val < rsi_short_min or rsi_val > rsi_short_max:
                continue
            direction = -1

        entry_price = close_5m[i]
        atr_val = atr_5m[i]
        if atr_val <= 0 or entry_price <= 0:
            continue

        tp_dist = atr_val * tp_mult
        sl_dist = atr_val * sl_mult
        qty = risk_per / sl_dist

        if direction == 1:
            tp_price = entry_price + tp_dist
            sl_price = entry_price - sl_dist
        else:
            tp_price = entry_price - tp_dist
            sl_price = entry_price + sl_dist

        exit_price = close_5m[min(i + 199, n - 1)]
        exit_bar   = min(i + 199, n - 1)

        for j in range(i + 1, min(i + 200, n)):
            h, l = high_5m[j], low_5m[j]
            if direction == 1:
                if l <= sl_price:
                    exit_price = sl_price
                    exit_bar   = j
                    break
                if h >= tp_price:
                    exit_price = tp_price
                    exit_bar   = j
                    break
            else:
                if h >= sl_price:
                    exit_price = sl_price
                    exit_bar   = j
                    break
                if l <= tp_price:
                    exit_price = tp_price
                    exit_bar   = j
                    break

        if direction == 1:
            raw_pnl = (exit_price - entry_price) * qty
        else:
            raw_pnl = (entry_price - exit_price) * qty

        fee = (entry_price + exit_price) * qty * fee_rate
        net_pnl = raw_pnl - fee
        pnl_arr[trade_count] = net_pnl
        trade_count += 1
        next_allowed = exit_bar + cooldown

    return pnl_arr[:trade_count]


def compute_stats(pnl_arr):
    if len(pnl_arr) == 0:
        return 0, 0.0, 0.0, 0.0
    n_trades = len(pnl_arr)
    wins     = np.sum(pnl_arr > 0)
    wr       = wins / n_trades * 100.0
    total_pnl = pnl_arr.sum()
    monthly_pnl = total_pnl / 6.0  # 180 days = 6 months
    gross_win  = float(pnl_arr[pnl_arr > 0].sum()) if wins > 0 else 0.0
    gross_loss = float(abs(pnl_arr[pnl_arr < 0].sum())) if (n_trades - wins) > 0 else 1e-9
    pf = gross_win / gross_loss if gross_loss > 0 else 999.0
    return int(n_trades), float(wr), float(monthly_pnl), float(pf)


def align_15m_to_5m(ts_5m, arr_15m):
    """Return is-bullish array for 15m bars, aligned to 5m timestamps."""
    ts_15m   = arr_15m[:, 0]
    open_15m = arr_15m[:, 1]
    close_15m= arr_15m[:, 4]
    is_bull  = (close_15m >= open_15m).astype(np.int8)
    idx = np.searchsorted(ts_15m, ts_5m, side='right') - 1
    idx = np.clip(idx, 0, len(ts_15m) - 1)
    return is_bull[idx]


def warmup_numba():
    """Warm up numba JIT with small dummy arrays."""
    print("Warming up numba JIT...", flush=True)
    dummy_close = np.random.rand(100).astype(np.float64) + 100
    dummy_high  = dummy_close + 0.5
    dummy_low   = dummy_close - 0.5
    dummy_hist  = np.random.randn(100).astype(np.float64)
    dummy_rsi   = np.random.rand(100).astype(np.float64) * 100
    dummy_bull  = np.random.randint(0, 2, 100).astype(np.int8)
    dummy_atr   = np.random.rand(100).astype(np.float64) + 0.5
    backtest_core(dummy_close, dummy_high, dummy_low,
                  dummy_hist, dummy_rsi, dummy_bull, dummy_atr,
                  1.0, 1.0, 40.0, 68.0, 3.0, 0.0009, 5)
    compute_macd_numba(dummy_close, 5, 13, 9)
    compute_rsi_numba(dummy_close, 14)
    compute_atr_numba(dummy_high, dummy_low, dummy_close, 7)
    print(f"  Warmup done ({time.time()-START_TIME:.1f}s)", flush=True)


def process_symbol(symbol):
    print(f"\n{'='*50}", flush=True)
    print(f"Processing {symbol}...", flush=True)

    path_5m  = DATA_DIR / f"{symbol}_5m_180d.json"
    path_15m = DATA_DIR / f"{symbol}_15m_180d.json"

    if not path_5m.exists() or not path_15m.exists():
        print(f"  Data missing for {symbol}, skipping", flush=True)
        return symbol, []

    arr_5m  = load_ohlcv(path_5m)
    arr_15m = load_ohlcv(path_15m)

    close_5m = arr_5m[:, 4]
    high_5m  = arr_5m[:, 2]
    low_5m   = arr_5m[:, 3]
    ts_5m    = arr_5m[:, 0]

    print(f"  5m bars: {len(close_5m)}, 15m bars: {len(arr_15m)}", flush=True)

    # Pre-compute fixed indicators
    atr_5m  = compute_atr_numba(high_5m, low_5m, close_5m, ATR_PERIOD)
    rsi_5m  = compute_rsi_numba(close_5m, RSI_PERIOD)
    bull_15m = align_15m_to_5m(ts_5m, arr_15m)

    # Pre-compute MACD histograms
    macd_cache = {}
    for fast, slow in product(MACD_FAST_LIST, MACD_SLOW_LIST):
        if fast >= slow:
            continue
        hist = compute_macd_numba(close_5m, fast, slow, 9)
        macd_cache[(fast, slow)] = hist

    print(f"  MACD combos: {len(macd_cache)}", flush=True)

    # Grid search
    results = []
    total_combos = len(macd_cache) * len(TP_MULT_LIST) * len(SL_MULT_LIST) * len(RSI_RANGES)
    print(f"  Total param combos: {total_combos}", flush=True)

    tested = 0
    timed_out = False
    for (fast, slow), macd_hist in macd_cache.items():
        for tp_mult in TP_MULT_LIST:
            for sl_mult in SL_MULT_LIST:
                for rsi_min, rsi_max in RSI_RANGES:

                    if time.time() - START_TIME > MAX_SECONDS:
                        timed_out = True
                        break

                    pnl_arr = backtest_core(
                        close_5m, high_5m, low_5m,
                        macd_hist, rsi_5m, bull_15m, atr_5m,
                        tp_mult, sl_mult,
                        float(rsi_min), float(rsi_max),
                        RISK_PER, FEE_RATE, COOLDOWN
                    )
                    tested += 1
                    n_trades, wr, monthly_pnl, pf = compute_stats(pnl_arr)

                    if n_trades >= 80 and wr >= 53.0 and monthly_pnl > 0:
                        results.append({
                            "macd": [fast, slow],
                            "rsi": [rsi_min, rsi_max],
                            "tp": tp_mult,
                            "sl": sl_mult,
                            "wr": round(wr, 2),
                            "monthly": round(monthly_pnl, 2),
                            "pf": round(pf, 3),
                            "trades": n_trades
                        })

                if timed_out:
                    break
            if timed_out:
                break
        if timed_out:
            break

    if timed_out:
        print(f"  ⚠️  Time limit hit at {tested}/{total_combos} combos", flush=True)
    else:
        print(f"  Tested all {tested} combos", flush=True)

    results.sort(key=lambda x: x["monthly"], reverse=True)
    top3 = results[:3]
    print(f"  Passed filter: {len(results)} | Top3 kept", flush=True)
    return symbol, top3


def main():
    print("=" * 60)
    print("Grid Search Short Strategy v2 (numba-accelerated)")
    print(f"Symbols: {SYMBOLS}")
    print("=" * 60, flush=True)

    warmup_numba()

    final_results = {}

    for symbol in SYMBOLS:
        if time.time() - START_TIME > MAX_SECONDS:
            print(f"⚠️  Time budget exhausted, skipping remaining symbols")
            final_results[symbol] = []
            continue
        sym, top3 = process_symbol(symbol)
        final_results[sym] = top3

    # Save results
    out_path = Path("research/grid_search_short_v2.json")
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(final_results, f, indent=2)

    print("\n" + "=" * 60)
    print("RESULTS SUMMARY — Top Parameters Per Symbol")
    print("=" * 60)

    for symbol, top3 in final_results.items():
        print(f"\n{symbol}:")
        if not top3:
            print("  (no combos passed filter: WR≥53%, monthly>0, trades≥80)")
        else:
            print(f"  {'#':<4} {'MACD':<10} {'RSI Range':<14} {'TP':<5} {'SL':<5} "
                  f"{'WR%':<8} {'Monthly$':<10} {'PF':<7} {'Trades'}")
            print(f"  {'-'*70}")
            for i, r in enumerate(top3, 1):
                print(f"  #{i:<3} {str(r['macd']):<10} {str(r['rsi']):<14} "
                      f"{r['tp']:<5} {r['sl']:<5} {r['wr']:<8} {r['monthly']:<10.2f} "
                      f"{r['pf']:<7.3f} {r['trades']}")

    elapsed = time.time() - START_TIME
    print(f"\n✅ Done in {elapsed:.1f}s")
    print(f"Results saved to: {out_path}")


if __name__ == "__main__":
    main()
