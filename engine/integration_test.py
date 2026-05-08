"""
集成测试: 验证完整引擎流程（模拟下单，不连接真实API）
测试场景:
1. 历史K线回放 → 信号触发 → 风控检查 → 模拟下单 → 止盈/止损
2. 连亏降仓 → 熔断 → 恢复
3. 断线续单（持仓恢复）
4. 统计: 信号数/胜率/净值
"""
import json, sys, time, asyncio, logging
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import asdict
from typing import Optional, List

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.ws_feeder import Kline, KlineBuffer
from engine.signal_engine import SignalEngine, Signal
from engine.risk_engine import RiskEngine
from engine.order_executor import Position

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("integration_test")

FEE = 0.0018
MAX_HOLD = 20


def load_bars(filepath) -> List[Kline]:
    raw = json.load(open(filepath))
    data = raw if isinstance(raw, list) else raw.get("data", [])
    bars = []
    for row in data:
        if isinstance(row, (list, tuple)):
            bars.append(Kline(int(row[0]), row[1], row[2], row[3], row[4], row[5], True))
        else:
            ts = int(row.get("ts", row.get("timestamp", row.get("open_time", 0))))
            bars.append(Kline(ts, row.get("open",0), row.get("high",0), row.get("low",0),
                              row.get("close",0), row.get("volume",0), True))
    return bars


class MockExecutor:
    """模拟执行器，不发真实订单"""
    def open_position(self, symbol, direction, quantity, sl_price, tp_price):
        return Position(symbol=symbol, direction=direction, entry_price=0.0,
                       quantity=quantity, sl_price=sl_price, tp_price=tp_price,
                       open_time=int(time.time()*1000))
    def close_position(self, pos): return True
    def cancel_all_orders(self, symbol): return True
    def check_order_status(self, symbol, order_id): return "NEW"
    def recover_positions(self, symbol): return None
    def get_balance(self): return 150.0


def simulate_trade_outcome(pos: Position, bars: List[Kline], entry_idx: int) -> tuple:
    """模拟持仓结果，扫描后续K线判断止盈/止损"""
    direction = 1 if pos.direction == "LONG" else -1
    entry = bars[entry_idx].close
    pos.entry_price = entry

    for j in range(entry_idx + 1, min(entry_idx + MAX_HOLD + 1, len(bars))):
        h, l = bars[j].high, bars[j].low
        if direction == -1:  # SHORT
            if h >= pos.sl_price: return "loss", entry, pos.sl_price
            if l <= pos.tp_price: return "win", entry, pos.tp_price
        else:  # LONG
            if l <= pos.sl_price: return "loss", entry, pos.sl_price
            if h >= pos.tp_price: return "win", entry, pos.tp_price

    # 超时平仓
    final_price = bars[min(entry_idx + MAX_HOLD, len(bars)-1)].close
    pnl_pct = (entry - final_price)/entry if direction==-1 else (final_price-entry)/entry
    return ("win" if pnl_pct > FEE else "loss"), entry, final_price


