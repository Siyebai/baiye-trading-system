"""
白夜系统短线信号研究 v1.0
研究日期: 2026-05-08
周期: 1m/3m/5m/15m 多周期共振
核心策略: 快速MACD + RSI + 三周期方向共振 + VWAP
"""
import requests, json, os, pickle
import numpy as np, pandas as pd
import ta

API = "https://api.binance.com/api/v3/klines"
SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","LINKUSDT","BNBUSDT"]
INTERVALS = ["1m","3m","5m","15m"]

def fetch_klines(symbol, interval, limit=500):
    r = requests.get(API, params={"symbol":symbol,"interval":interval,"limit":limit}, timeout=15)
    r.raise_for_status()
    return r.json()

def to_df(raw):
    df = pd.DataFrame(raw, columns=[
        "ts","open","high","low","close","volume",
        "close_time","quote_vol","trades","taker_buy_base","taker_buy_quote","ignore"])
    for c in ["open","high","low","close","volume","quote_vol","taker_buy_base","taker_buy_quote"]:
        df[c] = df[c].astype(float)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    return df.set_index("ts").sort_index()

def add_indicators(df):
    c=df["close"]; h=df["high"]; l=df["low"]; v=df["volume"]
    df["rsi7"]     = ta.momentum.RSIIndicator(c,7).rsi()
    df["rsi14"]    = ta.momentum.RSIIndicator(c,14).rsi()
    macd = ta.trend.MACD(c, window_slow=13, window_fast=5, window_sign=5)
    df["macd"]     = macd.macd()
    df["macd_sig"] = macd.macd_signal()
    df["macd_hist"]= macd.macd_diff()
    bb = ta.volatility.BollingerBands(c,20,2)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_mid"]   = bb.bollinger_mavg()
    df["bb_width"] = (df["bb_upper"]-df["bb_lower"])/df["bb_mid"]
    df["atr7"]     = ta.volatility.AverageTrueRange(h,l,c,7).average_true_range()
    df["ema9"]     = ta.trend.EMAIndicator(c,9).ema_indicator()
    df["ema21"]    = ta.trend.EMAIndicator(c,21).ema_indicator()
    tp = (h+l+c)/3
    df["vwap"]     = (tp*v).rolling(50).sum() / v.rolling(50).sum()
    df["vol_ma20"] = v.rolling(20).mean()
    df["vol_ratio"]= v / df["vol_ma20"]
    df["above_vwap"]=(c>df["vwap"]).astype(int)
    df["above_ema21"]=(c>df["ema21"]).astype(int)
    return df.dropna()

def generate_signals(df5, df3, df15):
    """核心短线信号生成器"""
    macd15 = df15["macd_hist"].reindex(df5.index, method="ffill")
    macd3  = df3["macd_hist"].reindex(df5.index, method="ffill")
    macd5  = df5["macd_hist"]

    cross_up   = (macd5>0) & (macd5.shift(1)<=0)
    cross_down = (macd5<0) & (macd5.shift(1)>=0)
    agree_bull = (macd15>0) & (macd3>0)
    agree_bear = (macd15<0) & (macd3<0)
    rsi_bull   = (df5["rsi14"]>40) & (df5["rsi14"]<68)
    rsi_bear   = (df5["rsi14"]>32) & (df5["rsi14"]<60)

    sig = pd.Series(0, index=df5.index)
    sig[cross_up   & agree_bull & rsi_bull] = 1
    sig[cross_down & agree_bear & rsi_bear] = -1
    return sig

if __name__ == "__main__":
    os.makedirs("data/realtime", exist_ok=True)
    print("获取实时数据...")
    data = {}
    for sym in SYMBOLS:
        data[sym] = {}
        for iv in INTERVALS:
            try:
                raw = fetch_klines(sym, iv)
                data[sym][iv] = add_indicators(to_df(raw))
                print(f"  ✅ {sym} {iv}: {len(data[sym][iv])}根K线")
            except Exception as e:
                print(f"  ❌ {sym} {iv}: {e}")
    print("数据获取完成")
