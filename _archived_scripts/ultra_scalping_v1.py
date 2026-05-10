#!/usr/bin/env python3
"""
极简订单流超短线策略 v1.0
==========================
核心指标：主动成交量 + 价格突破 + 成交量突增
拒绝滞后指标：RSI/MACD/布林带/KDJ/均线
"""
import json, numpy as np, pandas as pd
from dataclasses import dataclass
from typing import Dict, List, Optional
import warnings
warnings.filterwarnings('ignore')

@dataclass
class Signal:
    direction: str  # 'LONG'/'SHORT'
    symbol: str
    tf: str
    entry: float
    sl: float
    tp: float
    confidence: float
    reason: str

class OrderFlowStrategy:
    """极简订单流策略 - 只用 3 个原始指标"""
    
    # 品种参数配置（基于波动率特性）- 优化版：只做 5m，提高阈值
    PARAMS = {
        'BTCUSDT': {'vol_spike':3.0, 'break_period':15, 'tp_pct':0.008, 'sl_pct':0.006, 'max_bars':3, 'net_vol_thresh':3.0},
        'ETHUSDT': {'vol_spike':3.0, 'break_period':15, 'tp_pct':0.010, 'sl_pct':0.007, 'max_bars':3, 'net_vol_thresh':3.0},
        'SOLUSDT': {'vol_spike':3.5, 'break_period':12, 'tp_pct':0.012, 'sl_pct':0.010, 'max_bars':3, 'net_vol_thresh':3.5},
        'BNBUSDT': {'vol_spike':2.8, 'break_period':15, 'tp_pct':0.006, 'sl_pct':0.005, 'max_bars':3, 'net_vol_thresh':2.8},
        'XRPUSDT': {'vol_spike':3.0, 'break_period':12, 'tp_pct':0.010, 'sl_pct':0.008, 'max_bars':3, 'net_vol_thresh':3.0},
    }
    
    def __init__(self, symbol: str, use_taker_vol: bool = False):
        self.symbol = symbol
        self.params = self.PARAMS.get(symbol, self.PARAMS['BTCUSDT'])
        self.use_taker_vol = use_taker_vol  # 是否有主动成交量数据
        
    def check_signal(self, df: pd.DataFrame, tf: str) -> Optional[Signal]:
        """检查入场信号"""
        if len(df) < self.params['break_period'] + 5:
            return None
            
        p = self.params
        price = df['close'].iloc[-1]
        high = df['high'].iloc[-1]
        low = df['low'].iloc[-1]
        
        # 1. 价格突破检查
        prev_high = df['high'].iloc[-p['break_period']:-1].max()
        prev_low = df['low'].iloc[-p['break_period']:-1].min()
        
        breakout_long = high > prev_high
        breakout_short = low < prev_low
        
        # 2. 成交量突增检查
        vol = df['volume'].iloc[-1]
        vol_ma = df['volume'].iloc[-21:-1].mean()
        vol_spike = vol > vol_ma * p['vol_spike']
        
        # 3. 主动成交量净额（如果有数据）
        net_vol_signal = 0
        if self.use_taker_vol and 'taker_buy_vol' in df.columns:
            net_vol = df['taker_buy_vol'].iloc[-1] - df['taker_sell_vol'].iloc[-1]
            net_vol_ma = abs(df['taker_buy_vol'].iloc[-21:-1] - df['taker_sell_vol'].iloc[-21:-1]).mean()
            if net_vol > net_vol_ma * p['net_vol_thresh']:
                net_vol_signal = 1
            elif net_vol < -net_vol_ma * p['net_vol_thresh']:
                net_vol_signal = -1
        
        # 信号生成逻辑
        confidence = 0.0
        direction = None
        
        if breakout_long and vol_spike:
            confidence = 0.5
            if net_vol_signal > 0:
                confidence = 0.75
                direction = 'LONG'
            elif net_vol_signal == 0:
                confidence = 0.55
                direction = 'LONG'
                
        if breakout_short and vol_spike:
            conf = 0.5
            if net_vol_signal < 0:
                conf = 0.75
            elif net_vol_signal == 0:
                conf = 0.55
            if conf > confidence:
                confidence = conf
                direction = 'SHORT'
        
        if direction is None or confidence < 0.5:
            return None
            
        entry = price
        sl = entry * (1 - p['sl_pct']) if direction == 'LONG' else entry * (1 + p['sl_pct'])
        tp = entry * (1 + p['tp_pct']) if direction == 'LONG' else entry * (1 - p['tp_pct'])
        
        reason = f"breakout={'long' if breakout_long else 'short' if breakout_short else 'none'}|vol_spike={vol_spike}|net_vol={net_vol_signal}"
        return Signal(direction, self.symbol, tf, entry, sl, tp, confidence, reason)


