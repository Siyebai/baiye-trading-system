"""
模块3: 风控引擎
- 日亏损熔断 (≥6% 当日停止)
- 月度最大回撤熔断 (≥20% 月度暂停)
- 连续亏损降仓 (≥3连亏 → 降至1%风险)
- 仓位计算 (基于ATR的动态仓位)
- 状态持久化
"""
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("risk_engine")


@dataclass
class RiskState:
    # 账户状态
    capital: float = 150.0
    peak_capital: float = 150.0
    initial_capital: float = 150.0

    # 日统计
    today_date: str = ""
    today_start_capital: float = 150.0
    today_pnl: float = 0.0
    today_trades: int = 0

    # 月统计
    month_key: str = ""
    month_start_capital: float = 150.0
    month_pnl: float = 0.0

    # 连续亏损/连胜
    consecutive_losses: int = 0
    consecutive_wins: int = 0

    # 熔断状态
    daily_halted: bool = False
    monthly_halted: bool = False

    # 配置
    mode: str = "FIXED"  # "FIXED" or "PERCENT"
    risk_per_trade_pct: float = 0.02
    risk_per_trade_u: float = 3.0
    reduced_risk_u: float = 1.5
    reduced_risk_pct: float = 0.01
    max_daily_loss_u: float = 9.0
    max_daily_loss_pct: float = 0.06
    max_monthly_loss_u: float = 30.0
    max_monthly_dd_pct: float = 0.20
    consecutive_loss_reduce: int = 3
    consecutive_win_resume: int = 2


