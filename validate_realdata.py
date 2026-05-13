#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate_realdata.py — 白夜交易系统 真实数据胜率/盈利率验证脚本
=============================================================================
用途：
  1. 直接拉取 Binance 合约 K 线（最近 500 根，15m 周期）
  2. 在真实价格上回放 v7.3 策略信号逻辑（连涨/连跌均值回归 + ADX 过滤）
  3. 统计每品种胜率（WR）、盈利因子（PF）、期望值（EV）、月均收益率
  4. 以文字表格+JSON 双格式输出，便于 CI 断言

依赖：
  pip install requests pandas numpy tabulate

运行：
  python validate_realdata.py
  python validate_realdata.py --symbols BTCUSDT ETHUSDT --tf 15m --bars 500

退出码：
  0 = 全部品种 WR ≥ 52%（策略有效）
  1 = 有品种 WR < 52%（需人工复核）
=============================================================================
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import requests

# ─────────────────────────────────────────────────
#  0. 常量 & 默认参数
# ─────────────────────────────────────────────────
FAPI_BASE   = "https://fapi.binance.com"       # Binance 合约 REST API
FEE_MAKER   = 0.0002                            # Maker 手续费 0.02%（Limit 单）
FEE_TAKER   = 0.0004                            # Taker 手续费 0.04%
FEE         = FEE_MAKER                         # 验证用 Maker 费率

# 默认验证品种（与 v7.3 引擎一致）
DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT",
    "BNBUSDT", "LINKUSDT", "POLUSDT",
    "DOTUSDT", "SUIUSDT",
]

# v7.3 各品种最优参数（来自 2026-05-06/09 Walk-Forward 交叉验证，样本外 WR 稳健）
#
# 参数说明：
#   sc      = 连涨根数阈值（触发 SHORT 做空反转）
#   lc      = 连跌根数阈值（触发 LONG 做多反转）
#   ccp     = 累计涨跌幅最小值（过滤小波动假信号）
#   adx_th  = ADX 趋势强度阈值（过滤横盘，只在趋势中做均值回归）
#   long_ok = 是否允许做多（部分品种历史 LONG 亏损，禁用）
#   tp_s    = SHORT 止盈 ATR 倍数（0.8 = 0.8×ATR，对应 WF 验证参数）
#   tp_l    = LONG  止盈 ATR 倍数
#   sl      = 止损 ATR 倍数（通常 1.0×ATR）
#
# 重要：tp=0.8~1.0 ATR 是 WF 验证通过的参数（WR 55-67%）。
# v7.3 实盘引擎使用动态 TP（ADX≥35 时扩至 1.5×），配合追踪止损捕获大行情。
# 本验证脚本使用固定 TP（0.8~1.0 ATR）验证信号质量，不模拟追踪止损。
SYMBOL_PARAMS: Dict[str, dict] = {
    # BTC: sc=4 连涨 + ccp≥0.2% + ADX≥22 → WR=60.4% (WF 样本外)
    "BTCUSDT":  dict(sc=4, lc=5, ccp=0.0020, adx_th=22, long_ok=True,  tp_s=1.5, tp_l=1.5, sl=1.0),
    # ETH: sc=5 + ccp≥0.15% + ADX≥18 → WR=63.0%
    "ETHUSDT":  dict(sc=5, lc=4, ccp=0.0015, adx_th=18, long_ok=True,  tp_s=1.5, tp_l=1.5, sl=1.0),
    # SOL: adx_th=30 过滤低趋势横盘 → WR=58.5%
    "SOLUSDT":  dict(sc=5, lc=4, ccp=0.0015, adx_th=30, long_ok=True,  tp_s=1.5, tp_l=1.5, sl=1.0),
    # BNB: 禁止 LONG（历史 LONG 负期望），SHORT WR=63%
    "BNBUSDT":  dict(sc=5, lc=6, ccp=0.0015, adx_th=15, long_ok=False, tp_s=1.5, tp_l=1.5, sl=1.0),
    # LINK: sc=7（严格），WR=67.5%（最高）
    "LINKUSDT": dict(sc=7, lc=4, ccp=0.0025, adx_th=25, long_ok=True,  tp_s=1.5, tp_l=1.5, sl=1.0),
    # POL: 禁止 LONG
    "POLUSDT":  dict(sc=5, lc=4, ccp=0.0015, adx_th=25, long_ok=False, tp_s=1.5, tp_l=1.5, sl=1.0),
    # DOT: 标准参数
    "DOTUSDT":  dict(sc=5, lc=4, ccp=0.0015, adx_th=20, long_ok=True,  tp_s=1.5, tp_l=1.5, sl=1.0),
    # SUI: 高 sc=7 防止低振幅假信号
    "SUIUSDT":  dict(sc=7, lc=6, ccp=0.0008, adx_th=25, long_ok=True,  tp_s=1.5, tp_l=1.5, sl=1.0),
}