def backtest(df: pd.DataFrame, symbol: str, initial_capital=10000.0, use_taker_vol=False) -> dict:
    """回测引擎"""
    strategy = OrderFlowStrategy(symbol, use_taker_vol)
    capital = initial_capital
    pos = None
    trades = []
    equity = [capital]
    
    # 多时间框架：只测 5m（1m/3m 噪音大）
    results = {}
    for tf_name, minutes in [('5m',5)]:
        # 聚合 K 线
        agg = df.resample(f'{minutes}min').agg({
            'open':'first', 'high':'max', 'low':'min', 'close':'last', 'volume':'sum'
        }).dropna()
        
        if use_taker_vol and 'taker_buy_vol' in df.columns:
            agg['taker_buy_vol'] = df['taker_buy_vol'].resample(f'{minutes}min').sum()
            agg['taker_sell_vol'] = df['taker_sell_vol'].resample(f'{minutes}min').sum()
        
        capital_tf = initial_capital  # 单一时间框架
        pos_tf = None
        trades_tf = []
        
        for i, (t, row) in enumerate(agg.iterrows()):
            # 检查出场
            if pos_tf:
                exit_reason = None
                if pos_tf['dir'] == 'LONG':
                    if row['low'] <= pos_tf['sl']: exit_reason = 'SL'
                    elif row['high'] >= pos_tf['tp']: exit_reason = 'TP'
                else:
                    if row['high'] >= pos_tf['sl']: exit_reason = 'SL'
                    elif row['low'] <= pos_tf['tp']: exit_reason = 'TP'
                # 时间止损
                if not exit_reason and i - pos_tf['bar_idx'] >= strategy.params['max_bars']:
                    exit_reason = 'TIMEOUT'
                    
                if exit_reason:
                    pnl = (row['close']-pos_tf['entry'])/pos_tf['entry'] if pos_tf['dir']=='LONG' else (pos_tf['entry']-row['close'])/pos_tf['entry']
                    capital_tf *= (1 + pnl - 0.0009*2)  # 扣除双边手续费
                    trades_tf.append({'dir':pos_tf['dir'],'pnl':pnl*100,'exit':exit_reason,'tf':tf_name})
                    pos_tf = None
            
            # 检查入场
            if not pos_tf:
                sig = strategy.check_signal(agg.iloc[:i+1], tf_name)
                if sig:
                    pos_tf = {'dir':sig.direction,'entry':sig.entry,'sl':sig.sl,'tp':sig.tp,'bar_idx':i,'tf':tf_name}
        
        # 强制平仓
        if pos_tf:
            final_price = agg['close'].iloc[-1]
            pnl = (final_price-pos_tf['entry'])/pos_tf['entry'] if pos_tf['dir']=='LONG' else (pos_tf['entry']-final_price)/pos_tf['entry']
            capital_tf *= (1 + pnl - 0.0009*2)
            trades_tf.append({'dir':pos_tf['dir'],'pnl':pnl*100,'exit':'FORCED','tf':tf_name})
        
        results[tf_name] = {
            'capital': capital_tf,
            'trades': trades_tf,
            'return': (capital_tf - initial_capital/3) / (initial_capital/3) * 100
        }
    
    # 汇总
    total_capital = sum(r['capital'] for r in results.values())
    all_trades = []
    for tf, r in results.items():
        for t in r['trades']:
            all_trades.append(t)
    
    wins = [t for t in all_trades if t['pnl']>0]
    wr = len(wins)/len(all_trades)*100 if all_trades else 0
    avg_win = np.mean([t['pnl'] for t in wins]) if wins else 0
    avg_loss = np.mean([t['pnl'] for t in all_trades if t['pnl']<=0]) if [t for t in all_trades if t['pnl']<=0] else 0
    
    return {
        'symbol': symbol,
        'total_return': (total_capital-initial_capital)/initial_capital*100,
        'total_trades': len(all_trades),
        'win_rate': wr,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'profit_factor': abs(avg_win*len(wins)/(avg_loss*(len(all_trades)-len(wins)))) if avg_loss!=0 and len(all_trades)>len(wins) else 0,
        'by_tf': {tf: r['return'] for tf,r in results.items()},
        'trades_detail': all_trades
    }


