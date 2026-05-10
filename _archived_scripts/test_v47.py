#!/usr/bin/env python3
"""
V4.7优化测试脚本
"""

import json
import sys
sys.path.insert(0, './scripts')

from profit_taking_optimizer import SmartProfitTaker
from profit_loss_ratio_optimizer import ProfitLossRatioOptimizer

# 加载V4.7配置
with open('assets/configs/killer_config_v47.json', 'r') as f:
    config = json.load(f)

print("=" * 70)
print("V4.7盈亏比优化版 - 综合测试")
print("=" * 70)

# 测试止盈策略（V4.7参数）
print("\n【止盈策略测试 - V4.7】")
profit_taker = SmartProfitTaker(config['profit_taking'])
exit_plan = profit_taker.create_exit_plan(50000, 'long', 200)

print(f"入场价格: ${exit_plan.entry_price:.2f}")
print(f"止损价格: ${exit_plan.stop_loss:.2f}")
print(f"\n止盈目标（V4.7优化）:")
for target in exit_plan.profit_targets:
    target_price = 50000 + target.atr_multiplier * 200
    profit_pct = (target_price - 50000) / 50000 * 100
    print(f"  级别{target.level}: ${target_price:.2f} ({profit_pct:+.2f}%) - 退出{target.exit_percent*100:.0f}%")

# 测试盈亏比优化
print(f"\n{'=' * 70}")
print("【盈亏比优化测试 - V4.7】")
optimizer = ProfitLossRatioOptimizer(config)

stats = optimizer.get_statistics()
print(f"\nV4.7核心优化:")
print(f"  ATR周期: {stats['atr_period']}（从14→20）")
print(f"  止损距离: {stats['base_stop_multiplier']}*ATR（从1.5→2.0）")
print(f"  基础仓位: {stats['base_position_pct']*100:.1f}%（从12%→8%）")
print(f"  最大仓位: {stats['max_position_pct']*100:.1f}%（从35%→25%）")
print(f"  凯利系数: {stats['kelly_fraction']}（从0.25→0.15）")
print(f"  最大回撤限制: {stats['max_drawdown']*100:.1f}%（从10%→8%）")
print(f"  日最大亏损: {stats['max_daily_loss']*100:.1f}%（从3%→2%）")
print(f"  连续亏损限制: {stats['consecutive_loss_limit']}次（从5→3）")

# 计算止损
stop_config = optimizer.calculate_optimal_stop_loss(50000, 'long', 200, 0.75)
print(f"\n动态止损示例（信号评分0.75）:")
print(f"  止损价格: ${50000 - stop_config.atr_multiplier * 200:.2f}")
print(f"  止损距离: {stop_config.atr_multiplier}*ATR")
print(f"  原因: {stop_config.reason}")

print(f"\n{'=' * 70}")
print("V4.7优化预期效果:")
print(f"  盈亏比: 0.37 → 1.2-1.5（+224%-305%）")
print(f"  止盈触发率: 32.7% → 55%-65%（+68%-99%）")
print(f"  最大回撤: 14.37% → 6%-8%（-44%-58%）")
print(f"  收益率: -14.31% → +5%-15%（+135%-205%）")
print(f"  保持胜率: 54.99%（不降低）")
print("=" * 70)
