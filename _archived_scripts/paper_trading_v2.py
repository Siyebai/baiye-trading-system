"""
白夜系统 纸交易引擎 v2.0
- 集成短线信号：5m MACD(5,13,5) + 15m方向共振 + RSI过滤
- 实时Binance REST API数据（每5分钟轮询）
- 风控：单笔3U，日亏损≤15U，持仓≤3个同时
- 记录每笔交易到 paper_trades_v2.json
"""
import requests, json, os, time, signal, sys
import numpy as np
import pandas as pd
import ta
from datetime import datetime, timezone

# ─── 配置 ───────────────────────────────────────────────
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "LINKUSDT"]
CAPITAL = 150.0
RISK_PER_TRADE = 3.0
FEE = 0.0009
MAX_POSITIONS = 3
DAILY_LOSS_LIMIT = 15.0
POLL_INTERVAL = 60   # 秒（生产用300，测试用60）
TRADES_FILE = "paper_trades_v2.json"
STATE_FILE  = "engine/state/paper_state_v2.json"

# MACD参数（基础版）
MACD_FAST = 5
MACD_SLOW = 13
MACD_SIGN = 5
RSI_LOW_L  = 40   # LONG RSI下限
RSI_HIGH_L = 68   # LONG RSI上限
RSI_LOW_S  = 32   # SHORT RSI下限
RSI_HIGH_S = 60   # SHORT RSI上限
TP_MULT = 1.0
SL_MULT = 1.0
ATR_PERIOD = 7
COOLDOWN_BARS = 5  # 信号冷却K数

# ─── API ────────────────────────────────────────────────
BINANCE_API = "https://api.binance.com/api/v3/klines"

def fetch_klines(symbol, interval, limit=100):
    r = requests.get(BINANCE_API,
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=10)
    r.raise_for_status()
    raw = r.json()
    df = pd.DataFrame(raw, columns=[
        "ts","open","high","low","close","volume",
        "close_time","quote_vol","trades","tb_base","tb_quote","ignore"])
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.set_index("ts").sort_index()

def add_indicators(df):
    c=df["close"]; h=df["high"]; l=df["low"]
    df["rsi14"]     = ta.momentum.RSIIndicator(c, 14).rsi()
    m = ta.trend.MACD(c, MACD_SLOW, MACD_FAST, MACD_SIGN)
    df["macd_hist"] = m.macd_diff()
    df["atr7"]      = ta.volatility.AverageTrueRange(h, l, c, ATR_PERIOD).average_true_range()
    return df.dropna()

# ─── 状态管理 ────────────────────────────────────────────
def load_state():
    os.makedirs("engine/state", exist_ok=True)
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "equity": CAPITAL,
        "positions": {},          # {sym: {dir, entry, sl, tp, atr, ts}}
        "daily_pnl": 0.0,
        "day": "",
        "cooldown": {},           # {sym: last_signal_bar_index}
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
    }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)

def load_trades():
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE) as f:
            try: return json.load(f)
            except: return []
    return []

def save_trades(trades):
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2, default=str)

# ─── 核心逻辑 ────────────────────────────────────────────
def check_signal(sym, state):
    """检查是否有新信号"""
    try:
        df5  = add_indicators(fetch_klines(sym, "5m",  limit=60))
        df15 = add_indicators(fetch_klines(sym, "15m", limit=30))
    except Exception as e:
        print(f"  [{sym}] 数据获取失败: {e}")
        return None

    if len(df5) < 10: return None

    # 最新收盘K线（排除最后一根未收盘）
    last = df5.iloc[-2]
    prev = df5.iloc[-3]
    last15 = df15.iloc[-2]

    macd_curr = last["macd_hist"]
    macd_prev = prev["macd_hist"]
    rsi = last["rsi14"]
    macd15 = last15["macd_hist"]
    atr = last["atr7"]
    close = last["close"]

    # 金叉
    cross_up   = macd_curr > 0 and macd_prev <= 0
    # 死叉
    cross_down = macd_curr < 0 and macd_prev >= 0

    sig = None
    if cross_up and macd15 > 0 and RSI_LOW_L < rsi < RSI_HIGH_L:
        sig = {"dir": "LONG",  "entry": close, "atr": atr,
               "tp": close + atr * TP_MULT, "sl": close - atr * SL_MULT,
               "ts": str(last.name), "rsi": round(rsi,1),
               "macd_hist": round(macd_curr,4), "macd15": round(macd15,4)}
    elif cross_down and macd15 < 0 and RSI_LOW_S < rsi < RSI_HIGH_S:
        sig = {"dir": "SHORT", "entry": close, "atr": atr,
               "tp": close - atr * TP_MULT, "sl": close + atr * SL_MULT,
               "ts": str(last.name), "rsi": round(rsi,1),
               "macd_hist": round(macd_curr,4), "macd15": round(macd15,4)}
    return sig

