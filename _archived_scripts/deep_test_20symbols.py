#!/usr/bin/env python3
"""
白夜系统 - 扩展20品种深度回测
90天真实15m K线数据 + 网格参数搜索
"""
import requests
import pandas as pd
import sys
import json
import numpy as np
import itertools
import time
from pathlib import Path

sys.path.insert(0, '/root/.openclaw/workspace/killer-trading-system')
from engine.backtest_engine_v2 import compute_indicators, generate_signals, backtest_v2, calc_stats

SYMBOLS = [
    # 已验证
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'LINKUSDT', 'POLUSDT',
    # 新增
    'XRPUSDT', 'DOTUSDT', 'UNIUSDT', 'AAVEUSDT', 'OPUSDT', 'ARBUSDT',
    'SUIUSDT', 'TIAUSDT', 'ENAUSDT', 'WIFUSDT', 'JUPUSDT', 'NEARUSDT',
    'ATOMUSDT', 'LTCUSDT'
]

DAYS = 90

# 默认参数
DEFAULT_SC = 5
DEFAULT_LC = 4
DEFAULT_CCP = 0.002
DEFAULT_ADX_TH = 20
DEFAULT_TP_S = 0.8
DEFAULT_TP_L = 0.7
DEFAULT_SL_ATR = 1.5

# 网格范围
GRID_SC = [4, 5, 6, 7]
GRID_LC = [3, 4, 5, 6]
GRID_CCP = [0.001, 0.0015, 0.002, 0.0025, 0.003]
GRID_ADX_TH = [12, 15, 18, 20, 25]


def fetch_klines(sym, days=90, interval='15m'):
    """从Binance拉取spot K线数据"""
    limit = days * 96 + 200
    all_data = []
    end_ts = None
    fetched = 0
    max_iter = 20
    itr = 0
    while fetched < limit and itr < max_iter:
        itr += 1
        params = {"symbol": sym, "interval": interval, "limit": min(1000, limit - fetched)}
        if end_ts:
            params["endTime"] = end_ts
        try:
            r = requests.get("https://api.binance.com/api/v3/klines", params=params, timeout=20)
            batch = r.json()
        except Exception as e:
            print(f"  [{sym}] fetch error: {e}")
            break
        if not batch or isinstance(batch, dict):
            break
        all_data = batch + all_data
        end_ts = batch[0][0] - 1
        fetched += len(batch)
        if len(batch) < 1000:
            break
        time.sleep(0.1)

    if not all_data:
        return None

    df = pd.DataFrame(all_data, columns=["ts","open","high","low","close","volume","ct","qv","trades","tbb","tbq","ignore"])
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts").sort_index()
    cutoff = df.index[-1] - pd.Timedelta(days=days)
    df = df[df.index >= cutoff][["open","high","low","close","volume"]]
    return df


def run_backtest(df, sc, lc, ccp, adx_th, tp_s=DEFAULT_TP_S, tp_l=DEFAULT_TP_L, sl_atr=DEFAULT_SL_ATR):
    """运行单次回测，返回stats"""
    try:
        df_ind = compute_indicators(df)
        sigs = generate_signals(df_ind, sc=sc, lc=lc, ccp=ccp, adx_th=adx_th)
        trades = backtest_v2(df_ind, sigs, tp_s=tp_s, tp_l=tp_l, sl_atr=sl_atr, capital=150.0, risk_pct=0.02)
        stats = calc_stats(trades, capital=150.0, days=DAYS)
        return stats
    except Exception as e:
        return None


def grid_search(df, sym):
    """网格搜索最优参数"""
    best = None
    best_pf = -1
    total_combos = len(GRID_SC) * len(GRID_LC) * len(GRID_CCP) * len(GRID_ADX_TH)
    checked = 0

    for sc, lc, ccp, adx_th in itertools.product(GRID_SC, GRID_LC, GRID_CCP, GRID_ADX_TH):
        checked += 1
        stats = run_backtest(df, sc, lc, ccp, adx_th)
        if stats is None:
            continue
        # 筛选条件：WR≥55%, monthly>0, trades≥30
        if stats['wr'] >= 55.0 and stats['monthly_return'] > 0 and stats['trades'] >= 30:
            if stats['pf'] > best_pf:
                best_pf = stats['pf']
                best = {**stats, 'sc': sc, 'lc': lc, 'ccp': ccp, 'adx_th': adx_th}

    print(f"    网格搜索完成: {checked}/{total_combos} 组合")
    return best


