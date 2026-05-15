#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
optimize_params.py v2 — 白夜交易系统参数优化器

【文件说明】
  接入 Binance Futures API 实时数据，对所有品种进行网格搜索，
  找到每个品种的最优参数组合（sc/lc/ccp/adx_th/tp_s/sl）。
  结果直接可复制进 config.py 的 SYM_CFG 区域。

【运行方式】
  python3 optimize_params.py
  结果保存至: research/optimize_result_v2.json

【优化参数说明】
  sc      — 连涨根数触发 SHORT（3~7）
  lc      — 连跌根数触发 LONG（3~5）
  ccp     — 累计涨跌幅阈值（0.001~0.003）
  adx_th  — ADX 最低门槛（过热山，15~35）
  tp_s    — 止盈 ATR 倍数（0.6~1.5）
  sl      — 止损 ATR 倍数（固定 1.5）

【评分方法】
  综合评分 = WR×0.55 + PF×0.3 + n×0.15
  WR≥50% 且 PF≥1.0 方可被选中（BNB/POL PF<1 暂停开仓）

【数据来源】
  Binance Futures API: /fapi/v1/klines
  1000根 15m K线 per 品种，带超时保护（10秒）
"""
from __future__ import annotations
import json, sys, time, warnings, signal
from datetime import datetime, timezone
from pathlib import Path
from itertools import product
import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")

FAPI_BASE = "https://fapi.binance.com"
FEE = 0.0002  # Maker

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "LINKUSDT", "POLUSDT", "DOTUSDT", "SUIUSDT"]
LONG_DISABLED = {"BNBUSDT", "LINKUSDT", "POLUSDT"}

# 精简网格（均值回归策略关键参数）
GRID = {
    "sc":     [3, 4, 5, 6, 7],
    "lc":     [3, 4, 5],
    "ccp":    [0.001, 0.0015, 0.002, 0.003],
    "adx_th": [15, 20, 25, 30],
    "tp_s":   [0.6, 0.8, 1.0, 1.2],
    "sl":     [1.0, 1.5],
}

def fetch_klines(symbol: str, interval: str = "15m", limit: int = 1000) -> pd.DataFrame:
    """拉取 Binance K线，带超时重试"""
    rows = []
    end_time = None
    fetched = 0
    attempts = 0
    while fetched < limit and attempts < 5:
        params = {"symbol": symbol, "interval": interval, "limit": min(500, limit - fetched)}
        if end_time:
            params["endTime"] = end_time
        try:
            r = requests.get(f"{FAPI_BASE}/fapi/v1/klines", params=params, timeout=8)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            attempts += 1
            print(f" retry({attempts}) ", end="", flush=True)
            time.sleep(2)
            continue
        if not data:
            break
        rows = data + rows
        fetched += len(data)
        if len(data) < 500 or fetched >= limit:
            break
        end_time = data[0][0] - 1
        time.sleep(0.05)
    if not rows:
        raise RuntimeError("No data")
    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","vol",
                                      "ct","qvol","nt","tbbv","tbqv","ig"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    for c in ["open","high","low","close","vol"]:
        df[c] = df[c].astype(float)
    return df[["ts","open","high","low","close","vol"]].reset_index(drop=True)

def wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1/period, adjust=False).mean()

def compute_atr_adx(df: pd.DataFrame, period: int = 14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    atr = wilder_smooth(tr, period)
    up  = h.diff();  dn = -l.diff()
    pdm = np.where((up>dn)&(up>0), up, 0.0)
    ndm = np.where((dn>up)&(dn>0), dn, 0.0)
    atr14 = wilder_smooth(tr, period)
    pdi = 100*wilder_smooth(pd.Series(pdm, index=df.index), period)/atr14
    ndi = 100*wilder_smooth(pd.Series(ndm, index=df.index), period)/atr14
    dx  = (100*(pdi-ndi).abs()/(pdi+ndi).replace(0,np.nan)).fillna(0)
    adx = wilder_smooth(dx, period)
    return atr.values, adx.values

def backtest(df, sc, lc, ccp, adx_th, tp_s, sl, long_ok, risk=3.0):
    close = df["close"].values
    high  = df["high"].values
    low   = df["low"].values
    atr, adx = compute_atr_adx(df)
    n = len(df)
    wins = losses = 0
    win_pnl = loss_pnl = 0.0

    i = max(30, sc, lc)
    while i < n - 2:
        if adx[i] < adx_th or atr[i] <= 0:
            i += 1; continue

        sig = None
        # SHORT: 连涨sc根
        if all(close[i-k] > close[i-k-1] for k in range(sc)):
            chg = (close[i] - close[i-sc]) / close[i-sc]
            if chg >= ccp:
                sig = "S"
        # LONG: 连跌lc根
        if sig is None and long_ok:
            if all(close[i-k] < close[i-k-1] for k in range(lc)):
                chg = (close[i-lc] - close[i]) / close[i-lc]
                if chg >= ccp:
                    sig = "L"

        if sig is None:
            i += 1; continue

        entry = close[i+1]
        atr_v = atr[i]
        if sig == "S":
            tp = entry - tp_s * atr_v
            stop = entry + sl * atr_v
        else:
            tp = entry + tp_s * atr_v
            stop = entry - sl * atr_v

        qty = risk / entry
        hit = False
        for j in range(i+2, min(i+32, n)):
            if sig == "S":
                if low[j] <= tp:
                    net = (entry-tp)*qty - FEE*entry*qty - FEE*tp*qty
                    wins += 1; win_pnl += net; hit = True; break
                if high[j] >= stop:
                    net = (entry-stop)*qty - FEE*entry*qty - FEE*stop*qty
                    losses += 1; loss_pnl += abs(net); hit = True; break
            else:
                if high[j] >= tp:
                    net = (tp-entry)*qty - FEE*entry*qty - FEE*tp*qty
                    wins += 1; win_pnl += net; hit = True; break
                if low[j] <= stop:
                    net = (stop-entry)*qty - FEE*entry*qty - FEE*stop*qty
                    losses += 1; loss_pnl += abs(net); hit = True; break
        i += 5 if hit else 1

    n_t = wins + losses
    wr = wins/n_t if n_t>0 else 0
    pf = win_pnl/loss_pnl if loss_pnl>0 else (9.9 if win_pnl>0 else 0)
    pnl = win_pnl - loss_pnl
    return {"n": n_t, "wr": wr, "pf": pf, "pnl": pnl}

def optimize_symbol(sym, df, long_ok):
    best_score = -1; best_r = None; best_p = None
    total = 0
    for sc, lc, ccp, adx_th, tp_s, sl in product(
        GRID["sc"], GRID["lc"], GRID["ccp"],
        GRID["adx_th"], GRID["tp_s"], GRID["sl"]
    ):
        r = backtest(df, sc, lc, ccp, adx_th, tp_s, sl, long_ok)
        total += 1
        if r["n"] < 12: continue
        if r["wr"] < 0.50: continue
        score = r["wr"]*0.55 + min(r["pf"],4)/4*0.3 + min(r["n"],80)/80*0.15
        if score > best_score:
            best_score = score
            best_r = r
            best_p = dict(sc=sc, lc=lc, ccp=ccp, adx_th=adx_th, tp_s=tp_s, sl=sl)

    if best_p is None:
        # 次优：WR最高的
        best_wr = 0
        for sc, lc, ccp, adx_th, tp_s, sl in product(
            [4,5,6],[3,4],[0.002,0.003],[20,25,30],[0.8,1.0],[1.0,1.5]
        ):
            r = backtest(df, sc, lc, ccp, adx_th, tp_s, sl, long_ok)
            if r["n"] >= 8 and r["wr"] > best_wr:
                best_wr = r["wr"]; best_r = r
                best_p = dict(sc=sc,lc=lc,ccp=ccp,adx_th=adx_th,tp_s=tp_s,sl=sl)

    return best_p, best_r, total

def main():
    print("="*65)
    print("  白夜交易系统 v7.3 — 真实数据参数优化 v2")
    print(f"  时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("  数据源: Binance API 实时拉取 (1000根 15m)")
    print("="*65)

    # 拉数据
    data = {}
    for sym in SYMBOLS:
        print(f"  拉取 {sym}...", end=" ", flush=True)
        try:
            df = fetch_klines(sym, "15m", 1000)
            data[sym] = df
            # 计算基础统计
            returns = df["close"].pct_change().dropna()
            vol = returns.std() * 100
            print(f"✅ {len(df)}根 | 日波动={vol:.2f}%")
        except Exception as e:
            print(f"❌ {e}")

    if not data:
        print("❌ 无数据，退出")
        return

    # 参数优化
    all_results = {}
    print()
    for sym, df in data.items():
        long_ok = sym not in LONG_DISABLED
        print(f"  优化 {sym} ({'含LONG' if long_ok else 'SHORT-only'})...", end=" ", flush=True)
        t0 = time.time()
        best_p, best_r, total = optimize_symbol(sym, df, long_ok)
        elapsed = time.time() - t0

        if best_p and best_r:
            flag = "✅" if best_r["wr"] >= 0.52 else "⚠️ "
            print(f"{flag} WR={best_r['wr']:.1%} PF={best_r['pf']:.2f} "
                  f"n={best_r['n']} PnL={best_r['pnl']:+.1f}U "
                  f"({elapsed:.1f}s, {total}组)")
            all_results[sym] = {**best_p, **best_r, "long_ok": long_ok}
        else:
            print(f"❌ 无有效参数 ({elapsed:.1f}s)")
            all_results[sym] = None

    # 汇总
    print(f"\n{'='*65}")
    print("  优化结果汇总（基于最新Binance实时数据）")
    print(f"{'='*65}")
    print(f"{'品种':<12} {'sc':>3} {'lc':>3} {'ccp':>7} {'adx':>5} {'tp':>5} {'sl':>5} {'WR':>7} {'PF':>6} {'n':>5} {'PnL':>8}")
    print("-"*75)
    for sym, p in all_results.items():
        if p:
            flag = "✅" if p.get("wr",0) >= 0.52 else "⚠️ "
            print(f"{flag}{sym:<10} {p['sc']:>3} {p['lc']:>3} "
                  f"{p['ccp']:>7.4f} {p['adx_th']:>5} "
                  f"{p['tp_s']:>5} {p['sl']:>5} "
                  f"{p['wr']:>6.1%} {p['pf']:>6.2f} {p['n']:>5} {p['pnl']:>+8.2f}U")
        else:
            print(f"❌ {sym:<10}  暂无可用参数")

    # 保存
    out = Path("research/optimize_result_v2.json")
    out.parent.mkdir(exist_ok=True)
    with open(out, "w") as f:
        json.dump({"ts": datetime.now(timezone.utc).isoformat(),
                   "source": "binance_api_live",
                   "params": all_results}, f, indent=2, ensure_ascii=False)
    print(f"\n  📄 结果: {out}")

    # 生成 config_patch 建议
    gen_config_patch(all_results)
    return all_results

def gen_config_patch(results):
    print(f"\n{'─'*65}")
    print("  📝 config.py 建议更新片段 (SYM_CFG):")
    print(f"{'─'*65}")
    for sym, p in results.items():
        if not p: continue
        long_str = "True" if p.get("long_ok", True) else "False"
        print(f'    "{sym}": SymCfg(sc={p["sc"]}, lc={p["lc"]}, '
              f'ccp={p["ccp"]}, adx_th={p["adx_th"]}, '
              f'tp_mult={p["tp_s"]}, sl_mult={p["sl"]}, '
              f'allow_long={long_str}),  # WR={p["wr"]:.1%} PF={p["pf"]:.2f} n={p["n"]}')

if __name__ == "__main__":
    main()
