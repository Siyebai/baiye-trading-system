#!/usr/bin/env python3
import json, numpy as np, pandas as pd
from dataclasses import dataclass
import warnings
warnings.filterwarnings('ignore')

@dataclass
class Signal:
    dir: str; tf: str; price: float; sl: float; tp: float; conf: float

class Indicators:
    @staticmethod
    def ema(s, n): return s.ewm(span=n, adjust=False).mean()
    @staticmethod
    def rsi(s, n=14):
        delta = s.diff()
        up, down = delta.clip(lower=0), -delta.clip(upper=0)
        return 100 - (100 / (1 + up.rolling(n).mean() / down.rolling(n).mean().replace(0, np.nan)))
    @staticmethod
    def bb(close, n=20, k=2):
        mid = close.rolling(n).mean()
        return (close - (mid - k*close.rolling(n).std())) / (2*k*close.rolling(n).std())
    @staticmethod
    def macd_hist(close):
        ema_f = close.ewm(span=12, adjust=False).mean()
        ema_s = close.ewm(span=26, adjust=False).mean()
        return (ema_f - ema_s) - (ema_f - ema_s).ewm(span=9, adjust=False).mean()
    @staticmethod
    def adx(high, low, close, n=14):
        tr = np.maximum(high-low, np.abs(high-close.shift(1)), np.abs(low-close.shift(1)))
        atr = tr.rolling(n).mean()
        plus_dm = high.diff()
        minus_dm = -low.diff()
        plus_dm = plus_dm.where((plus_dm>minus_dm)&(plus_dm>0), 0)
        minus_dm = minus_dm.where((minus_dm>plus_dm)&(minus_dm>0), 0)
        plus_di = 100*(plus_dm.rolling(n).mean()/atr)
        minus_di = 100*(minus_dm.rolling(n).mean()/atr)
        dx = 100*np.abs(plus_di-minus_di)/(plus_di+minus_di)
        return dx.rolling(n).mean()

class Scanner:
    CFG = {
        'rsi_os': 35, 'rsi_ob': 65, 'rsi_exit': 45, 'bb_long': 0.25, 'bb_short': 0.75,
        'vol_spike': 1.5, 'conf_th': 0.45, 'sl_pct': 0.006, 'tp_pct': 0.005,
        'timeout_sec': 300, 'adx_min': 25, 'use_adx_filter': True,
    }
    def _features(self, df):
        df = df.copy()
        df['ema_f'], df['ema_s'] = Indicators.ema(df['close'], 20), Indicators.ema(df['close'], 50)
        df['rsi'] = Indicators.rsi(df['close'], 14)
        df['bb_pct'] = Indicators.bb(df['close'], 20, 2)
        df['vol_ratio'] = df['volume'] / df['volume'].rolling(20).mean()
        df['macd_h'] = Indicators.macd_hist(df['close'])
        df['adx'] = Indicators.adx(df['high'], df['low'], df['close'], 14)
        return df
    def get_signal(self, df, tf):
        if len(df) < 50: return Signal('HOLD', tf, 0, 0, 0, 0)
        feat = self._features(df)
        last, prev = feat.iloc[-1], feat.iloc[-2]
        price = last['close']
        # ADX 趋势过滤
        if self.CFG.get('use_adx_filter') and last['adx'] < self.CFG['adx_min']:
            return Signal('HOLD', tf, 0, 0, 0, 0)
        long = 0.0
        if last['rsi'] < self.CFG['rsi_os']: long += 0.25
        elif last['rsi'] < self.CFG['rsi_exit']: long += 0.15
        if last['bb_pct'] < self.CFG['bb_long']: long += 0.25
        if last['vol_ratio'] > self.CFG['vol_spike']: long += 0.20
        if prev['macd_h'] < 0 and last['macd_h'] > 0: long += 0.20
        short = 0.0
        if last['rsi'] > self.CFG['rsi_ob']: short += 0.25
        elif last['rsi'] > 100 - self.CFG['rsi_exit']: short += 0.15
        if last['bb_pct'] > self.CFG['bb_short']: short += 0.25
        if last['vol_ratio'] > self.CFG['vol_spike']: short += 0.20
        if prev['macd_h'] > 0 and last['macd_h'] < 0: short += 0.20
        if long >= self.CFG['conf_th'] and long >= short:
            return Signal('LONG', tf, price, price*(1-self.CFG['sl_pct']), price*(1+self.CFG['tp_pct']), long)
        if short >= self.CFG['conf_th']:
            return Signal('SHORT', tf, price, price*(1+self.CFG['sl_pct']), price*(1-self.CFG['tp_pct']), short)
        return Signal('HOLD', tf, 0, 0, 0, 0)