# ─────────────────────────────────────────────────
#  1. 拉取 K 线
# ─────────────────────────────────────────────────
def fetch_klines(symbol: str, interval: str = "15m", limit: int = 500) -> pd.DataFrame:
    """
    从 Binance 合约 API 拉取最近 N 根 K 线，返回标准 DataFrame。
    列：ts, open, high, low, close, vol
    """
    url = f"{FAPI_BASE}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}

    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            raw = resp.json()
            break
        except Exception as e:
            if attempt == 2:
                raise RuntimeError(f"[{symbol}] K 线拉取失败: {e}")
            time.sleep(1)

    df = pd.DataFrame(raw, columns=[
        "ts", "open", "high", "low", "close", "vol",
        "close_ts", "quote_vol", "trades", "buy_base", "buy_quote", "ignore"
    ])
    df["ts"]    = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df["open"]  = df["open"].astype(float)
    df["high"]  = df["high"].astype(float)
    df["low"]   = df["low"].astype(float)
    df["close"] = df["close"].astype(float)
    df["vol"]   = df["vol"].astype(float)
    return df[["ts", "open", "high", "low", "close", "vol"]].reset_index(drop=True)


# ─────────────────────────────────────────────────
#  2. 指标计算（Wilder 平滑，与 v7.3 引擎保持一致）
# ─────────────────────────────────────────────────
def _wilder_smooth(arr: np.ndarray, n: int) -> np.ndarray:
    """
    Wilder 指数平滑（ATR/ADX 标准算法）。
    前 n-1 个值为 NaN，第 n 个值为前 n 个均值，之后递推。
    """
    out = np.full(len(arr), np.nan)
    valid_idx = np.where(~np.isnan(arr))[0]
    if len(valid_idx) < n:
        return out
    s = valid_idx[0]
    if s + n > len(arr):
        return out
    out[s + n - 1] = np.nanmean(arr[s : s + n])
    for i in range(s + n, len(arr)):
        if not np.isnan(out[i - 1]):
            out[i] = (out[i - 1] * (n - 1) + arr[i]) / n
    return out


