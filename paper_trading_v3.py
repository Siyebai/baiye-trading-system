#!/usr/bin/env python3
"""
白夜纸交易引擎 v3.0 — 融合策略（均值回归 + DI/MACD/RSI 二次过滤）
- 手续费按名义仓位计算（正确模型）
- 模拟 Limit 单入场（Maker 费 0.02%）
- 15m 主周期
- 6品种（ETH暂停，BTC/SOL/BNB/LINK/POL）
"""
import json, time, requests, numpy as np, pandas as pd, ta, warnings
from pathlib import Path
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
import logging

warnings.filterwarnings("ignore")

# ── 配置 ──────────────────────────────────────────────────────────────
CAPITAL       = 150.0
RISK_PCT      = 0.02      # 单笔风险 2%
FEE_MAKER     = 0.0002    # Limit单 Maker 费 0.02%
MAX_POSITIONS = 3         # 最多同时持仓
INTERVAL      = "15m"
POLL_SECS     = 60        # 每分钟扫一次（15m策略，每轮扫完即可）
LOG_FILE      = Path("logs/paper_v3.log")
STATE_FILE    = Path("logs/paper_v3_state.json")
TRADES_FILE   = Path("paper_trades_v3.json")

LOG_FILE.parent.mkdir(exist_ok=True)

# ── 日志轮转配置（防止日志无限增长）────────────────────────────────
_log_handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=2, encoding='utf-8')
_log_handler.setFormatter(logging.Formatter('%(message)s'))
_logger = logging.getLogger('paper_v3')
_logger.addHandler(_log_handler)
_logger.setLevel(logging.INFO)

# ── 融合策略参数（来自 config/fusion_strategy_params.json 验证版）──────
FUSION_CONFIGS = {
    "BTCUSDT":  dict(sc=4, lc=5, ccp=0.002,  adx_th=22, filt="di_confirm",   tp_s=1.0, tp_l=1.5),
    "SOLUSDT":  dict(sc=5, lc=4, ccp=0.0015, adx_th=25, filt="rsi_confirm",  tp_s=1.0, tp_l=1.0),
    "BNBUSDT":  dict(sc=5, lc=6, ccp=0.0015, adx_th=15, filt="macd_confirm", tp_s=2.0, tp_l=2.0),
    "LINKUSDT": dict(sc=7, lc=4, ccp=0.0025, adx_th=15, filt="rsi_confirm",  tp_s=2.0, tp_l=1.5),
    "POLUSDT":  dict(sc=5, lc=4, ccp=0.0015, adx_th=25, filt="di_confirm",   tp_s=1.0, tp_l=1.0),
}
SYMBOLS = list(FUSION_CONFIGS.keys())

# ── Binance REST ───────────────────────────────────────────────────────
def fetch_klines(sym, interval="15m", limit=300):
    url = "https://api.binance.com/api/v3/klines"
    r = requests.get(url, params={"symbol":sym,"interval":interval,"limit":limit}, timeout=15)
    raw = r.json()
    df = pd.DataFrame(raw, columns=[
        "ts","open","high","low","close","volume",
        "ct","qv","trades","tbb","tbq","ignore"])
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    return df.set_index("ts").sort_index()

# ── 指标计算 ────────────────────────────────────────────────────────────
def add_ind(df):
    c=df["close"]; h=df["high"]; l=df["low"]
    df["ret"]=c.pct_change()
    df["atr14"]=ta.volatility.AverageTrueRange(h,l,c,14).average_true_range()
    df["ema200"]=ta.trend.EMAIndicator(c,200).ema_indicator()
    adx_i=ta.trend.ADXIndicator(h,l,c,14)
    df["adx"]=adx_i.adx(); df["dip"]=adx_i.adx_pos(); df["din"]=adx_i.adx_neg()
    macd=ta.trend.MACD(c,13,5,5); df["mh"]=macd.macd_diff()
    df["rsi"]=ta.momentum.RSIIndicator(c,14).rsi()
    return df.dropna()