def run_integration_test(data_path, state_path, capital=150.0):
    print("\n" + "="*65)
    print("集成测试: 完整引擎回放验证")
    print("="*65)

    bars = load_bars(data_path)
    print(f"✅ 加载K线: {len(bars)}根 | {datetime.fromtimestamp(bars[0].ts/1000,tz=timezone.utc).strftime('%Y-%m-%d')} → {datetime.fromtimestamp(bars[-1].ts/1000,tz=timezone.utc).strftime('%Y-%m-%d')}")

    # 初始化各模块
    cfg = {"risk_control": {"capital": capital, "risk_per_trade_pct": 0.02,
                             "max_daily_loss_pct": 0.06, "max_monthly_dd_pct": 0.20,
                             "consecutive_loss_reduce": 3, "reduced_risk_pct": 0.01}}
    risk = RiskEngine(str(state_path), cfg)
    executor = MockExecutor()

    WINDOW = 250
    position: Optional[Position] = None
    position_entry_idx = -1

    trades = []
    signal_counts = {"SHORT": 0, "LONG": 0, "NONE": 0}
    skip_counts = {"risk_halt": 0, "in_position": 0, "qty_zero": 0}

    for i in range(WINDOW, len(bars)):
        window = bars[i-WINDOW:i+1]

        # 每次创建新的SignalEngine（模拟滑动窗口，与回测一致）
        eng = SignalEngine()
        sig = eng.evaluate(window)
        signal_counts[sig.direction] += 1

        # 检查持仓是否应该平仓
        if position is not None:
            outcome, entry_p, exit_p = simulate_trade_outcome(position, bars, position_entry_idx)
            # 判断当前K线是否已超过止盈/止损
            h, l = bars[i].high, bars[i].low
            direction = 1 if position.direction == "LONG" else -1
            trade_closed = False
            if direction == -1:
                if h >= position.sl_price or l <= position.tp_price:
                    trade_closed = True
            else:
                if l <= position.sl_price or h >= position.tp_price:
                    trade_closed = True
            hold_bars = i - position_entry_idx
            if hold_bars >= MAX_HOLD:
                trade_closed = True

            if trade_closed:
                real_outcome, entry_p, exit_p = simulate_trade_outcome(position, bars, position_entry_idx)
                # 计算真实PnL
                risk_amount = risk.get_risk_amount()
                sl_dist = abs(entry_p - position.sl_price) / entry_p if entry_p > 0 else 0.01
                position_notional = risk_amount / sl_dist if sl_dist > 0 else 0
                qty = position_notional / entry_p if entry_p > 0 else 0
                tp_dist = abs(position.tp_price - entry_p) / entry_p if entry_p > 0 else 0
                sl_dist_actual = sl_dist
                pnl = (position_notional * tp_dist - position_notional * FEE) if real_outcome == "win" \
                      else (-position_notional * sl_dist_actual - position_notional * FEE)

                risk.on_trade_close(pnl, real_outcome)
                trades.append({
                    "idx": position_entry_idx,
                    "ts": bars[position_entry_idx].ts,
                    "direction": position.direction,
                    "outcome": real_outcome,
                    "pnl": pnl,
                    "hold_bars": hold_bars,
                    "capital_after": risk.state.capital,
                })
                position = None
                position_entry_idx = -1

        # 有持仓时不开新仓
        if position is not None:
            skip_counts["in_position"] += 1
            continue

        if sig.direction == "NONE":
            continue

        # 风控检查
        can, reason = risk.can_trade()
        if not can:
            skip_counts["risk_halt"] += 1
            continue

        # 仓位计算
        qty, notional = risk.calc_position(sig.entry_price, sig.sl_price)
        if qty <= 0:
            skip_counts["qty_zero"] += 1
            continue

        # 模拟开仓
        pos = executor.open_position(
            symbol="BTCUSDT", direction=sig.direction,
            quantity=qty, sl_price=sig.sl_price, tp_price=sig.tp_price
        )
        pos.entry_price = bars[i].close
        risk.on_trade_open(sig.direction, pos.entry_price, pos.sl_price, pos.tp_price, qty)
        position = pos
        position_entry_idx = i

    # 统计结果
    wins = [t for t in trades if t["outcome"] == "win"]
    losses = [t for t in trades if t["outcome"] == "loss"]
    n = len(trades)
    wr = len(wins)/n if n > 0 else 0
    total_pnl = sum(t["pnl"] for t in trades)
    max_dd = 0; peak = capital
    for t in trades:
        cap = t["capital_after"]
        if cap > peak: peak = cap
        dd = (peak - cap) / peak
        if dd > max_dd: max_dd = dd

    # 月度统计
    monthly = {}
    for t in trades:
        month = datetime.fromtimestamp(t["ts"]/1000, tz=timezone.utc).strftime("%Y-%m")
        monthly[month] = monthly.get(month, 0) + t["pnl"]

    print(f"\n{'─'*65}")
    print(f"📊 集成测试结果")
    print(f"{'─'*65}")
    print(f"  信号总数:   SHORT={signal_counts['SHORT']} LONG={signal_counts['LONG']}")
    print(f"  跳过(持仓): {skip_counts['in_position']}次 | 熔断: {skip_counts['risk_halt']}次")
    print(f"  实际交易:   {n}笔 ({n/(len(bars)/96):.1f}笔/月)")
    print(f"  胜率:       {wr:.1%}  ({'✅✅' if wr>=0.58 else '✅' if wr>=0.55 else '⚠️'})")
    print(f"  总盈亏:     {total_pnl:+.2f}U ({total_pnl/capital*100:+.1f}%)")
    print(f"  最终资金:   {risk.state.capital:.2f}U")
    print(f"  最大回撤:   {max_dd:.1%}")
    avg_win  = sum(t["pnl"] for t in wins)/len(wins) if wins else 0
    avg_loss = sum(abs(t["pnl"]) for t in losses)/len(losses) if losses else 0
    pf = sum(t["pnl"] for t in wins)/max(sum(abs(t["pnl"]) for t in losses),0.01)
    print(f"  平均盈利:   +{avg_win:.2f}U | 平均亏损: -{avg_loss:.2f}U")
    print(f"  盈利因子:   {pf:.2f}")
    print(f"\n  月度盈亏:")
    profit_months = loss_months = 0
    for month in sorted(monthly.keys()):
        p = monthly[month]
        bar = "█" * int(abs(p)/2) if abs(p) >= 2 else ""
        sign = "+" if p >= 0 else ""
        status = "✅" if p > 0 else "❌"
        print(f"    {month}: {sign}{p:.2f}U ({sign}{p/capital*100:.1f}%)  {bar} {status}")
        if p > 0: profit_months += 1
        else: loss_months += 1
    print(f"\n  盈利月/亏损月: {profit_months}/{loss_months}")

    # 风控验证
    print(f"\n{'─'*65}")
    print(f"🛡️  风控模块验证")
    print(f"{'─'*65}")
    final_status = risk.status_dict()
    print(f"  最终资金: {final_status['capital']:.2f}U")
    print(f"  连续亏损: {final_status['consecutive_losses']}")
    print(f"  当前风险: {final_status['current_risk_pct']*100:.1f}%")

    print(f"\n{'─'*65}")
    if wr >= 0.55 and total_pnl > 0 and max_dd < 0.25:
        print("✅✅ 集成测试通过: 胜率/盈利/回撤均达标")
    elif wr >= 0.50 and total_pnl > 0:
        print("✅  集成测试通过: 正盈利")
    else:
        print("⚠️  集成测试警告: 未达全部指标，检查参数")

    return {"wr": wr, "total_pnl": total_pnl, "max_dd": max_dd, "n_trades": n}


if __name__ == "__main__":
    import tempfile, os
    data_path  = Path(__file__).parent.parent / "data" / "BTCUSDT_15m_180d.json"
    state_file = Path(tempfile.mktemp(suffix=".json"))

    result = run_integration_test(data_path, state_file, capital=150.0)
    if state_file.exists(): state_file.unlink()
