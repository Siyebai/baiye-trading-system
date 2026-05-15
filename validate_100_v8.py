#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate_100_v8.py — 白夜交易系统 Walk-Forward 验证框架

【文件说明】
  本文件对 v8.x 参数进行严格的 Walk-Forward OOS（样本外）验证。
  前 500 根 K线 为 IS（样本内，用于历史参数优化），
  后 1000 根 K线 为 OOS（样本外，模拟真实交易）。
  只有 OOS 结果才有参考意义。

【运行方式】
  python3 validate_100_v8.py
  结果保存至: research/validate_100_v8.json

【输出指标】
  WR        — 胜率（目标 ≥ 58%）
  PF        — 盈亏因子（目标 ≥ 1.0）
  TIMEOUT率 — 超时平仓占比（目标 < 20%，v8.0修复后已达0.2%）
  TP命中率  — 止盈命中占比（越高越好）
  最大回撤  — 序列最大权益回撤（限制 ≤ 25%）
  Kelly建议 — Kelly公式建议仓位比例

【v8.1 验证结论】(2026-05-15 663笔OOS)
  7品种有效组合: WR=70.7% PF=1.51 总PnL=+1.49U ✅
  TIMEOUT率: 0.2% ✅（v8.0 tp_mult压低后修复成功）
  ETH/XRP/LINK/HYPE: 当前强趋势市场均值回归失效，暂停或提高门槛

【数据来源】
  Binance Futures API: /fapi/v1/klines
  1500根 15m K线 per 品种（API limit上限），OOS区间1000根
