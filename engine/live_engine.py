"""
主引擎: live_engine.py
整合4个模块，实现完整实盘交易循环:
1. 启动时: REST预填充K线 + 恢复未平仓位
2. WS实时监听: 每根15m K线收盘触发信号检测
3. 信号 → 风控检查 → 下单 → 状态记录
4. 轮询SL/TP状态 → 平仓回调 → 风控更新
5. 全程日志 + 状态持久化
"""
import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import signal
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# 确保引擎目录在路径中
sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.ws_feeder import BinanceWSFeeder, KlineBuffer, init_buffer_from_rest, Kline
from engine.signal_engine import SignalEngine, Signal
from engine.risk_engine import RiskEngine
from engine.order_executor import BinanceExecutor, Position

# ── 配置 ─────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH  = BASE_DIR / "config" / "strategy_v12.json"
STATE_PATH   = BASE_DIR / "engine" / "state" / "live_state.json"
LOG_PATH     = BASE_DIR / "logs" / "live_engine.log"

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

# 日志轮转：单文件最大5MB，保留2个备份
_root_logger = logging.getLogger()
_root_logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(name)s] %(message)s")
_fh = RotatingFileHandler(LOG_PATH, maxBytes=5*1024*1024, backupCount=2, encoding='utf-8')
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
_root_logger.addHandler(_fh)
_root_logger.addHandler(_sh)
logger = logging.getLogger("live_engine")

# ── API密钥（从TOOLS.md读取）─────────────────────
MAINNET_KEY    = "zv6mpAUG7avCTk9IUztR8Ysegyj3AgIPDEnZt31ycA4600msoQlwiU358jMse3w1"
MAINNET_SECRET = "JgtCa5lfjqf51Gj4XeOmGJWDwcITNBFm51eXXDyAXeg2FNZQ5hi9hLDcrx0EkG2Y"
TESTNET_KEY    = "Viubn6nQeiIIo5s2JMjtzvsH4GiSV32LZzyChHnSsIQuAAJgFUFvtcSwMlQhiIMU"
TESTNET_SECRET = "c56EysrokO9u8G82bXQp3h0sgx93tYDJowcQGEQ3rr84gefIa8GwZkPk0PBCNsFJ"


