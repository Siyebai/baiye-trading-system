#!/usr/bin/env python3
"""
白夜系统 100笔完整闭环验证 v2.0
- 使用 backtest_engine_v2 (v2.2) 正确接口
- 6品种 × 90天真实K线 → 确保 ≥100笔
- 输出: logs/100trades_result.json + logs/100trades_summary.md
"""
import sys, json, requests, numpy as np, pandas as pd
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))
from engine.backtest_engine_v2 import compute_indicators, generate_signals, backtest_v2, calc_stats

SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","LINKUSDT","POLUSDT"]
INTERVAL = "15m"
DAYS = 90
CAPITAL = 150.0
RISK_PCT = 0.02

CONFIGS = {
    "BTCUSDT":  dict(sc=5, lc=5, ccp=0.003,  adx_th=22, tp_s=0.8, tp_l=0.8, long_disabled=False),
    "ETHUSDT":  dict(sc=5, lc=4, ccp=0.0015, adx_th=20, tp_s=0.8, tp_l=0.7, long_disabled=False),
    "SOLUSDT":  dict(sc=5, lc=4, ccp=0.0015, adx_th=30, tp_s=0.8, tp_l=0.8, long_disabled=False),
    "BNBUSDT":  dict(sc=5, lc=6, ccp=0.0015, adx_th=15, tp_s=0.8, tp_l=0.8, long_disabled=True),
    "LINKUSDT": dict(sc=7, lc=4, ccp=0.0025, adx_th=15, tp_s=0.8, tp_l=0.7, long_disabled=False),
    "POLUSDT":  dict(sc=5, lc=4, ccp=0.0015, adx_th=25, tp_s=1.0, tp_l=0.7, long_disabled=True),
}

def fetch_klines(sym, days=90):
    url = "https://api.binance.com/api/v3/klines"
    limit = days * 96 + 100
    all_data = []
    end_ts = None
    fetched = 0
    while fetched < limit:
        params = {"symbol": sym, "interval": INTERVAL, "limit": min(1000, limit - fetched)}
        if end_ts:
            params["endTime"] = end_ts
        r = requests.get(url, params=params, timeout=20)
        batch = r.json()
        if not batch:
            break
        all_data = batch + all_data
        end_ts = batch[0][0] - 1
        fetched += len(batch)
        if len(batch) < 1000:
            break
    df = pd.DataFrame(all_data, columns=[
        "ts","open","high","low","close","volume",
        "ct","qv","trades","tbb","tbq","ignore"])
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts").sort_index()
    # 只取最近days天
    cutoff = df.index[-1] - pd.Timedelta(days=days)
    return df[df.index >= cutoff]

print("🚀 白夜系统 100笔完整闭环验证 v2.0")
print(f"   品种: {', '.join(SYMBOLS)} | 周期: 15m | 时间跨度: {DAYS}天")
print()

all_trades = []
sym_results = {}

for sym in SYMBOLS:
    cfg = CONFIGS[sym]
    print(f"  📥 {sym}", end="", flush=True)
    try:
        df = fetch_klines(sym, DAYS)
        print(f" {len(df)}根", end="", flush=True)
    except Exception as e:
        print(f" ❌ 数据: {e}")
        continue

    try:
        df2 = compute_indicators(df)
        # 添加 consec_up / consec_down / cum_chg 列（引擎依赖）
        from engine.backtest_engine_v2 import generate_signals
        sigs = generate_signals(df2, sc=cfg["sc"], lc=cfg["lc"], ccp=cfg["ccp"], adx_th=cfg["adx_th"])

        # 如 long_disabled，把 LONG 信号清零
        if cfg["long_disabled"]:
            sigs[sigs == 1] = 0

        trades = backtest_v2(df2, sigs,
                             tp_s=cfg["tp_s"], tp_l=cfg["tp_l"],
                             capital=CAPITAL, risk_pct=RISK_PCT)
    except Exception as e:
        print(f" ❌ 回测: {e}")
        import traceback; traceback.print_exc()
        continue

    # 给每笔trade打上品种+方向+时间标签
    ts_arr = df2.index.values
    n = len(ts_arr)
    for t in trades:
        t["symbol"] = sym
        t["direction"] = "LONG" if t["dir"] == 1 else "SHORT"
        t["result"] = "TP" if t["win"] else "SL"
        # 推算开仓时间（信号根 = bar-1）
        bar_idx = t.get("bar", 0)
        if 0 < bar_idx < n:
            t["close_ts"] = str(ts_arr[bar_idx])
            t["open_ts"] = str(ts_arr[max(0, bar_idx - 4)])  # 近似
        else:
            t["close_ts"] = t["open_ts"] = ""
        all_trades.append(t)

    stats = calc_stats(trades, capital=CAPITAL, days=DAYS)
    print(f" ✅ {len(trades)}笔 WR={stats.get('wr',0):.1%} 月均={stats.get('monthly_return',0):+.1%}")
    sym_results[sym] = {
        "trades": len(trades),
        "win_rate": stats.get("wr", 0),
        "monthly_avg": stats.get("monthly_return", 0),
        "max_drawdown": stats.get("max_dd", 0),
        "final_equity": stats.get("final_equity", CAPITAL),
        "profit_factor": stats.get("pf", 0),
    }