class RiskEngine:
    """
    风控引擎：每笔交易前检查，每笔交易后更新状态
    """

    def __init__(self, state_path: str, config: Optional[dict] = None):
        self.state_path = Path(state_path)
        self.state = self._load_state()
        if config:
            self._apply_config(config)

    def _apply_config(self, cfg: dict):
        rc = cfg.get("risk_control", {})
        if "capital" in rc and self.state.capital == 150.0:
            self.state.capital = rc["capital"]
            self.state.peak_capital = rc["capital"]
            self.state.initial_capital = rc["capital"]
            self.state.today_start_capital = rc["capital"]
            self.state.month_start_capital = rc["capital"]
        for field in ["mode", "risk_per_trade_pct", "risk_per_trade_u", "reduced_risk_u",
                      "reduced_risk_pct", "max_daily_loss_u", "max_daily_loss_pct",
                      "max_monthly_loss_u", "max_monthly_dd_pct", "consecutive_loss_reduce"]:
            if field in rc:
                setattr(self.state, field, rc[field])

    def _load_state(self) -> RiskState:
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text())
                s = RiskState(**{k: v for k, v in data.items() if k in RiskState.__dataclass_fields__})
                logger.info(f"[Risk] 已恢复状态: capital={s.capital:.2f}U")
                return s
            except Exception as e:
                logger.warning(f"[Risk] 状态加载失败({e})，使用默认值")
        return RiskState()

    def save_state(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(asdict(self.state), indent=2, ensure_ascii=False))

    def _refresh_period(self):
        """检查并刷新日/月统计周期"""
        now = datetime.now(tz=timezone.utc)
        today = now.strftime("%Y-%m-%d")
        month = now.strftime("%Y-%m")

        if self.state.today_date != today:
            logger.info(f"[Risk] 新的一天 {today}，重置日统计")
            self.state.today_date = today
            self.state.today_start_capital = self.state.capital
            self.state.today_pnl = 0.0
            self.state.today_trades = 0
            self.state.daily_halted = False

        if self.state.month_key != month:
            logger.info(f"[Risk] 新的月份 {month}，重置月统计")
            self.state.month_key = month
            self.state.month_start_capital = self.state.capital
            self.state.month_pnl = 0.0
            self.state.monthly_halted = False

    def can_trade(self) -> Tuple[bool, str]:
        """
        检查是否允许开新仓
        返回 (allowed: bool, reason: str)
        """
        self._refresh_period()

        if self.state.monthly_halted:
            dd = (self.state.month_start_capital - self.state.capital) / self.state.month_start_capital
            return False, f"月度熔断: 月内回撤{dd:.1%}≥{self.state.max_monthly_dd_pct:.0%}"

        if self.state.daily_halted:
            loss_pct = -self.state.today_pnl / self.state.today_start_capital
            return False, f"日内熔断: 今日亏损{loss_pct:.1%}≥{self.state.max_daily_loss_pct:.0%}"

        return True, "OK"

    def get_risk_pct(self) -> float:
        """返回当前风险比例（Fix v2.2: 补充缺失方法，原__main__和live_engine.py调用此方法会AttributeError）"""
        risk_u = self.get_risk_amount()
        return risk_u / max(self.state.capital, 1e-6)

    def get_risk_amount(self) -> float:
        """返回当前风险金额（U）"""
        if self.state.mode == "FIXED":
            if self.state.consecutive_losses >= self.state.consecutive_loss_reduce:
                return self.state.reduced_risk_u
            return self.state.risk_per_trade_u
        else:  # PERCENT模式
            if self.state.consecutive_losses >= self.state.consecutive_loss_reduce:
                return self.state.capital * self.state.reduced_risk_pct
            return self.state.capital * self.state.risk_per_trade_pct

    def calc_position(self, entry_price: float, sl_price: float) -> Tuple[float, float]:
        """计算开仓数量和名义仓位"""
        risk_u = self.get_risk_amount()
        sl_distance_pct = abs(entry_price - sl_price) / entry_price

        if sl_distance_pct < 1e-6:
            logger.warning("[Risk] SL距离太小")
            return 0.0, 0.0

        notional = risk_u / sl_distance_pct
        quantity = notional / entry_price
        quantity = round(quantity, 3)
        if quantity < 0.001:
            logger.warning(f"[Risk] 仓位{quantity:.4f}BTC < 0.001，调整")
            quantity = 0.001

        logger.info(f"[Risk] {self.state.mode} 风险={risk_u:.2f}U qty={quantity:.4f}BTC notional={notional:.2f}U")
        return quantity, notional

    def on_trade_open(self, direction: str, entry: float, sl: float, tp: float, qty: float):
        """开仓记录"""
        self._refresh_period()
        self.state.today_trades += 1
        self.save_state()
        logger.info(f"[Risk] 开仓: {direction} entry={entry} SL={sl} TP={tp} qty={qty:.4f}")

    def on_trade_close(self, pnl_usdt: float, outcome: str):
        """
        平仓后更新状态
        pnl_usdt: 实际盈亏（已扣手续费）
        outcome: "win" / "loss"
        """
        self._refresh_period()

        self.state.capital += pnl_usdt
        self.state.today_pnl += pnl_usdt
        self.state.month_pnl += pnl_usdt

        if self.state.capital > self.state.peak_capital:
            self.state.peak_capital = self.state.capital

        # 更新连续计数
        if outcome == "win":
            self.state.consecutive_losses = 0
            self.state.consecutive_wins += 1
        else:
            self.state.consecutive_wins = 0
            self.state.consecutive_losses += 1

        # 检查熔断 (支持FIXED/PERCENT)
        if self.state.mode == "FIXED":
            if -self.state.today_pnl >= self.state.max_daily_loss_u:
                self.state.daily_halted = True
                logger.warning(f"[Risk]熔断:今日{-self.state.today_pnl:.1f}U")
            if -self.state.month_pnl >= self.state.max_monthly_loss_u:
                self.state.monthly_halted = True
                logger.warning(f"[Risk]熔断:月内{-self.state.month_pnl:.1f}U")
        else:
            daily_loss_pct = -self.state.today_pnl / max(self.state.today_start_capital, 1e-6)
            if daily_loss_pct >= self.state.max_daily_loss_pct:
                self.state.daily_halted = True
            monthly_dd = (self.state.month_start_capital - self.state.capital) / max(self.state.month_start_capital, 1e-6)
            if monthly_dd >= self.state.max_monthly_dd_pct:
                self.state.monthly_halted = True

        # 连亏降仓提示
        if self.state.consecutive_losses >= self.state.consecutive_loss_reduce:
            logger.warning(
                f"[Risk] ⚠️ 连续亏损{self.state.consecutive_losses}笔，"
                f"风险降至{self.state.reduced_risk_pct*100:.1f}%"
            )

        # 降仓后2连胜恢复
        if (self.state.consecutive_losses < self.state.consecutive_loss_reduce and
                self.state.consecutive_wins >= self.state.consecutive_win_resume and
                self._was_reduced()):
            logger.info(f"[Risk] ✅ {self.state.consecutive_win_resume}连胜，风险恢复至{self.state.risk_per_trade_pct*100:.1f}%")

        self.save_state()

        logger.info(
            f"[Risk] 平仓 {outcome}: PnL={pnl_usdt:+.2f}U | "
            f"资金={self.state.capital:.2f}U | "
            f"今日={self.state.today_pnl:+.2f}U | "
            f"连亏={self.state.consecutive_losses} 连胜={self.state.consecutive_wins}"
        )

    def _was_reduced(self) -> bool:
        return self.state.consecutive_losses == 0 and self.state.consecutive_wins > 0

    def status_dict(self) -> dict:
        self._refresh_period()
        risk_u = self.get_risk_amount()
        risk_pct = risk_u / self.state.capital if self.state.mode=="FIXED" else risk_u
        can, reason = self.can_trade()
        monthly_dd = (self.state.month_start_capital - self.state.capital) / max(self.state.month_start_capital, 1e-6)
        daily_loss = -self.state.today_pnl / max(self.state.today_start_capital, 1e-6)
        return {
            "capital": round(self.state.capital, 2),
            "peak": round(self.state.peak_capital, 2),
            "today_pnl": round(self.state.today_pnl, 2),
            "month_pnl": round(self.state.month_pnl, 2),
            "daily_loss_pct": round(daily_loss, 4),
            "monthly_dd_pct": round(monthly_dd, 4),
            "current_risk_pct": risk_pct,
            "consecutive_losses": self.state.consecutive_losses,
            "consecutive_wins": self.state.consecutive_wins,
            "can_trade": can,
            "halt_reason": reason if not can else None,
        }


