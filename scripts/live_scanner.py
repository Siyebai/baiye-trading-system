"""
Phase 2: 实盘引擎核心
- 实时从Binance API拉15m K线
- 同时运行SHORT(n_sc连涨+EMA200)+LONG(n_lc连跌+EMA200)
- 信号互斥 + 风控 + 日志
- 纸交易模式（不下单，只记录信号）

Fix记录 v2.2:
  Bug#4: FEE=0.0018 (双边) -> FEE=0.0009 (单边) 与回测一致
  Bug#5: 硬编码参数 -> 从optimal_params_v21.json读取各品种最优参数
  Bug#8: SHORT WIN PnL公式错误 (tp-entry)/entry -> (entry-tp)/entry
  Bug#9: BNBUSDT/POLUSDT无long_disabled逻辑 -> 读取config中long_disabled
"""
import json, time, os, sys
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone
import urllib.request
import urllib.parse

LOG_FILE = Path("/root/.openclaw/workspace/killer-trading-system/logs/live_signal.log")
STATE_FILE = Path("/root/.openclaw/workspace/killer-trading-system/logs/live_state.json")
CONFIG_FILE = Path("/root/.openclaw/workspace/killer-trading-system/config/optimal_params_v21.json")
LOG_FILE.parent.mkdir(exist_ok=True)

# Fix v2.2: FEE=0.0009 single-side (was 0.0018 which over-charged fees ~2x)
FEE      = 0.0009
CAPITAL  = 150.0
RISK_PCT = 0.02
MAX_HOLD_BARS = 20

# Fix v2.2: 扩展到6品种（与回测一致）
SYMBOLS  = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "LINKUSDT", "POLUSDT"]
INTERVAL = "15m"
LIMIT    = 250  # 足够计算EMA200+ADX


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# Fix v2.2: Load per-symbol params from config (was all hardcoded to default values)
def load_symbol_params():
    """Load per-symbol strategy params from optimal_params_v21.json."""
    default = {'sc':5,'lc':4,'ccp':0.002,'adx':20,'tp_s':1.0,'tp_l':0.8,'long_disabled':False}
    if not CONFIG_FILE.exists():
        log(f"WARNING: config not found at {CONFIG_FILE}, using defaults")
        return {s: default.copy() for s in SYMBOLS}
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
        result = {}
        for sym in SYMBOLS:
            sp = cfg.get('symbols', {}).get(sym, {})
            result[sym] = {
                'sc': sp.get('sc', default['sc']),
                'lc': sp.get('lc', default['lc']),
                'ccp': sp.get('ccp', default['ccp']),
                'adx': sp.get('adx_th', default['adx']),
                'tp_s': sp.get('tp_s', default['tp_s']),
                'tp_l': sp.get('tp_l', default['tp_l']),
                'long_disabled': sp.get('long_disabled', default['long_disabled']),
            }
        return result
    except Exception as e:
        log(f"WARNING: config parse error ({e}), using defaults")
        return {s: default.copy() for s in SYMBOLS}


def fetch_klines(symbol, interval="15m", limit=250):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        return data
    except Exception as e:
        log(f"❌ fetch_klines {symbol}: {e}")
        return None


def parse_klines(data):
    if not data: return None
    rows = []
    for k in data:
        rows.append({
            "ts": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5])
        })
    return pd.DataFrame(rows)


def ema_vec(s, n):
    a = 2/(n+1); out = np.zeros(len(s)); out[0] = s[0]
    for i in range(1, len(s)): out[i] = s[i]*a + out[i-1]*(1-a)
    return out


def calc_atr(df, n=14):
    """Wilder's ATR - consistent with signal_engine.py and backtest_engine_v2.py (v2.2)"""
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    prev_c = np.roll(c, 1); prev_c[0] = c[0]
    tr = np.maximum(h-l, np.maximum(np.abs(h-prev_c), np.abs(l-prev_c)))
    tr[0] = h[0]-l[0]
    atr = np.zeros(len(tr))
    atr[:n] = tr[:n].mean()
    alpha = 1.0 / n
    for i in range(n, len(tr)):
        atr[i] = atr[i-1] * (1-alpha) + tr[i] * alpha
    return atr


