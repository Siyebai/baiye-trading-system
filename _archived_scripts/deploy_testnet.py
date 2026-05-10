#!/usr/bin/env python3
"""测试网部署脚本 - 4品种并行"""
import json, sys, asyncio, logging, signal
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))
from engine.ws_feeder import BinanceWSFeeder, KlineBuffer, init_buffer_from_rest
from engine.signal_engine import SignalEngine
from engine.risk_engine import RiskEngine
from engine.order_executor import BinanceExecutor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("deploy")

TESTNET_KEY = "Viubn6nQeiIIo5s2JMjtzvsH4GiSV32LZzyChHnSsIQuAAJgFUFvtcSwMlQhiIMU"
TESTNET_SECRET = "c56EysrokO9u8G82bXQp3h0sgx93tYDJowcQGEQ3rr84gefIa8GwZkPk0PBCNsFJ"

class MultiEngine:
    def __init__(self, config_path, testnet=True):
        self.config = json.loads(Path(config_path).read_text())
        self.symbols = self.config["symbols"]
        self.executor = BinanceExecutor(TESTNET_KEY, TESTNET_SECRET, testnet=True)
        self.risk = RiskEngine("engine/state/multi_state.json", self.config)
        self.engines = {}
        self.buffers = {}
        self.positions = {}
        
        for sym in self.symbols:
            self.engines[sym] = SignalEngine(symbol=sym,
                n_short=self.config["strategies"]["SHORT"]["n_bars"],
                n_long=self.config["strategies"]["LONG"]["n_bars"],
                min_pct=self.config["strategies"]["SHORT"]["min_pct"],
                adx_min=self.config["strategies"]["SHORT"]["adx_min"],
                tp_mult_short=self.config["strategies"]["SHORT"]["tp_atr_mult"],
                sl_mult_short=self.config["strategies"]["SHORT"]["sl_atr_mult"],
                tp_mult_long=self.config["strategies"]["LONG"]["tp_atr_mult"],
                sl_mult_long=self.config["strategies"]["LONG"]["sl_atr_mult"],
            )
            self.buffers[sym] = KlineBuffer(maxlen=300)
        logger.info(f"✅ 初始化完成: {len(self.symbols)}品种")
    
    async def start(self):
        logger.info("="*50)
        logger.info(f"🚀 测试网部署 | 品种: {', '.join(self.symbols)}")
        logger.info("="*50)
        
        # 预加载历史
        for sym in self.symbols:
            try:
                await init_buffer_from_rest(sym, "15m", self.buffers[sym], limit=250, testnet=True)
                logger.info(f"✅ {sym}: {len(self.buffers[sym])}根K线")
            except Exception as e:
                logger.error(f"❌ {sym} 加载失败: {e}")
        
        # 检查持仓恢复
        for sym in self.symbols:
            rec = self.executor.recover_positions(sym)
            if rec:
                self.positions[sym] = rec
                logger.info(f"⚠️ {sym} 恢复持仓: {rec.direction}")
        
        # 打印状态
        st = self.risk.status_dict()
        logger.info(f"💰 资金:{st['capital']:.2f}U | 风险:{st['current_risk_pct']*100:.1f}% | {st['can_trade']}")
        
        logger.info("\n📋 配置摘要:")
        logger.info(f"  品种数: {len(self.symbols)}")
        logger.info(f"  信号/月: ~{self.config['projection']['signals_per_month']}笔")
        logger.info(f"  风险/笔: {self.config['risk_control']['risk_per_trade_u']}U")
        logger.info(f"  日熔断: {self.config['risk_control']['max_daily_loss_u']}U")
        logger.info(f"  月熔断: {self.config['risk_control']['max_monthly_loss_u']}U")
        logger.info(f"  预期月收益: +{self.config['projection']['expected_monthly_pnl_u']}U (+{self.config['projection']['expected_monthly_return_pct']*100:.0f}%)")
        
        logger.info("\n✅ 部署完成！等待信号...")
        logger.info("📝 日志: logs/live_engine.log | 状态: engine/state/multi_state.json")

if __name__ == "__main__":
    cfg = sys.argv[1] if len(sys.argv)>1 else "config/strategy_v13_multi.json"
    engine = MultiEngine(cfg)
    asyncio.run(engine.start())