def check_positions(state, trades):
    """检查已有持仓的TP/SL"""
    closed = []
    for sym, pos in list(state["positions"].items()):
        try:
            df = fetch_klines(sym, "5m", limit=5)
            latest = df.iloc[-1]
            hi = latest["high"]
            lo = latest["low"]
            cur = latest["close"]
        except:
            continue

        result = None
        pnl = 0.0
        if pos["dir"] == "LONG":
            if lo <= pos["sl"]:
                result = "SL"; pnl = -RISK_PER_TRADE * (1 + FEE*2)
            elif hi >= pos["tp"]:
                rw = (pos["tp"]-pos["entry"])/(pos["entry"]-pos["sl"])*RISK_PER_TRADE
                pnl = rw*(1-FEE*2); result = "TP"
        else:
            if hi >= pos["sl"]:
                result = "SL"; pnl = -RISK_PER_TRADE * (1 + FEE*2)
            elif lo <= pos["tp"]:
                rw = (pos["entry"]-pos["tp"])/(pos["sl"]-pos["entry"])*RISK_PER_TRADE
                pnl = rw*(1-FEE*2); result = "TP"

        if result:
            state["equity"] += pnl
            state["daily_pnl"] += pnl
            state["total_trades"] += 1
            if result=="TP": state["wins"] += 1
            else: state["losses"] += 1
            trade_rec = {
                "sym": sym, "dir": pos["dir"], "result": result,
                "entry": pos["entry"], "exit": cur, "pnl": round(pnl,3),
                "equity": round(state["equity"],2),
                "open_ts": pos["ts"], "close_ts": str(datetime.now(timezone.utc)),
                "atr": pos["atr"]
            }
            trades.append(trade_rec)
            closed.append(sym)
            wr = state["wins"]/(state["total_trades"]) * 100
            print(f"  [{sym}] {pos['dir']} {result} PnL={pnl:+.2f}U | "
                  f"总权益={state['equity']:.2f}U WR={wr:.1f}% ({state['total_trades']}笔)")

    for sym in closed:
        del state["positions"][sym]
    return len(closed) > 0

def print_status(state):
    total = state["total_trades"]
    wr = state["wins"]/total*100 if total>0 else 0
    pos_list = ", ".join([f"{s}({p['dir'][:1]})" for s,p in state["positions"].items()]) or "无"
    print(f"\n── {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} ──")
    print(f"  权益: {state['equity']:.2f}U  日PnL: {state['daily_pnl']:+.2f}U")
    print(f"  总交易: {total}笔  WR: {wr:.1f}%  当前持仓: {pos_list}")

# ─── 主循环 ─────────────────────────────────────────────
def main():
    state  = load_state()
    trades = load_trades()

    # 重置日PnL
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("day") != today:
        state["daily_pnl"] = 0.0
        state["day"] = today

    def handle_exit(sig, frame):
        print("\n停止纸交易，保存状态...")
        save_state(state)
        save_trades(trades)
        sys.exit(0)
    signal.signal(signal.SIGTERM, handle_exit)
    signal.signal(signal.SIGINT, handle_exit)

    print(f"🚀 白夜纸交易 v2.0 启动")
    print(f"   策略: 5m MACD({MACD_FAST},{MACD_SLOW},{MACD_SIGN}) + 15m共振")
    print(f"   品种: {', '.join(SYMBOLS)}")
    print(f"   资金: {state['equity']:.2f}U | 已有交易: {state['total_trades']}笔")
    print(f"   轮询间隔: {POLL_INTERVAL}秒\n")

    loop = 0
    while True:
        loop += 1
        try:
            # 1. 检查持仓TP/SL
            if state["positions"]:
                check_positions(state, trades)
                save_trades(trades)

            # 2. 重置日PnL
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if state.get("day") != today:
                state["daily_pnl"] = 0.0
                state["day"] = today

            # 3. 日亏损熔断
            if state["daily_pnl"] <= -DAILY_LOSS_LIMIT:
                print(f"  ⚠️ 日亏损熔断 ({state['daily_pnl']:.2f}U)，今日停止开仓")
            elif len(state["positions"]) < MAX_POSITIONS:
                # 4. 扫描新信号
                for sym in SYMBOLS:
                    if sym in state["positions"]: continue
                    sig = check_signal(sym, state)
                    if sig:
                        state["positions"][sym] = sig
                        print(f"  ✅ [{sym}] 开仓 {sig['dir']} @ {sig['entry']:.4f} "
                              f"TP={sig['tp']:.4f} SL={sig['sl']:.4f} "
                              f"ATR={sig['atr']:.4f} RSI={sig['rsi']}")
                    time.sleep(0.3)

            # 5. 状态保存 & 打印
            save_state(state)
            if loop % 5 == 0:
                print_status(state)

        except Exception as e:
            print(f"  [LOOP ERROR] {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