def load_klines(filename: str) -> pd.DataFrame:
    """加载 K 线数据"""
    with open(filename) as f:
        data = json.load(f)
    
    if isinstance(data[0], list):
        df = pd.DataFrame(data, columns=['ts','open','high','low','close','volume','close_time','quote_vol','trades','taker_buy_vol','taker_buy_quote','ignore'])
    else:
        df = pd.DataFrame(data)
    
    for col in ['open','high','low','close','volume']:
        df[col] = df[col].astype(float)
    if 'taker_buy_vol' in df.columns:
        df['taker_buy_vol'] = df['taker_buy_vol'].astype(float)
        df['taker_sell_vol'] = df['volume'] - df['taker_buy_vol']
    
    df.index = pd.to_datetime(df['ts'] if 'ts' in df.columns else df['open_time'], unit='ms')
    return df


if __name__ == '__main__':
    import sys
    
    print("="*70)
    print("极简订单流超短线策略 v1.0 - 多品种回测")
    print("="*70)
    
    results = []
    symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT']
    
    for sym in symbols:
        # 尝试多种文件名
        found = False
        for fname in [f'{sym.lower()}_7d.json', f'{sym.lower()}_1m.json', 
                      f'{sym.replace("USDT","")}_1m.json', f'{sym.replace("USDT","")}_7d.json',
                      f'btc_1m.json', f'eth_1m.json']:
            try:
                df = load_klines(fname)
                print(f"已加载 {fname}")
                found = True
                break
            except:
                continue
        if not found:
            print(f"⚠️ {sym} 数据文件未找到，跳过")
            continue
        
        print(f"\n【{sym}】")
        print(f"数据：{df.index[0]} ~ {df.index[-1]} | {len(df)} 根 K 线")
        r = backtest(df, sym, 10000, use_taker_vol=True)
        print(f"总收益：{r['total_return']:.2f}% | 交易：{r['total_trades']} | 胜率：{r['win_rate']:.1f}%")
        pf = abs(r['avg_win']/r['avg_loss']) if r['avg_loss']!=0 else 0
        print(f"盈亏比：{pf:.2f} | 分 TF 收益：{r['by_tf']}")
        results.append(r)
    
    # 汇总对比
    print("\n" + "="*70)
    print("【策略对比 - 5m 周期】")
    print(f"{'品种':<12} {'收益%':>10} {'交易数':>8} {'胜率%':>8} {'盈亏比':>8}")
    print("-"*55)
    for r in results:
        pf = abs(r['avg_win']/r['avg_loss']) if r['avg_loss']!=0 else 0
        print(f"{r['symbol']:<12} {r['total_return']:>10.2f} {r['total_trades']:>8} {r['win_rate']:>8.1f} {pf:>8.2f}")
    print("="*70)
    
    # 推荐
    best = max(results, key=lambda x: x['total_return'])
    print(f"\n【推荐品种】{best['symbol']} — 收益{best['total_return']:.2f}%, 胜率{best['win_rate']:.1f}%")