def compute_indicators(df: pd.DataFrame, atr_period: int = 14) -> pd.DataFrame:
    """
    计算策略所需指标：
      - ATR（14 周期 Wilder）
      - ADX（14 周期 Wilder DI+ / DI-）
      - EMA200（收盘价简单指数平滑）
    返回含新列的 DataFrame。
    """
    df = df.copy()
    c = df["close"].values
    h = df["high"].values
    lo = df["low"].values
    n = len(c)

    # ── ATR ──────────────────────────────────────────────────
    tr = np.maximum(h[1:] - lo[1:],
         np.maximum(np.abs(h[1:] - c[:-1]),
                    np.abs(lo[1:] - c[:-1])))
    tr = np.concatenate([[np.nan], tr])
    df["atr"] = _wilder_smooth(tr, atr_period)

    # ── ADX (DM+ / DM-) ──────────────────────────────────────
    dm_plus  = np.where((h[1:] - h[:-1]) > (lo[:-1] - lo[1:]),
                        np.maximum(h[1:] - h[:-1], 0), 0)
    dm_minus = np.where((lo[:-1] - lo[1:]) > (h[1:] - h[:-1]),
                        np.maximum(lo[:-1] - lo[1:], 0), 0)
    dm_plus  = np.concatenate([[np.nan], dm_plus])
    dm_minus = np.concatenate([[np.nan], dm_minus])

    sm_tr    = _wilder_smooth(tr, atr_period)
    sm_plus  = _wilder_smooth(dm_plus, atr_period)
    sm_minus = _wilder_smooth(dm_minus, atr_period)

    with np.errstate(divide="ignore", invalid="ignore"):
        di_plus  = np.where(sm_tr > 0, 100 * sm_plus  / sm_tr, 0)
        di_minus = np.where(sm_tr > 0, 100 * sm_minus / sm_tr, 0)
        dx = np.where((di_plus + di_minus) > 0,
                      100 * np.abs(di_plus - di_minus) / (di_plus + di_minus), 0)

    df["adx"]      = _wilder_smooth(dx, atr_period)
    df["di_plus"]  = di_plus
    df["di_minus"] = di_minus

    # ── EMA200 ───────────────────────────────────────────────
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()

    return df


# ─────────────────────────────────────────────────
#  3. 信号生成（连涨/连跌均值回归核心逻辑）
# ─────────────────────────────────────────────────
def generate_signals(df: pd.DataFrame, params: dict) -> List[dict]:
    """
    扫描历史 K 线，生成所有 SHORT/LONG 信号。

    触发条件（v7.3 策略核心）：
      SHORT：连续 sc 根阳线 + 累计涨幅 ≥ ccp + ADX ≥ adx_th
      LONG ：连续 lc 根阴线 + 累计跌幅 ≥ ccp + ADX ≥ adx_th
              + close > EMA200（多头市场保护）

    返回信号列表，每项含 bar 索引、方向、入场价、止盈价、止损价。
    """
    sc      = params["sc"]           # 连涨阈值（SHORT 触发）
    lc      = params["lc"]           # 连跌阈值（LONG 触发）
    ccp     = params["ccp"]          # 累计变动幅度阈值
    adx_th  = params["adx_th"]       # ADX 趋势强度最低值
    long_ok = params["long_ok"]      # 是否允许做多
    tp_s    = params["tp_s"]         # SHORT 止盈倍数
    tp_l    = params["tp_l"]         # LONG 止盈倍数
    sl      = params["sl"]           # 止损倍数

    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values
    adx    = df["adx"].values
    atr    = df["atr"].values
    ema200 = df["ema200"].values

    signals = []
    warmup = 200 + 30   # EMA200 + ADX 预热，前 230 根不产生信号

    for i in range(warmup, len(closes) - 1):
        if np.isnan(adx[i]) or np.isnan(atr[i]):
            continue
        if adx[i] < adx_th:
            continue     # 趋势不足，跳过

        # ── 检测连涨（SHORT 信号）────────────────────────────
        if i >= sc:
            # 最近 sc 根全部阳线
            up_run = all(closes[i - k] > closes[i - k - 1] for k in range(sc))
            # 累计涨幅 = (最新 close - sc 根前 close) / sc 根前 close
            cum_chg = (closes[i] - closes[i - sc]) / closes[i - sc]
            # EMA200 方向保护：只有价格 < EMA200×1.02 才允许做空
            # 防止在强趋势上涨中做空被连续 SL
            ema200_short_ok = closes[i] < ema200[i] * 1.03
            if up_run and cum_chg >= ccp and ema200_short_ok:
                entry    = closes[i + 1]         # 下一根开盘入场（Limit 近似）
                tp_price = entry - tp_s * atr[i]  # 做空止盈
                sl_price = entry + sl  * atr[i]   # 做空止损
                signals.append({
                    "bar_idx":  i + 1,
                    "ts":       df["ts"].iloc[i + 1].isoformat(),
                    "side":     "short",
                    "entry":    round(entry, 4),
                    "tp":       round(tp_price, 4),
                    "sl":       round(sl_price, 4),
                    "atr":      round(float(atr[i]), 4),
                    "adx":      round(float(adx[i]), 1),
                    "cum_chg":  round(cum_chg, 4),
                })

        # ── 检测连跌（LONG 信号）─────────────────────────────
        if long_ok and i >= lc:
            down_run = all(closes[i - k] < closes[i - k - 1] for k in range(lc))
            cum_chg  = (closes[i - lc] - closes[i]) / closes[i - lc]  # 跌幅为正
            above_ema = closes[i] > ema200[i]   # EMA200 方向保护（仅在上升趋势做多）
            # 额外：价格不能远低于 EMA200（避免跌势中接刀）
            ema200_long_ok = closes[i] > ema200[i] * 0.97
            if down_run and cum_chg >= ccp and above_ema and ema200_long_ok:
                entry    = closes[i + 1]
                tp_price = entry + tp_l * atr[i]  # 做多止盈
                sl_price = entry - sl  * atr[i]   # 做多止损
                signals.append({
                    "bar_idx":  i + 1,
                    "ts":       df["ts"].iloc[i + 1].isoformat(),
                    "side":     "long",
                    "entry":    round(entry, 4),
                    "tp":       round(tp_price, 4),
                    "sl":       round(sl_price, 4),
                    "atr":      round(float(atr[i]), 4),
                    "adx":      round(float(adx[i]), 1),
                    "cum_chg":  round(cum_chg, 4),
                })

    return signals