def main():
    print("=" * 60)
    print("白夜系统 - 扩展20品种深度回测")
    print(f"测试期: 90天 | 时间框架: 15m")
    print("=" * 60)

    results = []
    skip_list = []

    for i, sym in enumerate(SYMBOLS):
        print(f"\n[{i+1}/{len(SYMBOLS)}] 处理 {sym}...")

        # 下载数据
        df = fetch_klines(sym, days=DAYS)
        if df is None or len(df) < 500:
            print(f"  ⚠ 数据不足，跳过 {sym} (rows={len(df) if df is not None else 0})")
            skip_list.append(sym)
            continue

        print(f"  数据: {len(df)} 根K线 ({df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')})")

        # 默认参数回测
        default_stats = run_backtest(df, DEFAULT_SC, DEFAULT_LC, DEFAULT_CCP, DEFAULT_ADX_TH,
                                     DEFAULT_TP_S, DEFAULT_TP_L, DEFAULT_SL_ATR)
        if default_stats:
            print(f"  默认参数: trades={default_stats['trades']}, WR={default_stats['wr']}%, "
                  f"月均={default_stats['monthly_return']}%, PF={default_stats['pf']}")

        # 网格搜索
        print(f"  执行网格搜索 ({len(GRID_SC)*len(GRID_LC)*len(GRID_CCP)*len(GRID_ADX_TH)} 组合)...")
        best_stats = grid_search(df, sym)

        if best_stats:
            print(f"  最优参数: sc={best_stats['sc']}, lc={best_stats['lc']}, "
                  f"ccp={best_stats['ccp']}, adx_th={best_stats['adx_th']}")
            print(f"  最优结果: trades={best_stats['trades']}, WR={best_stats['wr']}%, "
                  f"月均={best_stats['monthly_return']}%, 最大回撤={best_stats['max_dd']}%, PF={best_stats['pf']}")
            is_verified = sym in ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'LINKUSDT', 'POLUSDT']
            results.append({
                'symbol': sym,
                'type': '已验证' if is_verified else '新增',
                'trades': best_stats['trades'],
                'wr': best_stats['wr'],
                'monthly_return': best_stats['monthly_return'],
                'max_dd': best_stats['max_dd'],
                'pf': best_stats['pf'],
                'final_equity': best_stats['final_equity'],
                'sc': best_stats['sc'],
                'lc': best_stats['lc'],
                'ccp': best_stats['ccp'],
                'adx_th': best_stats['adx_th'],
                'status': '✅ 优秀' if best_stats['wr'] >= 60 and best_stats['monthly_return'] >= 1.5 else '✓ 合格'
            })
        else:
            # 没有满足条件的最优参数，记录默认结果
            if default_stats and default_stats['trades'] > 0:
                is_verified = sym in ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'LINKUSDT', 'POLUSDT']
                results.append({
                    'symbol': sym,
                    'type': '已验证' if is_verified else '新增',
                    'trades': default_stats['trades'],
                    'wr': default_stats['wr'],
                    'monthly_return': default_stats['monthly_return'],
                    'max_dd': default_stats['max_dd'],
                    'pf': default_stats['pf'],
                    'final_equity': default_stats['final_equity'],
                    'sc': DEFAULT_SC,
                    'lc': DEFAULT_LC,
                    'ccp': DEFAULT_CCP,
                    'adx_th': DEFAULT_ADX_TH,
                    'status': '⚠ 无优化参数(默认)'
                })
                print(f"  未找到满足WR≥55%+月均>0+trades≥30的参数组合，记录默认参数结果")
            else:
                skip_list.append(sym)
                print(f"  ❌ 无有效回测结果")

    # 按WR降序排序
    results.sort(key=lambda x: x['wr'], reverse=True)

    # 保存JSON
    Path('/root/.openclaw/workspace/killer-trading-system/research').mkdir(exist_ok=True)
    with open('/root/.openclaw/workspace/killer-trading-system/research/expand_20symbols_results.json', 'w') as f:
        json.dump({'results': results, 'skipped': skip_list}, f, indent=2, ensure_ascii=False)

    # 生成报告
    generate_report(results, skip_list)

    print("\n" + "=" * 60)
    print("DEEP_TEST_COMPLETE")
    print(f"总计: {len(results)} 个品种完成回测, {len(skip_list)} 个跳过")
    if results:
        top3 = results[:3]
        print("\n📊 TOP3 排行:")
        for r in top3:
            print(f"  {r['symbol']}: WR={r['wr']}%, 月均={r['monthly_return']}%, PF={r['pf']}")
    print("=" * 60)