def backtest(df_1m, scanner, capital=10000.0):
    pos, trades, equity = None, [], [capital]
    tf_data = {}
    for tf in ['3m','5m','10m','15m','30m']:
        r = df_1m.resample(f'{int(tf[:-1])}min')
        tf_data[tf] = pd.DataFrame({'open':r['open'].first(),'high':r['high'].max(),'low':r['low'].min(),'close':r['close'].last(),'volume':r['volume'].sum()}).dropna()
    all_times = sorted(set().union(*[tf_data[tf].index for tf in tf_data]))
    for t in all_times:
        best_sig = None
        for tf, df_tf in tf_data.items():
            if t not in df_tf.index: continue
            loc = df_tf.index.get_loc(t)
            if loc >= 20:
                sig = scanner.get_signal(df_tf.iloc[:loc+1], tf)
                if sig.dir != 'HOLD' and (best_sig is None or sig.conf > best_sig.conf): best_sig = sig
        if pos:
            price = df_1m.loc[t]['close'] if t in df_1m.index else None
            if price:
                exit_reason = None
                if pos['dir']=='LONG':
                    if price <= pos['sl']: exit_reason='SL'
                    elif price >= pos['tp']: exit_reason='TP'
                else:
                    if price >= pos['sl']: exit_reason='SL'
                    elif price <= pos['tp']: exit_reason='TP'
                if not exit_reason and (t-pos['time']).total_seconds()>scanner.CFG['timeout_sec']: exit_reason='TIMEOUT'
                if exit_reason:
                    pnl = (price-pos['entry'])/pos['entry']*100 if pos['dir']=='LONG' else (pos['entry']-price)/pos['entry']*100
                    capital *= (1+pnl/100)
                    trades.append({'dir':pos['dir'],'pnl':pnl,'exit':exit_reason,'tf':pos['tf']})
                    pos, equity = None, equity + [capital]
        if not pos and best_sig:
            pos = {'dir':best_sig.dir,'entry':best_sig.price,'sl':best_sig.sl,'tp':best_sig.tp,'time':t,'tf':best_sig.tf}
        capital -= capital * 0.0009
        equity.append(capital)
    if pos:
        final_price = df_1m['close'].iloc[-1]
        pnl = (final_price-pos['entry'])/pos['entry']*100 if pos['dir']=='LONG' else (pos['entry']-final_price)/pos['entry']*100
        capital *= (1+pnl/100)
        trades.append({'dir':pos['dir'],'pnl':pnl,'exit':'FORCED','tf':pos['tf']})
        equity.append(capital)
    wr = len([t for t in trades if t['pnl']>0])/len(trades)*100 if trades else 0
    returns = pd.Series(equity).pct_change().dropna()
    sharpe = np.sqrt(365*1440)*returns.mean()/returns.std() if returns.std() else 0
    dd = (np.maximum.accumulate(equity)-equity).max()/max(equity)*100 if max(equity)>0 else 0
    wins, losses = [t['pnl'] for t in trades if t['pnl']>0], [t['pnl'] for t in trades if t['pnl']<=0]
    return {'trades':len(trades),'win_rate':wr,'return':(capital-10000)/10000*100,'max_dd':dd,'sharpe':sharpe,
            'avg_win':np.mean(wins) if wins else 0,'avg_loss':np.mean(losses) if losses else 0,'trades_detail':trades}

def test_session(df, name):
    scanner = Scanner()
    r = backtest(df, scanner)
    pf = abs(r['avg_win']*len([t for t in r['trades_detail'] if t['pnl']>0]) / (r['avg_loss']*len([t for t in r['trades_detail'] if t['pnl']<=0]))) if r['avg_loss']!=0 else 0
    print(f"\n【{name}】")
    print(f"  K 线数:{len(df)} | 交易:{r['trades']} | 胜率:{r['win_rate']:.1f}% | 收益:{r['return']:.2f}% | 回撤:{r['max_dd']:.2f}%")
    print(f"  盈亏比:{abs(r['avg_win']/r['avg_loss']):.2f} | 盈利因子:{pf:.2f}" if r['avg_loss']!=0 else "  无亏损")
    return r

def load_data(filename):
    with open(filename) as f: data = json.load(f)
    df = pd.DataFrame(data, columns=['ts','open','high','low','close','volume']+['x']*6)
    df[['open','high','low','close','volume']] = df[['open','high','low','close','volume']].astype(float)
    df.index = pd.to_datetime(df['ts'], unit='ms')
    return df

if __name__ == '__main__':
    print("="*60)
    print("策略 B+C: ETH + ADX 趋势过滤")
    print("="*60)
    
    # 测试 ETH
    df_eth = load_data('eth_1m.json')
    print(f"\nETH 数据：{df_eth.index[0]} ~ {df_eth.index[-1]} | {len(df_eth)} 根 K 线")
    n = len(df_eth)
    e1 = test_session(df_eth.iloc[:n//3], "ETH 亚盘")
    e2 = test_session(df_eth.iloc[n//3:2*n//3], "ETH 欧盘")
    e3 = test_session(df_eth.iloc[2*n//3:], "ETH 美盘")
    
    # 汇总 ETH
    total_eth = e1['trades']+e2['trades']+e3['trades']
    wins_eth = len([t for t in e1['trades_detail']+e2['trades_detail']+e3['trades_detail'] if t['pnl']>0])
    print(f"\n【ETH 汇总】总交易:{total_eth} | 总胜率:{wins_eth/total_eth*100:.1f}%" if total_eth>0 else "【ETH 汇总】无交易")
    
    # 对比 BTC（无 ADX 过滤）
    print("\n"+"="*60)
    print("对比：BTC 无 ADX 过滤")
    print("="*60)
    df_btc = load_data('btc_1m.json')
    print(f"\nBTC 数据：{df_btc.index[0]} ~ {df_btc.index[-1]} | {len(df_btc)} 根 K 线")
    n = len(df_btc)
    b1 = test_session(df_btc.iloc[:n//3], "BTC 亚盘")
    b2 = test_session(df_btc.iloc[n//3:2*n//3], "BTC 欧盘")
    b3 = test_session(df_btc.iloc[2*n//3:], "BTC 美盘")
    
    total_btc = b1['trades']+b2['trades']+b3['trades']
    wins_btc = len([t for t in b1['trades_detail']+b2['trades_detail']+b3['trades_detail'] if t['pnl']>0])
    print(f"\n【BTC 汇总】总交易:{total_btc} | 总胜率:{wins_btc/total_btc*100:.1f}%" if total_btc>0 else "【BTC 汇总】无交易")