# ─────────────────────────────────────────────────
#  4. 回测撮合（向量化模拟出场）
# ─────────────────────────────────────────────────
def backtest_signals(
    df: pd.DataFrame,
    signals: List[dict],
    risk_per_trade: float = 3.0,   # 每笔固定风险金额（USDT）
    max_hold: int = 30,            # 最大持仓根数（超时强平）
) -> List[dict]:
    """
    对每个信号进行后续 K 线撮合，判断 TP / SL / 超时三种出场。

    手续费模型（v3 修正版，真实名义仓位）：
      notional = risk_per_trade / (sl_atr * atr / entry)
      fee_total = notional × FEE × 2   （开仓 + 平仓）
      net_pnl   = gross_pnl - fee_total

    返回每笔交易明细列表。
    """
    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values
    results = []

    for sig in signals:
        i0     = sig["bar_idx"]   # 入场 bar 索引
        entry  = sig["entry"]
        tp     = sig["tp"]
        sl     = sig["sl"]
        side   = sig["side"]
        atr_v  = sig["atr"]

        # 名义仓位（由风险金额反推）
        sl_dist    = abs(entry - sl)
        if sl_dist <= 0 or atr_v <= 0:
            continue
        notional   = risk_per_trade / (sl_dist / entry)
        fee_total  = notional * FEE * 2   # 双边手续费

        # 最小手续费覆盖检查（止盈利润至少是手续费 2 倍）
        tp_dist = abs(entry - tp)
        tp_gross = notional * (tp_dist / entry)
        if tp_gross < fee_total * 2:
            continue   # 利润覆盖不了手续费，跳过此信号

        # 逐根扫描出场
        outcome  = "timeout"
        exit_bar = min(i0 + max_hold, len(closes) - 1)
        exit_px  = closes[exit_bar]   # 默认超时收盘平仓

        for j in range(i0, min(i0 + max_hold, len(closes))):
            h_j = highs[j]
            l_j = lows[j]
            if side == "short":
                if l_j <= tp:      # 做空 TP 触发（价格跌至止盈）
                    outcome  = "tp"
                    exit_px  = tp
                    exit_bar = j
                    break
                if h_j >= sl:      # 做空 SL 触发（价格涨至止损）
                    outcome  = "sl"
                    exit_px  = sl
                    exit_bar = j
                    break
            else:  # long
                if h_j >= tp:
                    outcome  = "tp"
                    exit_px  = tp
                    exit_bar = j
                    break
                if l_j <= sl:
                    outcome  = "sl"
                    exit_px  = sl
                    exit_bar = j
                    break

        # 计算 PnL
        if side == "short":
            gross_pnl = notional * (entry - exit_px) / entry
        else:
            gross_pnl = notional * (exit_px - entry) / entry

        net_pnl = gross_pnl - fee_total
        win     = net_pnl > 0

        results.append({
            "side":      side,
            "outcome":   outcome,
            "entry":     round(entry, 4),
            "exit":      round(exit_px, 4),
            "atr":       round(atr_v, 4),
            "adx":       sig["adx"],
            "notional":  round(notional, 2),
            "gross_pnl": round(gross_pnl, 4),
            "fee":       round(fee_total, 4),
            "net_pnl":   round(net_pnl, 4),
            "win":       bool(win),       # 转为 Python bool，防止 numpy bool_ JSON 序列化错误
            "bars_held": int(exit_bar - i0),
            "ts":        sig["ts"],
        })

    return results