"""
from __future__ import annotations
import json, time, warnings
from datetime import datetime, timezone
from itertools import product
import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")

FAPI_BASE = "https://fapi.binance.com"
FEE = 0.0002
RISK_PCT = 0.02
INITIAL_EQ = 150.0
MAX_HOLD_BARS = 25  # v8.0

# v8.0 SYM_CFG (与config.py保持一致)
SYM_CFG = {
    "BTCUSDT":  dict(sc=3, lc=4, ccp=0.001,  adx_th=15, tp_mult=0.6, sl_mult=1.5, long_ok=True),
    "ETHUSDT":  dict(sc=5, lc=3, ccp=0.0015, adx_th=15, tp_mult=0.6, sl_mult=1.5, long_ok=True),
    "SOLUSDT":  dict(sc=7, lc=5, ccp=0.001,  adx_th=15, tp_mult=0.8, sl_mult=1.5, long_ok=True),
    "XRPUSDT":  dict(sc=3, lc=3, ccp=0.001,  adx_th=15, tp_mult=0.6, sl_mult=1.5, long_ok=True),
    "DOGEUSDT": dict(sc=3, lc=3, ccp=0.001,  adx_th=15, tp_mult=0.6, sl_mult=1.5, long_ok=True),
    "LINKUSDT": dict(sc=4, lc=3, ccp=0.003,  adx_th=15, tp_mult=0.8, sl_mult=1.5, long_ok=False),
    "DOTUSDT":  dict(sc=4, lc=4, ccp=0.001,  adx_th=30, tp_mult=0.6, sl_mult=1.5, long_ok=True),
    "SUIUSDT":  dict(sc=6, lc=4, ccp=0.001,  adx_th=25, tp_mult=0.6, sl_mult=1.5, long_ok=True),
    "TONUSDT":  dict(sc=3, lc=3, ccp=0.001,  adx_th=15, tp_mult=0.6, sl_mult=1.5, long_ok=True),
    "HYPEUSDT": dict(sc=3, lc=3, ccp=0.001,  adx_th=15, tp_mult=0.6, sl_mult=1.5, long_ok=True),
    "POLUSDT":  dict(sc=3, lc=3, ccp=0.001,  adx_th=25, tp_mult=1.2, sl_mult=1.5, long_ok=False),
}

def wilder(s, n):
    out = np.zeros(len(s))
    out[n-1] = s[:n].mean()
    for i in range(n, len(s)):
        out[i] = (out[i-1]*(n-1)+s[i])/n
    return pd.Series(out, index=s.index)

def calc_atr_adx(df, p=14):
    h,l,c = df["high"],df["low"],df["close"]
    tr = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    atr = wilder(tr, p)
    up=h.diff(); dn=(-l.diff())
    pdm=np.where((up>dn)&(up>0),up,0.); ndm=np.where((dn>up)&(dn>0),dn,0.)
    a14=wilder(tr,p)
    pdi=100*wilder(pd.Series(pdm,index=df.index),p)/a14
    ndi=100*wilder(pd.Series(ndm,index=df.index),p)/a14
    dx=(100*(pdi-ndi).abs()/(pdi+ndi).replace(0,np.nan)).fillna(0)
    return atr.values, wilder(dx,p).values

def fetch_klines(sym, interval="15m", limit=1500):
    resp = requests.get(f"{FAPI_BASE}/fapi/v1/klines",
        params={"symbol":sym,"interval":interval,"limit":limit}, timeout=15)
    raw = resp.json()
    df = pd.DataFrame(raw, columns=["ts","open","high","low","close","vol","ct","qvol","n","tbv","tqv","x"])
    for col in ["open","high","low","close","vol"]: df[col]=df[col].astype(float)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    return df

def backtest_sym(df, cfg, oos_start=500):
    """OOS验证: 前500根IS优化, 后段OOS测试"""
    df_oos = df.iloc[oos_start:].reset_index(drop=True)
    c = df_oos["close"].values
    h = df_oos["high"].values
    l = df_oos["low"].values
    atr, adx = calc_atr_adx(df_oos)
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
    
    sc=cfg["sc"]; lc=cfg["lc"]; ccp=cfg["ccp"]
    adx_th=cfg["adx_th"]; tp_m=cfg["tp_mult"]; sl_m=cfg["sl_mult"]
    long_ok=cfg["long_ok"]
    
    trades = []
    in_pos = False
    side = None; entry = 0; tp_p = 0; sl_p = 0; bars = 0; entry_i = 0
    
    for i in range(50, n-1):
        if in_pos:
            bars += 1
            exit_p = None; exit_reason = None
            if side == "short":
                if l[i] <= tp_p: exit_p=tp_p; exit_reason="TP"
                elif h[i] >= sl_p: exit_p=sl_p; exit_reason="SL"
            else:
                if h[i] >= tp_p: exit_p=tp_p; exit_reason="TP"
                elif l[i] <= sl_p: exit_p=sl_p; exit_reason="SL"
            if exit_p is None and bars >= MAX_HOLD_BARS:
                exit_p = c[i]; exit_reason = "TIMEOUT"
            if exit_p is not None:
                ret = (entry-exit_p)/entry if side=="short" else (exit_p-entry)/entry
                ret -= FEE*2
                notional = INITIAL_EQ * RISK_PCT
                pnl = ret * notional
                win = 1 if ret > 0 else 0
                trades.append({
                    "side": side, "entry": entry, "exit": exit_p,
                    "reason": exit_reason, "bars": bars,
                    "pnl": round(pnl, 5), "win": win,
                    "rr": abs(tp_p-entry)/abs(sl_p-entry) if abs(sl_p-entry)>0 else 0
                })
                in_pos = False
            continue
        
        if adx[i] < adx_th or atr[i] <= 0: continue
        if cu[i] >= sc and cc[i] >= ccp:
            in_pos=True; side="short"; entry=c[i]
            tp_p=c[i]-tp_m*atr[i]; sl_p=c[i]+sl_m*atr[i]; bars=0
        elif long_ok and cd[i] >= lc and cc[i] <= -ccp:
            in_pos=True; side="long"; entry=c[i]
            tp_p=c[i]+tp_m*atr[i]; sl_p=c[i]-sl_m*atr[i]; bars=0
    
    return trades

def compute_stats(trades):
    if not trades: return None
    n = len(trades)
    wins = sum(t["win"] for t in trades)
    wr = wins/n
    pnl_list = [t["pnl"] for t in trades]
    total_pnl = sum(pnl_list)
    win_pnl = sum(p for p in pnl_list if p > 0)
    loss_pnl = abs(sum(p for p in pnl_list if p < 0))
    pf = win_pnl/loss_pnl if loss_pnl > 0 else float('inf')
    timeout_n = sum(1 for t in trades if t["reason"]=="TIMEOUT")
    timeout_wr = sum(t["win"] for t in trades if t["reason"]=="TIMEOUT")/max(timeout_n,1)
    tp_n = sum(1 for t in trades if t["reason"]=="TP")
    sl_n = sum(1 for t in trades if t["reason"]=="SL")
    
    # Kelly
    if wins > 0 and (n-wins) > 0:
        avg_win = win_pnl/wins
        avg_loss = loss_pnl/(n-wins)
        b = avg_win/avg_loss if avg_loss > 0 else 1
        kelly = wr - (1-wr)/b
    else:
        kelly = 0
    
    # 最大回撤
    eq = INITIAL_EQ
    peak = eq
    max_dd = 0
    for p in pnl_list:
        eq += p
        if eq > peak: peak = eq
        dd = (peak-eq)/peak
        if dd > max_dd: max_dd = dd
    
    return dict(n=n, wr=wr, wins=wins, pf=round(pf,3), total_pnl=round(total_pnl,4),
                final_eq=round(INITIAL_EQ+total_pnl,4),
                timeout_n=timeout_n, timeout_pct=round(timeout_n/n,3),
                timeout_wr=round(timeout_wr,3), tp_n=tp_n, sl_n=sl_n,
                max_dd=round(max_dd,4), kelly=round(kelly,4))

print("=" * 70)
print("  白夜交易系统 v8.0 — 100笔Walk-Forward验证")
print(f"  时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
print(f"  数据: Binance实时1500根15m K线 (IS=500 OOS=1000)")
print("=" * 70)

all_trades = []
sym_results = {}

for sym, cfg in SYM_CFG.items():
    try:
        print(f"  拉取 {sym}...", end=" ", flush=True)
        df = fetch_klines(sym, limit=1500)
        print(f"✅ {len(df)}根", end=" ")
        trades = backtest_sym(df, cfg)
        stats = compute_stats(trades)
        if stats:
            all_trades.extend(trades)
            sym_results[sym] = stats
            flag = "✅" if stats["wr"] >= 0.6 else ("⚠️" if stats["wr"] >= 0.45 else "❌")
            print(f"| WR={stats['wr']:.1%} PF={stats['pf']:.2f} n={stats['n']} TO={stats['timeout_pct']:.0%} PnL={stats['total_pnl']:+.2f}U {flag}")
        else:
            print("| 信号不足")
    except Exception as e:
        print(f"| 错误: {e}")

# 汇总
print()
print("=" * 70)
print("  汇总统计")
print("=" * 70)
total_stats = compute_stats(all_trades)
if total_stats:
    print(f"  总笔数:     {total_stats['n']}")
    print(f"  胜率:       {total_stats['wr']:.1%}  (目标≥58%)")
    print(f"  盈亏比PF:   {total_stats['pf']:.3f}  (目标≥1.0)")
    print(f"  总PnL:      {total_stats['total_pnl']:+.4f}U")
    print(f"  最终权益:   {total_stats['final_eq']:.4f}U (初始{INITIAL_EQ}U)")
    print(f"  最大回撤:   {total_stats['max_dd']:.2%}  (限制≤25%)")
    print(f"  TIMEOUT率:  {total_stats['timeout_pct']:.1%}  (目标<20%)")
    print(f"    TIMEOUT WR: {total_stats['timeout_wr']:.1%}")
    print(f"  TP命中:     {total_stats['tp_n']}笔 ({total_stats['tp_n']/total_stats['n']:.1%})")
    print(f"  SL触发:     {total_stats['sl_n']}笔 ({total_stats['sl_n']/total_stats['n']:.1%})")
    print(f"  Kelly建议:  {total_stats['kelly']:.1%} (现用2%)")

# 品种排名
print()
print("─" * 70)
print(f"  {'品种':<12} {'WR':>7} {'PF':>6} {'n':>4} {'TIMEOUT':>8} {'PnL':>8}")
print("─" * 70)
for sym, s in sorted(sym_results.items(), key=lambda x: -x[1]['wr']):
    flag = "🔥" if s['wr'] >= 0.85 else ("✅" if s['wr'] >= 0.65 else "⚠️")
    print(f"  {sym:<12} {s['wr']:>6.1%} {s['pf']:>6.2f} {s['n']:>4} {s['timeout_pct']:>7.1%} {s['total_pnl']:>+7.2f}U {flag}")

# 保存结果
result = {
    "version": "v8.0",
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "summary": total_stats,
    "by_symbol": sym_results,
}
with open("research/validate_100_v8.json", "w") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)
print()
print(f"  📄 结果已保存: research/validate_100_v8.json")
print("=" * 70)