def calc_adx(df, n=14):
    """Wilder's ADX - consistent with signal_engine.py and backtest_engine_v2.py (v2.2)"""
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    prev_c = np.roll(c, 1); prev_c[0] = c[0]
    tr = np.maximum(h-l, np.maximum(np.abs(h-prev_c), np.abs(l-prev_c)))
    tr[0] = h[0]-l[0]
    pdm = np.where((h - np.roll(h, 1) > np.roll(l, 1) - l) & (h - np.roll(h, 1) > 0),
                   h - np.roll(h, 1), 0.0)
    ndm = np.where((np.roll(l, 1) - l > h - np.roll(h, 1)) & (np.roll(l, 1) - l > 0),
                   np.roll(l, 1) - l, 0.0)
    pdm[0] = ndm[0] = 0.0
    alpha = 1.0 / n
    atr14 = np.zeros(len(tr)); atr14[:n] = tr[:n].mean()
    p14   = np.zeros(len(tr)); p14[:n]   = pdm[:n].mean()
    d14   = np.zeros(len(tr)); d14[:n]   = ndm[:n].mean()
    for i in range(n, len(tr)):
        atr14[i] = atr14[i-1]*(1-alpha) + tr[i]*alpha
        p14[i]   = p14[i-1]*(1-alpha) + pdm[i]*alpha
        d14[i]   = d14[i-1]*(1-alpha) + ndm[i]*alpha
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = np.where(atr14 > 0, 100 * p14 / atr14, 0.0)
        ndi = np.where(atr14 > 0, 100 * d14 / atr14, 0.0)
        dx  = np.where((pdi + ndi) > 0, 100 * np.abs(pdi - ndi) / (pdi + ndi), 0.0)
    adx = np.zeros(len(dx)); adx[:n] = dx[:n].mean()
    for i in range(n, len(dx)):
        adx[i] = adx[i-1]*(1-alpha) + dx[i]*alpha
    return adx


def check_short_signal(df, sc=6, min_pct=0.002, adx_th=20):
    """Fix v2.2: Accept per-symbol params (was hardcoded n=6, pct=0.002, adx=20)"""
    if len(df) < 220: return None
    c = df["close"].values
    adx = calc_adx(df)
    i = len(c) - 1
    if adx[i] < adx_th: return None
    if i < sc+1: return None
    mvs = [c[i-k]-c[i-k-1] for k in range(sc)]
    cum = (c[i]-c[i-sc])/c[i-sc]
    if all(m > 0 for m in mvs) and cum >= min_pct:
        atr = calc_atr(df)
        return {
            "direction": "SHORT",
            "price": c[i],
            "sl": c[i] + atr[i],
            "tp": c[i] - atr[i],
            "atr": atr[i],
            "adx": adx[i],
            "cum_rise": cum
        }
    return None


def check_long_signal(df, lc=4, min_pct=0.002, adx_th=20, tp_mult=0.8):
    """Fix v2.2: Accept per-symbol params + tp_mult; was hardcoded n=4, pct=0.002, adx=20, tp=1.0ATR"""
    if len(df) < 220: return None
    c = df["close"].values
    adx = calc_adx(df)
    ema200 = ema_vec(c, 200)
    i = len(c) - 1
    if adx[i] < adx_th: return None
    if c[i] <= ema200[i]: return None
    if i < lc+1: return None
    mvs = [c[i-k]-c[i-k-1] for k in range(lc)]
    cum = (c[i-lc]-c[i])/c[i-lc]
    if all(m < 0 for m in mvs) and cum >= min_pct:
        atr = calc_atr(df)
        return {
            "direction": "LONG",
            "price": c[i],
            "sl": c[i] - atr[i],
            "tp": c[i] + atr[i]*tp_mult,
            "atr": atr[i],
            "adx": adx[i],
            "cum_drop": cum,
            "ema200": ema200[i]
        }
    return None


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"positions": {}, "trades": [], "capital": CAPITAL, "scan_count": 0}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def position_size(capital, price, atr, risk_pct=RISK_PCT):
    """计算开仓量 (张数)"""
    risk_u = capital * risk_pct
    sl_u   = atr
    qty    = risk_u / sl_u
    return round(risk_u, 2), round(qty, 6)


def update_positions(state, symbol, current_price, current_high, current_low):
    """Check TP/SL hits. Fix v2.2: PnL formula was wrong for SHORT WIN."""
    pos = state["positions"].get(symbol)
    if not pos: return None
    direction = pos["direction"]
    sl = pos["sl"]; tp = pos["tp"]
    entry = pos["entry"]
    result = None
    if direction == "SHORT":
        if current_high >= sl:
            result = "LOSS"
        elif current_low <= tp:
            result = "WIN"
    else:  # LONG
        if current_low <= sl:
            result = "LOSS"
        elif current_high >= tp:
            result = "WIN"
    if result:
        # Fix v2.2: was (tp-entry)/entry for SHORT which is negative (wrong direction)
        # SHORT win: price dropped from entry to tp -> profit = (entry-tp)/entry (positive)
        # LONG win:  price rose from entry to tp -> profit = (tp-entry)/entry (positive)
        if result == "WIN":
            pnl_r = (entry - tp) / entry if direction == "SHORT" else (tp - entry) / entry
        else:  # LOSS
            pnl_r = -(sl - entry) / entry if direction == "SHORT" else -(entry - sl) / entry
        risk_u = state["capital"] * RISK_PCT
        sl_dist_pct = abs(entry - pos["sl"]) / entry if entry > 0 else 0.01
        if sl_dist_pct > 0:
            # PnL = risk * (price_move / sl_distance) - fees
            pnl_u = pnl_r / sl_dist_pct * risk_u - 2 * FEE * risk_u / sl_dist_pct
        else:
            pnl_u = pnl_r * risk_u
        exit_price = tp if result == "WIN" else sl
        log(f"  🔔 {symbol} {direction} {result}: entry={entry:.2f} exit={exit_price:.2f} PnL={pnl_u:+.2f}U")
        state["trades"].append({
            "symbol": symbol, "direction": direction, "result": result,
            "entry": entry, "exit": exit_price,
            "pnl_u": round(pnl_u, 3),
            "time": datetime.now(timezone.utc).isoformat()
        })
        state["capital"] += pnl_u
        del state["positions"][symbol]
    return result