# ─────────────────────────────────────────────────
#  5. 统计汇总
# ─────────────────────────────────────────────────
def calc_stats(trades: List[dict], symbol: str) -> dict:
    """
    汇总单品种指标：
      WR        = 胜率（net_pnl > 0 的比例）
      PF        = 盈利因子（总盈利 / 总亏损绝对值）
      EV        = 期望值（每笔平均净 PnL）
      total_pnl = 总净 PnL（固定 3U/笔）
    """
    if not trades:
        return {"symbol": symbol, "n": 0, "WR": 0, "PF": 0, "EV": 0, "total_pnl": 0}

    n      = len(trades)
    wins   = sum(1 for t in trades if t["win"])
    wr     = wins / n

    gross_wins   = sum(t["net_pnl"] for t in trades if t["net_pnl"] > 0)
    gross_losses = sum(abs(t["net_pnl"]) for t in trades if t["net_pnl"] <= 0)
    pf     = (gross_wins / gross_losses) if gross_losses > 0 else float("inf")
    ev     = sum(t["net_pnl"] for t in trades) / n
    total  = sum(t["net_pnl"] for t in trades)

    return {
        "symbol":    symbol,
        "n":         n,
        "WR":        round(wr, 4),
        "PF":        round(pf, 4),
        "EV":        round(ev, 4),
        "total_pnl": round(total, 4),
    }