print()
total = len(all_trades)
print(f"{'='*55}")
print(f"  总交易笔数: {total}")

if total == 0:
    print("❌ 无交易，退出")
    sys.exit(1)

wins = sum(1 for t in all_trades if t.get("win", False))
wr = wins / total
gross_pnl = sum(t.get("pnl", 0) for t in all_trades)
gross_win = sum(t["pnl"] for t in all_trades if t.get("pnl", 0) > 0)
gross_loss = sum(t["pnl"] for t in all_trades if t.get("pnl", 0) <= 0)
pf = abs(gross_win / gross_loss) if gross_loss != 0 else float("inf")
final_eq = CAPITAL + gross_pnl

print(f"  胜率: {wr:.1%} | 总PnL: {gross_pnl:+.2f}U | 最终资金: {final_eq:.2f}U")
print(f"  盈利因子: {pf:.2f} | 总盈: {gross_win:+.2f}U | 总亏: {gross_loss:+.2f}U")
print()

# ── 保存JSON ──
out_path = Path("logs/100trades_result.json")
out_path.parent.mkdir(exist_ok=True)
output = {
    "meta": {
        "generated": datetime.now(timezone.utc).isoformat(),
        "engine": "backtest_engine_v2 v2.2",
        "days": DAYS, "interval": INTERVAL,
        "capital": CAPITAL, "risk_pct": RISK_PCT,
        "total_trades": total,
        "win_rate": round(wr, 4),
        "profit_factor": round(pf, 4),
        "gross_pnl": round(gross_pnl, 4),
        "final_equity": round(final_eq, 4),
    },
    "symbol_summary": sym_results,
    "trades": all_trades,
}
out_path.write_text(json.dumps(output, indent=2, default=str))
print(f"  💾 完整记录: {out_path}")

# ── Markdown报告 ──
md_lines = [
    "# 白夜系统 100笔完整闭环验证报告",
    f"**生成**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} | **引擎**: v2.2 | **跨度**: {DAYS}天 15m",
    "",
    "## 总体结果",
    f"| 指标 | 值 |",
    f"|------|-----|",
    f"| 总交易笔数 | **{total}** |",
    f"| 胜率 | **{wr:.1%}** |",
    f"| 总净PnL | **{gross_pnl:+.2f}U** ({(gross_pnl/CAPITAL)*100:+.1f}%) |",
    f"| 盈利因子 | **{pf:.2f}** |",
    f"| 最终资金 | **{final_eq:.2f}U** (初始 {CAPITAL}U) |",
    "",
    "## 各品种表现",
    "| 品种 | 笔数 | 胜率 | 月均% | 最大回撤 | PF | 终值 |",
    "|------|------|------|-------|---------|-----|------|",
]
for sym, r in sym_results.items():
    md_lines.append(
        f"| {sym} | {r['trades']} | {r['win_rate']:.1%} | "
        f"{r['monthly_avg']:+.1%} | {r['max_drawdown']:.1%} | "
        f"{r['profit_factor']:.2f} | {r['final_equity']:.1f}U |"
    )
md_lines += [
    "",
    "## 交易明细（前50笔）",
    "| # | 品种 | 方向 | 结果 | PnL(U) | 入场价 | 出场价 |",
    "|---|------|------|------|--------|--------|--------|",
]
for i, t in enumerate(all_trades[:50], 1):
    res = "✅TP" if t.get("win") else "❌SL"
    pnl = t.get("pnl", 0)
    entry = round(t.get("entry", 0), 4)
    ex = round(t.get("exit", 0), 4)
    md_lines.append(f"| {i} | {t['symbol']} | {t['direction']} | {res} | {pnl:+.3f} | {entry} | {ex} |")

if total > 50:
    md_lines.append(f"\n*…共 {total} 笔，完整数据见 logs/100trades_result.json*")

md_path = Path("logs/100trades_summary.md")
md_path.write_text("\n".join(md_lines))
print(f"  📄 摘要报告: {md_path}")
print()
print("✅ 完成！")