class LiveEngine:
    """
    实盘引擎主控
    TESTNET=True: 使用测试网纸交易
    TESTNET=False: 主网实盘（需主人确认）
    """

    def __init__(self, testnet: bool = True):
        self.testnet = testnet
        self.symbol = "BTCUSDT"
        self.interval = "15m"
        self._position: Optional[Position] = None
        self._running = False

        # 加载配置
        self.config = json.loads(CONFIG_PATH.read_text())
        logger.info(f"[Main] 策略配置: {self.config['version']}")

        # 初始化各模块
        key    = TESTNET_KEY    if testnet else MAINNET_KEY
        secret = TESTNET_SECRET if testnet else MAINNET_SECRET

        self.buf      = KlineBuffer(maxlen=300)
        self.signal_engine = SignalEngine(
            symbol=self.symbol,
            n_short=self.config["strategies"]["SHORT"]["n_bars"],
            n_long=self.config["strategies"]["LONG"]["n_bars"],
            min_pct=self.config["strategies"]["SHORT"]["min_pct"],
            adx_min=self.config["strategies"]["SHORT"]["adx_min"],
            tp_mult_short=self.config["strategies"]["SHORT"]["tp_atr_mult"],
            sl_mult_short=self.config["strategies"]["SHORT"]["sl_atr_mult"],
            tp_mult_long=self.config["strategies"]["LONG"]["tp_atr_mult"],
            sl_mult_long=self.config["strategies"]["LONG"]["sl_atr_mult"],
        )
        self.risk    = RiskEngine(str(STATE_PATH), self.config)
        self.executor = BinanceExecutor(key, secret, testnet=testnet)
        self.feeder   = BinanceWSFeeder(
            symbol=self.symbol,
            interval=self.interval,
            buffer=self.buf,
            on_closed=self._on_kline_closed,
            testnet=testnet,
        )

        net_str = "【测试网】" if testnet else "【主网实盘】"
        logger.info(f"[Main] LiveEngine 初始化完成 {net_str}")

    # ── 启动流程 ──────────────────────────────────

    async def start(self):
        self._running = True
        logger.info("="*60)
        logger.info(f"[Main] 启动实盘引擎 v1.2 | {self.symbol} {self.interval}")
        logger.info("="*60)

        # Step1: 预填充历史K线
        logger.info("[Main] Step1: 预加载250根历史K线...")
        try:
            await init_buffer_from_rest(
                self.symbol, self.interval, self.buf,
                limit=250, testnet=self.testnet
            )
            logger.info(f"[Main] 历史K线加载完成: {len(self.buf)}根")
        except Exception as e:
            logger.error(f"[Main] 历史K线加载失败: {e}")

        # Step2: 断线续单检查
        logger.info("[Main] Step2: 检查未平仓位...")
        try:
            recovered = self.executor.recover_positions(self.symbol)
            if recovered:
                self._position = recovered
                logger.info(f"[Main] 恢复持仓: {recovered.direction} entry={recovered.entry_price}")
            else:
                logger.info("[Main] 无未平仓位")
        except Exception as e:
            logger.error(f"[Main] 持仓恢复失败: {e}")

        # Step3: 打印当前风控状态
        status = self.risk.status_dict()
        logger.info(f"[Main] 风控状态: {status}")

        # Step4: 启动WS监听
        logger.info("[Main] Step3: 启动WebSocket监听...")
        ws_task = asyncio.create_task(self.feeder.run())

        # Step5: 启动持仓监控
        monitor_task = asyncio.create_task(self._position_monitor_loop())

        # Step6: 启动心跳日志
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try:
            await asyncio.gather(ws_task, monitor_task, heartbeat_task)
        except asyncio.CancelledError:
            logger.info("[Main] 收到停止信号")
        finally:
            self.feeder.stop()

    def stop(self):
        self._running = False
        self.feeder.stop()
        logger.info("[Main] 引擎停止")

    # ── K线收盘回调（核心逻辑）────────────────────

    async def _on_kline_closed(self, kline: Kline):
        """每根15m K线收盘时触发"""
        ts_str = datetime.fromtimestamp(kline.ts/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        logger.info(f"[Signal] ── K线收盘: {ts_str} C={kline.close:.2f} ──")

        # 已有持仓：不开新仓
        if self._position is not None:
            logger.info(f"[Signal] 持仓中({self._position.direction})，跳过信号检测")
            return

        # 获取K线快照
        bars = self.buf.closed_bars()
        if len(bars) < 250:
            logger.info(f"[Signal] K线不足({len(bars)}<250)，跳过")
            return

        # 风控检查
        can, reason = self.risk.can_trade()
        if not can:
            logger.warning(f"[Signal] 风控阻止: {reason}")
            return

        # 信号检测
        sig = self.signal_engine.evaluate(bars)
        logger.info(f"[Signal] {sig.direction} | {sig.reason}")

        if sig.direction == "NONE":
            return

        # 仓位计算
        qty, notional = self.risk.calc_position(sig.entry_price, sig.sl_price)
        if qty <= 0:
            logger.warning("[Signal] 仓位为0，跳过")
            return

        # 开仓
        await self._open_trade(sig, qty)

    # ── 开仓 ─────────────────────────────────────

    async def _open_trade(self, sig: Signal, qty: float):
        logger.info(
            f"[Trade] 开仓 {sig.direction} | "
            f"entry≈{sig.entry_price:.2f} SL={sig.sl_price:.2f} TP={sig.tp_price:.2f} "
            f"qty={qty:.4f} | {sig.reason}"
        )
        pos = self.executor.open_position(
            symbol=self.symbol,
            direction=sig.direction,
            quantity=qty,
            sl_price=sig.sl_price,
            tp_price=sig.tp_price,
        )
        if pos is None:
            logger.error("[Trade] 开仓失败")
            return

        self._position = pos
        self.risk.on_trade_open(sig.direction, pos.entry_price, pos.sl_price, pos.tp_price, qty)

        # 持久化持仓
        self._save_position()
        logger.info(f"[Trade] ✅ 开仓成功: {pos.direction} entry={pos.entry_price}")

    # ── 持仓监控循环 ──────────────────────────────

    async def _position_monitor_loop(self):
        """每30秒轮询SL/TP订单状态"""
        while self._running:
            await asyncio.sleep(30)
            if self._position is None:
                continue

            pos = self._position
            closed = False
            outcome = None

            try:
                # 检查SL订单
                if pos.sl_order_id:
                    sl_status = self.executor.check_order_status(pos.symbol, pos.sl_order_id)
                    if sl_status == "FILLED":
                        logger.info(f"[Monitor] 止损触发: SL={pos.sl_price}")
                        closed = True
                        outcome = "loss"

                # 检查TP订单
                if not closed and pos.tp_order_id:
                    tp_status = self.executor.check_order_status(pos.symbol, pos.tp_order_id)
                    if tp_status == "FILLED":
                        logger.info(f"[Monitor] 止盈触发: TP={pos.tp_price}")
                        closed = True
                        outcome = "win"

                # 超时平仓检查（MAX_HOLD=20根K线=300分钟）
                if not closed:
                    hold_ms = int(time.time() * 1000) - pos.open_time
                    hold_bars = hold_ms / (15 * 60 * 1000)
                    if hold_bars >= 20:
                        logger.warning(f"[Monitor] 超时({hold_bars:.1f}根)，强制平仓")
                        self.executor.close_position(pos)
                        self.executor.cancel_all_orders(pos.symbol)
                        closed = True
                        outcome = "timeout"

                if closed:
                    await self._on_trade_closed(pos, outcome)

            except Exception as e:
                logger.error(f"[Monitor] 监控异常: {e}")

    async def _on_trade_closed(self, pos: Position, outcome: str):
        """持仓关闭回调"""
        # 取消剩余挂单
        self.executor.cancel_all_orders(pos.symbol)

        # 估算PnL（实际应从成交回报获取）
        try:
            live_positions = self.executor.get_positions()
            closed_pos = next((p for p in live_positions if p["symbol"] == pos.symbol), None)
            if closed_pos is None:
                # 仓位已平，估算PnL
                risk_amount = self.risk.state.capital * self.risk.get_risk_pct()
                pnl = risk_amount if outcome == "win" else -risk_amount * (pos.sl_price / pos.entry_price if pos.entry_price > 0 else 1)
            else:
                pnl = float(closed_pos.get("unrealizedProfit", 0))
        except Exception:
            pnl = 0.0

        self.risk.on_trade_close(pnl, outcome if outcome != "timeout" else "loss")
        self._position = None
        self._save_position()

        logger.info(
            f"[Trade] 平仓完成: {outcome.upper()} PnL≈{pnl:+.2f}U | "
            f"资金={self.risk.state.capital:.2f}U"
        )

    # ── 心跳日志 ──────────────────────────────────

    async def _heartbeat_loop(self):
        """每15分钟打印一次状态摘要"""
        while self._running:
            await asyncio.sleep(900)
            status = self.risk.status_dict()
            pos_str = f"{self._position.direction}" if self._position else "空仓"
            logger.info(
                f"[♥] 心跳 | 资金={status['capital']:.2f}U | "
                f"今日={status['today_pnl']:+.2f}U | "
                f"持仓={pos_str} | "
                f"风险={status['current_risk_pct']*100:.1f}% | "
                f"{'⛔'+status['halt_reason'] if not status['can_trade'] else '✅可交易'}"
            )

    # ── 状态持久化 ────────────────────────────────

    def _save_position(self):
        pos_path = STATE_PATH.parent / "current_position.json"
        if self._position:
            data = asdict(self._position)
            pos_path.write_text(json.dumps(data, indent=2))
        else:
            if pos_path.exists():
                pos_path.unlink()


# ── 入口 ─────────────────────────────────────────

def main(testnet: bool = True):
    engine = LiveEngine(testnet=testnet)

    # 优雅退出
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown(*_):
        logger.info("[Main] 收到退出信号，正在停止...")
        engine.stop()
        for task in asyncio.all_tasks(loop):
            task.cancel()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        loop.run_until_complete(engine.start())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
        logger.info("[Main] 引擎已退出")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="杀手锏交易系统 v1.2 实盘引擎")
    parser.add_argument("--live", action="store_true", help="使用主网实盘（默认测试网）")
    args = parser.parse_args()

    if args.live:
        confirm = input("⚠️  即将启动主网实盘交易！确认请输入 'YES': ")
        if confirm != "YES":
            print("已取消")
            sys.exit(0)
        main(testnet=False)
    else:
        print("🧪 测试网模式启动（使用 --live 切换主网）")
        main(testnet=True)