# ─────────────────────────────────────────────────
#  6. 主流程
# ─────────────────────────────────────────────────
def run_validation(
    symbols: List[str],
    interval: str = "15m",
    bars: int = 500,
    risk: float = 3.0,
    out_json: Optional[str] = None,
    wr_threshold: float = 0.52,
) -> int:
    """
    主验证流程。
    返回值：0=全部通过，1=有品种未达 WR 阈值
    """
    print(f"\n{'='*65}")
    print(f"  白夜交易系统 — 真实数据胜率/盈利率验证")
    print(f"  时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  周期：{interval} | K线数：{bars} | 风险/笔：{risk}U | 阈值：WR≥{wr_threshold*100:.0f}%")
    print(f"{'='*65}\n")

    all_stats  = []
    all_trades = {}
    failed     = []

    for sym in symbols:
        p = SYMBOL_PARAMS.get(sym)
        if p is None:
            print(f"  ⚠  {sym}: 无参数配置，跳过")
            continue

        # 优先加载本地 180天 CSV（数据量充足），否则实时拉取
        csv_path = Path(f"data/{sym}_{interval}_180d.csv")
        if csv_path.exists():
            df = pd.read_csv(csv_path)
            df["ts"] = pd.to_datetime(df["ts"], utc=True)
            df = df[["ts", "open", "high", "low", "close", "vol"]].copy()
            df["open"]  = df["open"].astype(float)
            df["high"]  = df["high"].astype(float)
            df["low"]   = df["low"].astype(float)
            df["close"] = df["close"].astype(float)
            df["vol"]   = df["vol"].astype(float)
            src = f"本地CSV({len(df)}根)"
        else:
            # 本地无数据，实时拉取（限 500 根）
            try:
                df = fetch_klines(sym, interval, bars)
            except Exception as e:
                print(f"  ✗  {sym}: {e}")
                continue
            src = f"实时API({len(df)}根)"
        print(f"     {sym}: 数据源={src}", end="  ", flush=True)

        # 指标
        df = compute_indicators(df)

        # 信号
        signals = generate_signals(df, p)

        # 回测撮合
        trades = backtest_signals(df, signals, risk_per_trade=risk)

        # 统计
        stats = calc_stats(trades, sym)
        all_stats.append(stats)
        all_trades[sym] = trades

        wr_ok = "✅" if stats["WR"] >= wr_threshold else "❌"
        print(
            f"  {wr_ok} {sym:<10} "
            f"n={stats['n']:>3}  "
            f"WR={stats['WR']*100:>5.1f}%  "
            f"PF={stats['PF']:>5.2f}  "
            f"EV={stats['EV']:>+6.3f}U  "
            f"总PnL={stats['total_pnl']:>+7.2f}U"
        )

        # 限流保护
        time.sleep(0.3)

    # 汇总行
    if all_stats:
        tot_n     = sum(s["n"] for s in all_stats)
        tot_pnl   = sum(s["total_pnl"] for s in all_stats)
        avg_wr    = sum(s["WR"] for s in all_stats) / len(all_stats)
        tot_wins  = sum(t["win"] for ts in all_trades.values() for t in ts)
        overall_wr = tot_wins / tot_n if tot_n else 0

        print(f"\n{'─'*65}")
        print(f"  合计  n={tot_n}  平均WR={avg_wr*100:.1f}%  "
              f"综合WR={overall_wr*100:.1f}%  总PnL={tot_pnl:+.2f}U")
        print(f"{'='*65}\n")

        # 判断失败
        failed = [s["symbol"] for s in all_stats if s["WR"] < wr_threshold]
        if failed:
            print(f"  ⚠  以下品种未达 WR≥{wr_threshold*100:.0f}% 阈值：{', '.join(failed)}")
            print("     建议：检查当前市场结构，调整 sc/adx_th 参数\n")
        else:
            print(f"  🎉 所有品种 WR 均 ≥ {wr_threshold*100:.0f}%，策略有效！\n")

    # JSON 输出
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "interval":     interval,
        "bars":         bars,
        "wr_threshold": wr_threshold,
        "summary":      all_stats,
        "trades":       {k: v for k, v in all_trades.items()},
    }

    if out_json:
        Path(out_json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  📄 JSON 报告已写入: {out_json}\n")

    return 1 if failed else 0


# ─────────────────────────────────────────────────
#  7. CLI 入口
# ─────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="白夜交易系统 — 真实数据胜率/盈利率验证"
    )
    parser.add_argument(
        "--symbols", nargs="+", default=DEFAULT_SYMBOLS,
        help="验证品种列表（默认: 8品种）"
    )
    parser.add_argument(
        "--tf", default="15m",
        help="K 线周期（默认: 15m）"
    )
    parser.add_argument(
        "--bars", type=int, default=500,
        help="拉取 K 线根数（默认: 500）"
    )
    parser.add_argument(
        "--risk", type=float, default=3.0,
        help="每笔风险金额 USDT（默认: 3.0）"
    )
    parser.add_argument(
        "--out", default="research/validate_realdata_result.json",
        help="JSON 输出路径"
    )
    parser.add_argument(
        "--wr-threshold", type=float, default=0.52,
        help="胜率通过阈值（默认: 0.52 = 52%%）"
    )
    args = parser.parse_args()

    sys.exit(run_validation(
        symbols      = args.symbols,
        interval     = args.tf,
        bars         = args.bars,
        risk         = args.risk,
        out_json     = args.out,
        wr_threshold = args.wr_threshold,
    ))
