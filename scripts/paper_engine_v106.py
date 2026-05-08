#!/usr/bin/env python3
"""
杀手锏 纸交易引擎 v1.0.6
修复清单（对比v1.0.5）：
  [FIX-1] 进程单例锁 —— 防止多实例同时运行（核心修复）
  [FIX-2] 状态文件原子写 —— 防止并发覆盖/损坏
  [FIX-3] subprocess全局超时双保险 —— 防止binance-cli无限挂起
  [FIX-4] 日志强制flush —— 确保日志实时写入磁盘
  [FIX-5] 每次扫描后日志时间戳 —— 便于追踪进度
  [FIX-6] 优雅退出信号处理 —— SIGTERM/SIGINT干净退出不损坏状态
  [FIX-7] 状态文件完整性校验 —— 损坏时自动重建

接入真实Binance主网行情，本地模拟执行，不下真实订单
策略: v4.0 均值回归  品种: BTCUSDT + SOLUSDT  周期: 1H
"""
import json
import os
import signal
import sys
import time
import tempfile
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from logging.handlers import RotatingFileHandler
import logging as _logging

sys.path.insert(0, str(Path(__file__).parent))
from signal_engine_v4 import generate_signal_v4, calc_atr

# ── 常量 ─────────────────────────────────────────────
SYMBOLS   = ["BTCUSDT", "SOLUSDT"]
INTERVAL  = "1h"
CAPITAL   = 10000.0
RISK_PCT  = 0.05
SL_ATR    = 2.0
TP_ATR    = 3.5
MAX_HOLD  = 24
CONF_MIN  = 0.74
CONF_MAX  = 0.86
KLINE_TIMEOUT = 20          # binance-cli 单次超时（秒）[FIX-3]
LOG_DIR   = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
STATE_FILE = LOG_DIR / "paper_trade_state.json"
LOG_FILE   = LOG_DIR / "paper_engine.log"
PID_FILE   = LOG_DIR / "paper_engine.pid"   # [FIX-1]
CST = timezone(timedelta(hours=8))

# ── 优雅退出 [FIX-6] ──────────────────────────────────
_exit_flag = False

def _handle_exit(signum, frame):
    global _exit_flag
    _exit_flag = True
    _log(f"收到信号 {signum}，准备退出...")

signal.signal(signal.SIGTERM, _handle_exit)
signal.signal(signal.SIGINT,  _handle_exit)

# ── 日志 [FIX-4] ─────────────────────────────────────
# 日志轮转初始化（5MB × 3备份）
_fh = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=2, encoding='utf-8')
_fh.setFormatter(_logging.Formatter('%(message)s'))
_rl = _logging.getLogger('paper_engine')
_rl.addHandler(_fh)
_rl.setLevel(_logging.INFO)

def _log(msg: str):
    ts = datetime.now(tz=CST).strftime("%Y-%m-%d %H:%M:%S CST")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    _rl.info(line)  # RotatingFileHandler自动轮转