# ── 信号生成 ────────────────────────────────────────────────────────────
def gen_signal(df, cfg):
    """返回最新K线的信号: 1=LONG, -1=SHORT, 0=无"""
    r=df["ret"]; ris=(r>0); fal=(r<0)
    # 连续计数
    def consec(s):
        out=np.zeros(len(s),dtype=int); cnt=0
        for i,v in enumerate(s.values):
            cnt=cnt+1 if v else 0; out[i]=cnt
        return out
    # 累计涨跌幅
    def cumchg(ret,mask):
        out=np.zeros(len(ret)); cc=0.0
        for i in range(len(ret)):
            if mask.iloc[i]: cc+=ret.iloc[i]
            else: cc=0.0
            out[i]=cc
        return out
    cu=consec(ris); cd=consec(fal)
    cum_up=cumchg(r,ris); cum_dn=cumchg(r,fal)

    sc=cfg["sc"]; lc=cfg["lc"]; ccp=cfg["ccp"]; adx_th=cfg["adx_th"]
    filt=cfg["filt"]
    last=len(df)-1   # 最后一根（正在形成的K线不计，用倒数第2根）
    idx = last - 1   # 已完成的最后一根

    adx_ok=(df["adx"].iloc[idx]>=adx_th)
    ema200_ok=(df["close"].iloc[idx]>df["ema200"].iloc[idx])

    # 基础信号
    base_short=(cu[idx]>=sc)and(cum_up[idx]>=ccp)and adx_ok
    base_long =(cd[idx]>=lc)and(cum_dn[idx]<=-ccp)and adx_ok and ema200_ok

    # 二次过滤
    if filt=="di_confirm":
        filter_short=df["din"].iloc[idx]>df["dip"].iloc[idx]
        filter_long =df["dip"].iloc[idx]>df["din"].iloc[idx]
    elif filt=="macd_confirm":
        filter_short=df["mh"].iloc[idx]>df["mh"].iloc[idx-1]
        filter_long =df["mh"].iloc[idx]<df["mh"].iloc[idx-1]
    elif filt=="rsi_confirm":
        filter_short=df["rsi"].iloc[idx]>55
        filter_long =df["rsi"].iloc[idx]<45
    else:
        filter_short=filter_long=True

    if base_short and filter_short: return -1
    if base_long  and filter_long:  return  1
    return 0

# ── 仓位管理 ────────────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"equity": CAPITAL, "positions": {}, "trades": [], "scan_count": 0}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    _logger.info(line)  # 使用RotatingFileHandler自动轮转（5MB×3）

def check_exits(state, prices):
    """检查持仓是否触及TP/SL（用当前价格近似）"""
    to_close = []
    for sym, pos in list(state["positions"].items()):
        price = prices.get(sym)
        if not price: continue
        direction = pos["direction"]
        tp = pos["tp"]; sl = pos["sl"]
        if direction==1:
            if price>=tp:
                to_close.append((sym,"TP",tp))
            elif price<=sl:
                to_close.append((sym,"SL",sl))
        else:
            if price<=tp:
                to_close.append((sym,"TP",tp))
            elif price>=sl:
                to_close.append((sym,"SL",sl))
    return to_close