def scan_once(state):
    # Fix v2.2: load per-symbol params from config
    sym_params = load_symbol_params()

    state["scan_count"] = state.get("scan_count", 0) + 1
    log(f"=== 扫描 #{state['scan_count']} | 资金: {state['capital']:.2f}U ===")
    signals_found = []
    for symbol in SYMBOLS:
        data = fetch_klines(symbol, INTERVAL, LIMIT)
        if not data: continue
        df = parse_klines(data)
        if df is None or len(df) < 220: continue
        last = df.iloc[-1]
        # 检查已有持仓
        if symbol in state["positions"]:
            update_positions(state, symbol, last["close"], last["high"], last["low"])
            continue
        # Fix v2.2: use per-symbol params
        p = sym_params.get(symbol, {})
        sc = p.get('sc', 6); lc = p.get('lc', 4); ccp = p.get('ccp', 0.002)
        adx_th = p.get('adx', 20); tp_s = p.get('tp_s', 1.0); tp_l = p.get('tp_l', 0.8)
        long_disabled = p.get('long_disabled', False)

        # 检查新信号（互斥）
        sig_s = check_short_signal(df, sc=sc, min_pct=ccp, adx_th=adx_th)
        # Fix v2.2: respect long_disabled flag per symbol (BNBUSDT/POLUSDT)
        sig_l = None if long_disabled else check_long_signal(df, lc=lc, min_pct=ccp, adx_th=adx_th, tp_mult=tp_l)
        if sig_s and sig_l:
            log(f"  ⚡ {symbol}: SHORT+LONG冲突 → 跳过")
            continue
        sig = sig_s or sig_l
        if sig:
            signals_found.append((symbol, sig))
            log(f"  🟢 {symbol} {sig['direction']} @ {sig['price']:.4f} | ADX={sig['adx']:.1f} | TP={sig['tp']:.4f} | SL={sig['sl']:.4f}")
    # 开仓（最多同时持有3个，与optimal_params_v21一致）
    active = len(state["positions"])
    for symbol, sig in signals_found:
        if active >= 3: break
        risk_u, qty = position_size(state["capital"], sig["price"], sig["atr"])
        state["positions"][symbol] = {
            "direction": sig["direction"],
            "entry": sig["price"],
            "sl": sig["sl"],
            "tp": sig["tp"],
            "atr": sig["atr"],
            "risk_u": risk_u,
            "qty": qty,
            "open_time": datetime.now(timezone.utc).isoformat(),
            "scan_open": state["scan_count"]
        }
        log(f"  📌 开仓: {symbol} {sig['direction']} entry={sig['price']:.4f} qty={qty} risk={risk_u:.2f}U")
        active += 1
    if not signals_found:
        log(f"  〇 无信号 | 持仓: {list(state['positions'].keys()) or '空'}")
    return state


def print_summary(state):
    trades = state.get("trades", [])
    if not trades:
        log("  📊 无成交记录")
        return
    wins = sum(1 for t in trades if t["result"]=="WIN")
    n = len(trades)
    total_pnl = sum(t.get("pnl_u",0) for t in trades)
    wr = wins/n if n > 0 else 0
    log(f"  📊 累计: {n}笔 WR={wr:.1%} 总PnL={total_pnl:+.2f}U 资金={state['capital']:.2f}U")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    state = load_state()
    if mode == "once":
        log("🚀 单次扫描模式")
        state = scan_once(state)
        print_summary(state)
        save_state(state)
    elif mode == "loop":
        log(f"🚀 循环扫描模式 (每15分钟)")
        while True:
            try:
                state = scan_once(state)
                print_summary(state)
                save_state(state)
                log(f"💤 等待 15 分钟...")
                time.sleep(900)
            except KeyboardInterrupt:
                log("⛔ 手动停止")
                break
            except Exception as e:
                log(f"❌ 异常: {e}")
                time.sleep(60)
    elif mode == "status":
        print_summary(state)
        print(json.dumps(state["positions"], indent=2, ensure_ascii=False))
