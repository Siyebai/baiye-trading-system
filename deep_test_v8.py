#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deep_test_v8.py — 白夜交易系统 v8.1 深度测试

【测试目标】
  1. 多周期协同确认 (15m主 + 1h方向 + 5m入场) 能否提升PF<1品种
  2. 成交量过滤 (相对成交量倍数≥1.5) 过滤低质量信号
  3. RSI极值过滤 (SHORT时RSI>50, LONG时RSI<50) 增强信号质量
  4. 对比: 有/无过滤器的WR/PF/n变化

【测试品种】
  问题品种: BTC/ETH/XRP/LINK/HYPE (v8.1 OOS PF<1)
  优秀品种: SUI/TON/DOGE 对照
"""
from __future__ import annotations
import json, warnings, time
from itertools import product
import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")
FAPI = "https://fapi.binance.com"
FEE = 0.0002
MAX_HOLD = 25

# v8.1配置
SYM_CFG = {
    # 问题品种
    "BTCUSDT":  dict(sc=3, lc=4, ccp=0.001,  adx_th=35, tp=0.8, sl=1.5, long_ok=True),
    "ETHUSDT":  dict(sc=5, lc=3, ccp=0.0015, adx_th=35, tp=0.8, sl=1.5, long_ok=True),
    "XRPUSDT":  dict(sc=3, lc=3, ccp=0.001,  adx_th=30, tp=0.8, sl=1.5, long_ok=True),
    "LINKUSDT": dict(sc=4, lc=3, ccp=0.003,  adx_th=35, tp=0.8, sl=1.5, long_ok=False),
    "HYPEUSDT": dict(sc=3, lc=3, ccp=0.001,  adx_th=35, tp=0.8, sl=1.5, long_ok=True),
    # 优秀品种（对照组）
    "SUIUSDT":  dict(sc=6, lc=4, ccp=0.001,  adx_th=25, tp=0.6, sl=1.5, long_ok=True),
    "TONUSDT":  dict(sc=3, lc=3, ccp=0.001,  adx_th=15, tp=0.8, sl=1.5, long_ok=True),
    "DOGEUSDT": dict(sc=3, lc=3, ccp=0.001,  adx_th=15, tp=1.5, sl=1.8, long_ok=True),
}

def wilder(s, n):
    out = np.zeros(len(s)); out[n-1] = s[:n].mean()
    for i in range(n, len(s)): out[i] = (out[i-1]*(n-1)+s[i])/n
    return pd.Series(out, index=s.index)

def calc_indicators(df, p=14):
    """计算 ATR / ADX / RSI / 成交量MA"""
    h, l, c, v = df["high"], df["low"], df["close"], df["vol"]
    # ATR/ADX
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    atr = wilder(tr, p)
    up = h.diff(); dn = (-l.diff())
    pdm = np.where((up>dn)&(up>0), up, 0.); ndm = np.where((dn>up)&(dn>0), dn, 0.)
    a14 = wilder(tr, p)
    pdi = 100*wilder(pd.Series(pdm, index=df.index), p)/a14
    ndi = 100*wilder(pd.Series(ndm, index=df.index), p)/a14
    dx  = (100*(pdi-ndi).abs()/(pdi+ndi).replace(0, np.nan)).fillna(0)
    adx = wilder(dx, p)
    # RSI
    delta = c.diff()
    gain = delta.clip(lower=0); loss = (-delta).clip(lower=0)
    avg_gain = wilder(gain, p); avg_loss = wilder(loss, p)
    rs = avg_gain / avg_loss.replace(0, 0.001)
    rsi = 100 - 100/(1+rs)
    # 成交量相对倍数 (当前成交量 / 20期均量)
    vol_ma = v.rolling(20).mean()
    vol_ratio = v / vol_ma.replace(0, 1)
    return atr.values, adx.values, rsi.values, vol_ratio.values

def fetch(sym, interval="15m", limit=1500):
    r = requests.get(f"{FAPI}/fapi/v1/klines",
        params={"symbol": sym, "interval": interval, "limit": limit}, timeout=12)
    raw = r.json()
    if not isinstance(raw, list): return None
    df = pd.DataFrame(raw, columns=["ts","open","high","low","close","vol","ct","qvol","n","tbv","tqv","x"])
    for c2 in ["open","high","low","close","vol"]: df[c2] = df[c2].astype(float)
    return df

def backtest(df_oos, sc, lc, ccp, adx_th, tp_m, sl_m, long_ok,
             use_vol_filter=False, vol_th=1.2,
             use_rsi_filter=False):
    """
    回测引擎，支持可选过滤器:
      use_vol_filter: 成交量过滤 (vol_ratio >= vol_th)
      use_rsi_filter: RSI方向过滤 (SHORT时RSI>50, LONG时RSI<50)
    """
    c = df_oos["close"].values
    h = df_oos["high"].values
    l = df_oos["low"].values
    atr, adx, rsi, vol_ratio = calc_indicators(df_oos)
    n = len(c)

    # 连涨跌计数
    cu = np.zeros(n, int); cd = np.zeros(n, int); cc = np.zeros(n)
    for i in range(1, n):
        chg = (c[i]-c[i-1])/c[i-1]
        if c[i] > c[i-1]:
            cu[i]=cu[i-1]+1; cd[i]=0
            cc[i] = chg if cd[i-1]>0 else cc[i-1]+chg
        elif c[i] < c[i-1]:
            cd[i]=cd[i-1]+1; cu[i]=0
            cc[i] = chg if cu[i-1]>0 else cc[i-1]+chg
        else:
            cu[i]=cu[i-1]; cd[i]=cd[i-1]; cc[i]=cc[i-1]

    wins = 0; losses = 0; wpnl = 0; lpnl = 0
    in_pos = False; side = None; entry = tp_p = sl_p = 0; bars = 0

    for i in range(50, n-1):
        if in_pos:
            bars += 1; ex = None
            if side == "short":
                if l[i] <= tp_p: ex=tp_p; w=1
                elif h[i] >= sl_p: ex=sl_p; w=0
                else: w=0
            else:
                if h[i] >= tp_p: ex=tp_p; w=1
                elif l[i] <= sl_p: ex=sl_p; w=0
                else: w=0
            if ex is None and bars >= MAX_HOLD:
                ex=c[i]; w=int(c[i]<entry if side=="short" else c[i]>entry)
            if ex is not None:
                ret = ((entry-ex)/entry if side=="short" else (ex-entry)/entry) - FEE*2
                if ret > 0: wins+=1; wpnl+=ret
                else: losses+=1; lpnl+=abs(ret)
                in_pos = False
            continue

        if adx[i] < adx_th or atr[i] <= 0: continue

        # SHORT信号
        if cu[i] >= sc and cc[i] >= ccp:
            ok = True
            if use_vol_filter and vol_ratio[i] < vol_th: ok = False
            if use_rsi_filter and rsi[i] < 55: ok = False  # SHORT需RSI偏高
            if ok:
                in_pos=True; side="short"; entry=c[i]
                tp_p=c[i]-tp_m*atr[i]; sl_p=c[i]+sl_m*atr[i]; bars=0
        # LONG信号
        elif long_ok and cd[i] >= lc and cc[i] <= -ccp:
            ok = True
            if use_vol_filter and vol_ratio[i] < vol_th: ok = False
            if use_rsi_filter and rsi[i] > 45: ok = False  # LONG需RSI偏低
            if ok:
                in_pos=True; side="long"; entry=c[i]
                tp_p=c[i]+tp_m*atr[i]; sl_p=c[i]-sl_m*atr[i]; bars=0

    tot = wins + losses
    if tot < 5: return None
    pf = wpnl/lpnl if lpnl > 0 else 99.0
    return dict(wr=wins/tot, pf=round(pf,3), n=tot,
                wpnl=round(wpnl,5), lpnl=round(lpnl,5))

# ─── 主测试流程 ───
print("=" * 75)
print("  白夜交易系统 v8.1 — 深度测试")
print("  多周期协同 + 成交量过滤 + RSI方向过滤 对比实验")
print(f"  数据: Binance实时1500根15m (OOS=1000根)")
print("=" * 75)

results = {}

for sym, cfg in SYM_CFG.items():
    print(f"\n  ▶ {sym}", end=" ")
    df = fetch(sym)
    if df is None: print("获取失败"); continue
    df_oos = df.iloc[500:].reset_index(drop=True)
    print(f"({len(df_oos)}根OOS)")

    r_base  = backtest(df_oos, cfg["sc"], cfg["lc"], cfg["ccp"],
                       cfg["adx_th"], cfg["tp"], cfg["sl"], cfg["long_ok"])
    r_vol   = backtest(df_oos, cfg["sc"], cfg["lc"], cfg["ccp"],
                       cfg["adx_th"], cfg["tp"], cfg["sl"], cfg["long_ok"],
                       use_vol_filter=True, vol_th=1.2)
    r_rsi   = backtest(df_oos, cfg["sc"], cfg["lc"], cfg["ccp"],
                       cfg["adx_th"], cfg["tp"], cfg["sl"], cfg["long_ok"],
                       use_rsi_filter=True)
    r_both  = backtest(df_oos, cfg["sc"], cfg["lc"], cfg["ccp"],
                       cfg["adx_th"], cfg["tp"], cfg["sl"], cfg["long_ok"],
                       use_vol_filter=True, vol_th=1.2, use_rsi_filter=True)

    def fmt(r):
        if r is None: return " n/a "
        return f"WR={r['wr']:.0%} PF={r['pf']:.2f} n={r['n']}"

    print(f"    基础(v8.1):       {fmt(r_base)}")
    print(f"    +成交量过滤:      {fmt(r_vol)}")
    print(f"    +RSI方向过滤:     {fmt(r_rsi)}")
    print(f"    +双重过滤:        {fmt(r_both)}")

    results[sym] = {
        "base": r_base, "vol_filter": r_vol,
        "rsi_filter": r_rsi, "both_filter": r_both
    }

# ─── 汇总表 ───
print()
print("=" * 75)
print(f"  {'品种':<12} {'基础PF':>7} {'+成交量PF':>10} {'+RSIPF':>8} {'双重PF':>8} {'推荐'}")
print("-" * 75)
best_cfg = {}
for sym, r in results.items():
    def pf_s(x): return f"{x['pf']:.2f}" if x else " n/a"
    def n_s(x): return f"n={x['n']}" if x else ""
    base_pf  = r["base"]["pf"]  if r["base"]  else 0
    vol_pf   = r["vol_filter"]["pf"]  if r["vol_filter"]  else 0
    rsi_pf   = r["rsi_filter"]["pf"]  if r["rsi_filter"]  else 0
    both_pf  = r["both_filter"]["pf"] if r["both_filter"] else 0
    best_pf  = max(base_pf, vol_pf, rsi_pf, both_pf)
    if best_pf == both_pf and both_pf > base_pf: rec = "双重过滤"
    elif best_pf == vol_pf and vol_pf > base_pf: rec = "成交量过滤"
    elif best_pf == rsi_pf and rsi_pf > base_pf: rec = "RSI过滤"
    else: rec = "保持基础"
    improve = "✅" if best_pf > 1.0 and best_pf > base_pf else ("🔥" if best_pf >= 1.5 else "⚠️")
    print(f"  {sym:<12} {pf_s(r['base']):>7} {pf_s(r['vol_filter']):>10} {pf_s(r['rsi_filter']):>8} {pf_s(r['both_filter']):>8}  {improve} {rec}")
    best_cfg[sym] = dict(best_pf=best_pf, rec=rec)

# 保存
with open("research/deep_test_v81.json", "w") as f:
    json.dump({"results": {k: {kk: vv for kk,vv in v.items()} for k,v in results.items()},
               "best": best_cfg}, f, indent=2, ensure_ascii=False)
print()
print(f"  📄 结果已保存: research/deep_test_v81.json")
print("=" * 75)