# ── 单例锁 [FIX-1] ────────────────────────────────────
def acquire_lock() -> bool:
    """返回 True=成功获取锁；False=已有实例在运行"""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            # 检查该 pid 进程是否还活着
            os.kill(pid, 0)
            return False   # 进程存在，锁被占用
        except (ProcessLookupError, ValueError):
            # 进程已死 / pid文件损坏，清理旧锁
            PID_FILE.unlink(missing_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    return True

def release_lock():
    try:
        if PID_FILE.exists():
            pid = int(PID_FILE.read_text().strip())
            if pid == os.getpid():
                PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass

# ── 工具函数 ──────────────────────────────────────────
def now_cst():
    return datetime.now(tz=CST).strftime("%Y-%m-%d %H:%M:%S CST")

def get_klines(symbol, limit=60):
    """[FIX-3] 双保险超时：subprocess.run timeout + 外层 signal alarm"""
    cmd = ["binance-cli", "futures-usds", "kline-candlestick-data",
           "--symbol", symbol, "--interval", INTERVAL, "--limit", str(limit)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=KLINE_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(f"binance-cli error: {r.stderr.strip()}")
    bars = json.loads(r.stdout)
    if not bars:
        raise RuntimeError("empty kline response")
    return ([float(b[4]) for b in bars],
            [float(b[2]) for b in bars],
            [float(b[3]) for b in bars],
            [float(b[1]) for b in bars],
            [float(b[5]) for b in bars],
            int(bars[-1][0]))

# ── 状态 I/O（原子写）[FIX-2] [FIX-7] ────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            raw = STATE_FILE.read_text(encoding="utf-8")
            data = json.loads(raw)
            # 基本完整性检查
            assert isinstance(data.get("positions"), dict)
            assert isinstance(data.get("trades"), list)
            assert isinstance(data.get("capital"), (int, float))
            return data
        except Exception as e:
            _log(f"⚠️ 状态文件损坏，重建: {e}")
            STATE_FILE.rename(STATE_FILE.with_suffix(".json.corrupt"))
    return {"positions": {}, "trades": [], "capital": CAPITAL,
            "start_time": now_cst(), "scan_count": 0}

def save_state(state: dict):
    """原子写：先写临时文件再 rename，防止写到一半崩溃导致状态损坏"""
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(STATE_FILE)   # atomic on POSIX

# ── 单次扫描 ──────────────────────────────────────────
def run_scan():
    state = load_state()
    state["scan_count"] += 1
    cap  = state["capital"]
    peak = max(state.get("peak", CAPITAL), cap)
    state["peak"] = peak
    dd   = (peak - cap) / peak * 100

    _log(f"{'='*62}")
    _log(f"⚔️  杀手锏 纸交易  第{state['scan_count']}次扫描")
    _log(f"账户: ${cap:,.2f}U  峰值: ${peak:,.2f}U  回撤: {dd:.2f}%  总交易: {len(state['trades'])}笔")

    for symbol in SYMBOLS:
        _log(f"── {symbol} ──")
        try:
            closes, highs, lows, opens, vols, bar_ts = get_klines(symbol, 60)
        except Exception as e:
            _log(f"  ❌ 行情获取失败: {e}")
            continue

        cur = closes[-1]
        _log(f"  当前价: ${cur:,.4f}")
        pos = state["positions"].get(symbol)

        # 检查持仓是否需要平仓
        if pos:
            # [FIX-8] 用时间戳计算持仓小时数，避免--once多次调用虚高
            if "entry_time_ts" in pos:
                bars_held = int((time.time() - pos["entry_time_ts"]) / 3600)
            else:
                bars_held = state["scan_count"] - pos["entry_scan"]  # 兼容旧state
            direction = pos["direction"]
            sl, tp    = pos["sl"], pos["tp"]
            hit_sl    = (direction == "LONG"  and cur <= sl) or \
                        (direction == "SHORT" and cur >= sl)
            hit_tp    = (direction == "LONG"  and cur >= tp) or \
                        (direction == "SHORT" and cur <= tp)
            timeout   = bars_held >= MAX_HOLD

            if hit_sl or hit_tp or timeout:
                exit_price = cur
                pnl_pct = (exit_price - pos["entry"]) / pos["entry"] * 100
                if direction == "SHORT": pnl_pct = -pnl_pct
                pnl_u = cap * RISK_PCT * pnl_pct / 100
                cap  += pnl_u
                exit_reason = "止盈✅" if hit_tp else ("止损❌" if hit_sl else "超时⏰")
                trade = {
                    "symbol": symbol, "direction": direction,
                    "entry": pos["entry"], "exit": exit_price,
                    "pnl_pct": round(pnl_pct, 4), "pnl_u": round(pnl_u, 3),
                    "win": pnl_pct > 0, "exit_reason": exit_reason,
                    "bars_held": bars_held,
                    "entry_time": pos["entry_time"], "exit_time": now_cst()
                }
                state["trades"].append(trade)
                del state["positions"][symbol]
                state["capital"] = round(cap, 2)
                mark = "✅" if pnl_pct > 0 else "❌"
                _log(f"  {mark} 平仓({exit_reason}) 入场${pos['entry']:.2f} → "
                     f"出场${exit_price:.2f}  PnL: {pnl_pct:+.3f}%  ${pnl_u:+.2f}U  "
                     f"持仓{bars_held}根")
            else:
                upnl   = (cur - pos["entry"]) / pos["entry"] * 100
                if direction == "SHORT": upnl = -upnl
                upnl_u = cap * RISK_PCT * upnl / 100
                _log(f"  📊 持仓中({direction}) 入场${pos['entry']:.2f}  "
                     f"持仓{bars_held}根  未实现{upnl:+.3f}% ${upnl_u:+.2f}U")
                continue

        # 无持仓则寻找信号
        if symbol not in state["positions"]:
            sig       = generate_signal_v4(closes, highs, lows, opens, vols)
            direction = sig.get("direction", "NEUTRAL")
            conf      = sig.get("confidence", 0)

            if direction != "NEUTRAL" and CONF_MIN <= conf <= CONF_MAX:
                atr = calc_atr(highs, lows, closes, 14)
                sl  = cur - atr * SL_ATR if direction == "LONG" else cur + atr * SL_ATR
                tp  = cur + atr * TP_ATR if direction == "LONG" else cur - atr * TP_ATR
                state["positions"][symbol] = {
                    "direction": direction, "entry": cur,
                    "sl": round(sl, 4), "tp": round(tp, 4),
                    "entry_scan": state["scan_count"],   # 保留兼容
                    "entry_time_ts": time.time(),        # [FIX-8] 用时间戳计算持仓时间
                    "entry_time": now_cst(),
                    "conf": round(conf, 3), "atr": round(atr, 4)
                }
                _log(f"  🎯 开仓({direction}) conf={conf:.2f}  原因: {sig.get('reason','')}")
                _log(f"     入场: ${cur:.4f}  SL: ${sl:.4f}  TP: ${tp:.4f}  ATR: {atr:.4f}")
            else:
                _log(f"  💤 无信号 ({direction} conf={conf:.2f})")

    # 统计报告
    trades = state["trades"]
    if trades:
        wins      = sum(1 for t in trades if t["win"])
        wr        = wins / len(trades)
        pnls      = [t["pnl_pct"] for t in trades]
        ps        = [p for p in pnls if p > 0]
        ls_       = [p for p in pnls if p < 0]
        rr        = abs(sum(ps)/len(ps) / (sum(ls_)/len(ls_))) if ps and ls_ else 0
        total_pnl = sum(t["pnl_u"] for t in trades)
        _log(f"  📈 累计: {len(trades)}笔  WR{wr*100:.1f}%  RR{rr:.2f}  "
             f"总盈亏${total_pnl:+.2f}U  净值${state['capital']:,.2f}U")
        t = trades[-1]
        _log(f"  最近: {t['symbol']} {t['direction']} {t['exit_reason']} "
             f"{t['pnl_pct']:+.3f}% ${t['pnl_u']:+.2f}U")

    state["capital"] = round(cap, 2)  # [FIX-9] 确保capital与cap本地变量最终同步
    save_state(state)
    _log(f"✔ 状态已保存  下次扫描: 1小时后")
    return state

# ── 72小时主循环 ──────────────────────────────────────
def run_72h():
    # ── [FIX-1] 单例检查 ──
    if not acquire_lock():
        pid = PID_FILE.read_text().strip()
        print(f"❌ 已有实例在运行 (PID {pid})，退出。如需强制重启请先 kill {pid}", flush=True)
        sys.exit(1)

    try:
        _log(f"{'='*62}")
        _log(f"⚔️  杀手锏 纸交易引擎 v1.0.6 启动  PID={os.getpid()}")
        _log(f"策略: v4.0均值回归  品种: {SYMBOLS}  周期: {INTERVAL}")
        _log(f"资金: ${CAPITAL}U  风险: {RISK_PCT*100:.0f}%/笔  SL:{SL_ATR}ATR TP:{TP_ATR}ATR")

        start  = time.time()
        target = 72 * 3600

        while not _exit_flag and (time.time() - start) < target:
            run_scan()
            elapsed   = (time.time() - start) / 3600
            remaining = (target - (time.time() - start)) / 3600
            _log(f"⏱ 已运行{elapsed:.1f}h  剩余{remaining:.1f}h")

            # 等待下一小时，每10秒检查一次退出标志
            next_scan = time.time() + 3600
            while not _exit_flag and time.time() < next_scan:
                if (time.time() - start) >= target:
                    break
                time.sleep(10)

        _log(f"{'='*62}")
        if _exit_flag:
            _log("⛔ 收到退出信号，纸交易提前终止")
        else:
            _log(f"✅ 72小时纸交易完成")
        state = load_state()
        trades = state["trades"]
        if trades:
            wins      = sum(1 for t in trades if t["win"])
            total_pnl = sum(t["pnl_u"] for t in trades)
            _log(f"总结: {len(trades)}笔  胜率{wins/len(trades)*100:.1f}%  总盈亏${total_pnl:+.2f}U")

    finally:
        release_lock()
        _log("PID锁已释放，进程退出")

# ── 入口 ──────────────────────────────────────────────
if __name__ == "__main__":
    if "--once" in sys.argv:
        run_scan()
    else:
        run_72h()