def generate_report(results, skip_list):
    """生成Markdown报告"""
    verified = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'LINKUSDT', 'POLUSDT']
    new_symbols = [r for r in results if r['symbol'] not in verified]
    excellent = [r for r in results if r['wr'] >= 60 and r['monthly_return'] >= 1.5 and r['pf'] >= 1.3]
    qualified = [r for r in results if r['wr'] >= 55 and r['monthly_return'] > 0]
    new_top5 = sorted([r for r in new_symbols], key=lambda x: x['wr'], reverse=True)[:5]

    lines = [
        "# 白夜系统 - 扩展20品种深度回测报告",
        "",
        f"> 生成时间: {pd.Timestamp.now(tz='UTC+8').strftime('%Y-%m-%d %H:%M')} CST",
        f"> 测试参数: 90天 15m K线 | 资金: $150 | 风险: 2%/笔",
        f"> 引擎版本: backtest_engine_v2 (Wilder's ATR/ADX)",
        "",
        "---",
        "",
        "## 1. 总排行榜（按WR降序）",
        "",
        "| 排名 | 品种 | 类型 | 笔数 | WR% | 月均% | 最大回撤% | PF | sc | lc | ccp | adx_th | 状态 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    for i, r in enumerate(results, 1):
        lines.append(
            f"| {i} | {r['symbol']} | {r['type']} | {r['trades']} | "
            f"{r['wr']} | {r['monthly_return']} | {r['max_dd']} | {r['pf']} | "
            f"{r['sc']} | {r['lc']} | {r['ccp']} | {r['adx_th']} | {r['status']} |"
        )

    lines += [
        "",
        "---",
        "",
        "## 2. 新品种TOP5详细分析",
        "",
    ]

    for i, r in enumerate(new_top5, 1):
        grade = "🏆 强力推荐" if r['wr'] >= 62 and r['monthly_return'] >= 2.0 else \
                "✅ 推荐加入" if r['wr'] >= 58 and r['monthly_return'] >= 1.0 else \
                "⚠ 观察期" if r['wr'] >= 55 else "❌ 暂不推荐"
        lines += [
            f"### {i}. {r['symbol']} — {grade}",
            "",
            f"| 指标 | 数值 |",
            f"| --- | --- |",
            f"| 回测笔数 | {r['trades']} |",
            f"| 胜率 | {r['wr']}% |",
            f"| 月均收益 | {r['monthly_return']}% |",
            f"| 最大回撤 | {r['max_dd']}% |",
            f"| 盈亏比 | {r['pf']} |",
            f"| 最终权益 | ${r['final_equity']} |",
            f"| 最优参数 | sc={r['sc']}, lc={r['lc']}, ccp={r['ccp']}, adx_th={r['adx_th']} |",
            "",
        ]

    lines += [
        "---",
        "",
        "## 3. 全量数据汇总表格",
        "",
        "| 品种 | 笔数 | WR% | 月均% | 最大回撤% | PF | 最优参数 | 状态 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    for r in results:
        params = f"sc={r['sc']},lc={r['lc']},ccp={r['ccp']},adx={r['adx_th']}"
        lines.append(
            f"| {r['symbol']} | {r['trades']} | {r['wr']} | {r['monthly_return']} | "
            f"{r['max_dd']} | {r['pf']} | {params} | {r['status']} |"
        )

    if skip_list:
        lines += ["", f"**跳过品种** (数据不足): {', '.join(skip_list)}"]

    lines += [
        "",
        "---",
        "",
        "## 4. 结论：哪些品种可以加入白夜系统",
        "",
    ]

    can_add = [r for r in results if r['wr'] >= 58 and r['monthly_return'] >= 1.0 and r['pf'] >= 1.2]
    watch = [r for r in results if r['symbol'] not in [x['symbol'] for x in can_add] and r['wr'] >= 55 and r['monthly_return'] > 0]
    no_add = [r for r in results if r not in can_add and r not in watch]

    lines.append("### ✅ 立即可加入（WR≥58% + 月均≥1% + PF≥1.2）")
    lines.append("")
    if can_add:
        for r in can_add:
            lines.append(f"- **{r['symbol']}**: WR={r['wr']}%, 月均={r['monthly_return']}%, PF={r['pf']}, 最优: sc={r['sc']},lc={r['lc']},ccp={r['ccp']},adx={r['adx_th']}")
    else:
        lines.append("- 暂无品种满足所有条件")

    lines += ["", "### ⚠ 观察期（WR≥55% + 月均>0%）", ""]
    if watch:
        for r in watch:
            lines.append(f"- **{r['symbol']}**: WR={r['wr']}%, 月均={r['monthly_return']}%, PF={r['pf']}")
    else:
        lines.append("- 无")

    lines += ["", "### ❌ 暂不推荐（不满足基础条件）", ""]
    if no_add:
        for r in no_add:
            lines.append(f"- **{r['symbol']}**: WR={r['wr']}%, 月均={r['monthly_return']}%, PF={r['pf']}")
    else:
        lines.append("- 无")

    lines += [
        "",
        "---",
        "",
        "## 5. 系统扩展建议",
        "",
        f"- **总测试品种**: {len(results) + len(skip_list)} 个",
        f"- **完成回测**: {len(results)} 个",
        f"- **跳过**: {len(skip_list)} 个",
        f"- **可立即加入**: {len(can_add)} 个",
        f"- **观察期**: {len(watch)} 个",
        "",
        "**操作建议**:",
        "1. 立即将优质品种加入白夜系统多品种组合，提升信号频率",
        "2. 观察期品种建议在纸交易中运行2-4周后再决策",
        "3. 所有新品种使用最优参数，避免用通用默认参数",
        "4. 建议每月重新运行此测试，动态调整品种池",
        "",
        "---",
        "_由白夜交易系统自动生成 | backtest_engine_v2_",
    ]

    report_path = '/root/.openclaw/workspace/killer-trading-system/research/expand_20symbols_report.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f"\n✅ 报告已生成: {report_path}")


if __name__ == '__main__':
    main()