# ── 主循环 ──────────────────────────────────────────────────────────────
def main():
    log("🚀 白夜纸交易 v3.0 启动")
    log(f"   策略: 均值回归 + 融合过滤 | 周期: 15m | Maker 0.02%")
    log(f"   品种: {', '.join(SYMBOLS)}")

    state = load_state()
    log(f"   初始资金: {state['equity']:.2f}U | 历史交易: {len(state['trades'])}笔")

    round_num = 0
    while True:
        round_num += 1
        state["scan_count"] = round_num
        prices = {}

        # 1. 获取最新价格（用于检查退出）
        for sym in SYMBOLS:
            try:
                r = requests.get("https://api.binance.com/api/v3/ticker/price",
                                  params={"symbol":sym}, timeout=5)
                prices[sym] = float(r.json()["price"])
            except: pass

        # 2. 检查持仓退出
        exits = check_exits(state, prices)
        for sym, reason, exit_price in exits:
            pos = state["positions"].pop(sym)
            risk_amt = pos["risk_amt"]
            sl_pct = pos["sl_dist_pct"]
            notional = risk_amt / sl_pct
            fee_cost = notional * FEE_MAKER * 2
            direction = pos["direction"]
            tp_m = pos["tp_mult"]

            if reason=="TP":
                pnl = risk_amt * tp_m - fee_cost
            else:
                pnl = -risk_amt - fee_cost

            state["equity"] += pnl
            trade = {
                "sym": sym, "direction": "LONG" if direction==1 else "SHORT",
                "entry": pos["entry"], "exit": exit_price, "reason": reason,
                "pnl": round(pnl,4), "equity": round(state["equity"],4),
                "open_ts": pos["open_ts"],
                "close_ts": datetime.now(timezone.utc).isoformat(),
                "fee": round(fee_cost,4), "notional": round(notional,2)
            }
            state["trades"].append(trade)
            # 限制trades列表最多500条（防止内存无限增长）
            if len(state["trades"]) > 500:
                state["trades"] = state["trades"][-500:]
            TRADES_FILE.write_text(json.dumps(state["trades"], indent=2, default=str))
            emoji = "✅" if reason=="TP" else "❌"
            log(f"  {emoji} {sym} {trade['direction']} [{reason}] PnL={pnl:+.3f}U 名义={notional:.0f}U 费={fee_cost:.3f}U | 总资金={state['equity']:.2f}U")

        # 3. 扫描新信号（仓位未满时）
        open_count = len(state["positions"])
        if open_count < MAX_POSITIONS:
            for sym in SYMBOLS:
                if sym in state["positions"]: continue
                if len(state["positions"]) >= MAX_POSITIONS: break
                try:
                    df = fetch_klines(sym, INTERVAL, 300)
                    df = add_ind(df)
                    sig = gen_signal(df, FUSION_CONFIGS[sym])
                except Exception as e:
                    log(f"  ⚠️ {sym} 数据获取失败: {e}")
                    continue

                if sig == 0: continue

                cfg = FUSION_CONFIGS[sym]
                price = prices.get(sym, df["close"].iloc[-1])
                atr = df["atr14"].iloc[-2]
                direction = sig
                sl_m = 1.0
                tp_m = cfg["tp_l"] if sig==1 else cfg["tp_s"]

                sl_dist = atr * sl_m
                sl_pct = sl_dist / price if price>0 else 0.005
                tp = price + atr*tp_m*direction
                sl = price - atr*sl_m*direction

                risk_amt = state["equity"] * RISK_PCT
                notional = risk_amt / sl_pct
                fee_cost = notional * FEE_MAKER * 2

                side = "LONG" if sig==1 else "SHORT"
                filt = cfg["filt"]
                log(f"  📍 {sym} {side} | 入场≈{price:.4f} TP={tp:.4f} SL={sl:.4f} | "
                    f"过滤:{filt} ATR={atr:.4f} 名义={notional:.0f}U 费={fee_cost:.3f}U")

                state["positions"][sym] = {
                    "direction": direction, "entry": price,
                    "tp": tp, "sl": sl, "tp_mult": tp_m,
                    "risk_amt": risk_amt, "sl_dist_pct": sl_pct,
                    "open_ts": datetime.now(timezone.utc).isoformat(),
                    "filter": filt
                }

        # 4. 状态摘要
        wins  = sum(1 for t in state["trades"] if t["reason"]=="TP")
        total = len(state["trades"])
        wr    = wins/total*100 if total>0 else 0
        total_pnl = state["equity"] - CAPITAL
        log(f"  📊 轮{round_num} | 持仓:{len(state['positions'])} | "
            f"交易:{total}笔 WR:{wr:.1f}% | "
            f"总PnL:{total_pnl:+.2f}U | 资金:{state['equity']:.2f}U")

        save_state(state)
        time.sleep(POLL_SECS)

if __name__ == "__main__":
    main()