if __name__ == "__main__":
    import tempfile, os
    logging.basicConfig(level=logging.INFO)

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        state_file = f.name

    cfg = {"risk_control": {"capital": 150.0, "risk_per_trade_pct": 0.02,
                             "max_daily_loss_pct": 0.06, "max_monthly_dd_pct": 0.20,
                             "consecutive_loss_reduce": 3, "reduced_risk_pct": 0.01}}
    risk = RiskEngine(state_file, cfg)

    print("=== 风控引擎单元测试 ===")
    print(f"初始状态: {risk.status_dict()}")

    # 测试仓位计算
    qty, notional = risk.calc_position(81000, 80763)  # SL=1×ATR≈237
    print(f"仓位计算: qty={qty:.4f}BTC notional={notional:.2f}U")

    # 模拟3连亏
    for i in range(3):
        risk.on_trade_close(-3.0, "loss")
    print(f"3连亏后风险: {risk.get_risk_pct()*100:.1f}%")
    print(f"可交易: {risk.can_trade()}")

    # 模拟2连胜
    for i in range(2):
        risk.on_trade_close(2.4, "win")
    print(f"2连胜后风险: {risk.get_risk_pct()*100:.1f}%")

    # 测试日内熔断
    risk.on_trade_close(-10.0, "loss")
    print(f"大亏后: {risk.can_trade()}")
    print(f"最终状态: {risk.status_dict()}")
    os.unlink(state_file)
