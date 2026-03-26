import os
import sys
import time
import logging
import pandas as pd
import yfinance as yf
import requests
import pytz
from datetime import datetime

# ====================== Configuration ======================
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
if not TOKEN or not CHAT_ID:
    print("❌ Missing TOKEN or CHAT_ID. Set them in Railway variables.")
    sys.exit(1)

PAIRS = ["GC=F", "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X", "NZDUSD=X"]
KILLZONES = {
    "Asian": (20, 22),      # 8-10 PM EST
    "London": (2, 5),       # 2-5 AM EST
    "NewYork": (7, 9),      # 7-9 AM EST
    "LondonClose": (10, 12) # 10-12 PM EST
}
SLEEP_INTERVAL = 900  # seconds (15 minutes)
STOP_DISTANCE = {
    "GC=F": 2.0,
    "default": 0.0005
}

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.FileHandler('ict_bot.log'), logging.StreamHandler()])

# ====================== Helper Classes ======================
class DataFetcher:
    """Fetch and cache market data with yfinance."""
    def __init__(self):
        self.cache = {}

    def get_data(self, symbol, interval, period):
        """Return DataFrame for given symbol, interval, period."""
        key = f"{symbol}_{interval}_{period}"
        # Check cache
        if key in self.cache:
            df = self.cache[key]
            if not df.empty and (datetime.now() - df.index[-1].to_pydatetime()).seconds < 120:
                return df
        try:
            df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=False)
        except Exception as e:
            logging.error(f"yfinance error for {symbol} {interval}: {e}")
            return pd.DataFrame()
        if df.empty:
            logging.warning(f"No data for {symbol} {interval}")
            return pd.DataFrame()
        # Flatten MultiIndex columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        self.cache[key] = df
        return df

class ICTIndicators:
    """Detect ICT patterns from price data."""
    @staticmethod
    def detect_fvg(df):
        """Identify Fair Value Gaps (3‑candle pattern)."""
        fvgs = []
        for i in range(2, len(df)):
            if df['Low'].iloc[i] > df['High'].iloc[i-2]:
                fvgs.append({'type': 'bullish', 'top': df['High'].iloc[i-2], 'bottom': df['Low'].iloc[i]})
            elif df['High'].iloc[i] < df['Low'].iloc[i-2]:
                fvgs.append({'type': 'bearish', 'top': df['High'].iloc[i], 'bottom': df['Low'].iloc[i-2]})
        return fvgs

    @staticmethod
    def detect_displacement(df, lookback=10, body_ratio=0.65):
        """Check if last candle shows displacement (large body relative to range)."""
        if len(df) < lookback + 1:
            return False
        body = abs(df['Close'].iloc[-1] - df['Open'].iloc[-1])
        rng = df['High'].iloc[-1] - df['Low'].iloc[-1]
        if rng == 0:
            return False
        body_pct = body / rng
        avg_body = df['Close'].diff().abs().rolling(lookback).mean().iloc[-1]
        return body_pct > body_ratio and body > avg_body

    @staticmethod
    def detect_mss(df):
        """Market Structure Shift: break of recent swing high/low."""
        if len(df) < 20:
            return {'bullish': False, 'bearish': False}
        # Use a rolling window to find recent swing points
        highs = df['High'].rolling(5).max()
        lows = df['Low'].rolling(5).min()
        last_high = highs.iloc[-3]
        last_low = lows.iloc[-3]
        return {
            'bullish': df['Low'].iloc[-1] < last_low,   # break below last low
            'bearish': df['High'].iloc[-1] > last_high  # break above last high
        }

    @staticmethod
    def equilibrium(df, window=20):
        """Calculate equilibrium as mid of recent range."""
        return (df['High'].tail(window).max() + df['Low'].tail(window).min()) / 2

class TelegramSender:
    """Send messages via Telegram (text only)."""
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id

    def send_text(self, text):
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            requests.post(url, json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
        except Exception as e:
            logging.error(f"Telegram send error: {e}")

class ICTBot:
    def __init__(self):
        self.fetcher = DataFetcher()
        self.sender = TelegramSender(TOKEN, CHAT_ID)
        self.indicators = ICTIndicators()

    def is_killzone(self):
        """Return True if current time falls into any killzone (EST)."""
        now = datetime.now(pytz.timezone('America/New_York'))
        hour = now.hour
        for start, end in KILLZONES.values():
            if start <= hour < end:
                return True
        return False

    def get_bias(self, df_higher):
        """Determine bullish/bearish bias from higher timeframe."""
        if len(df_higher) < 20:
            return 'neutral'
        # Simple: compare current close to 20-period SMA
        sma = df_higher['Close'].rolling(20).mean().iloc[-1]
        if df_higher['Close'].iloc[-1] > sma:
            return 'bullish'
        elif df_higher['Close'].iloc[-1] < sma:
            return 'bearish'
        return 'neutral'

    def analyze_pair(self, symbol):
        """Run full analysis for one symbol and send signal if conditions met."""
        try:
            # Fetch data
            df_daily = self.fetcher.get_data(symbol, "1d", "10d")
            df_1h = self.fetcher.get_data(symbol, "1h", "10d")
            df_15m = self.fetcher.get_data(symbol, "15m", "5d")

            if df_daily.empty or df_1h.empty or df_15m.empty:
                logging.warning(f"Insufficient data for {symbol}")
                return

            # Previous day high/low
            prev_high = df_daily['High'].iloc[-2]
            prev_low = df_daily['Low'].iloc[-2]

            # Detect patterns
            fvg_list = self.indicators.detect_fvg(df_15m)
            mss = self.indicators.detect_mss(df_15m)
            displacement = self.indicators.detect_displacement(df_15m)
            equilibrium = self.indicators.equilibrium(df_15m)
            current_price = df_15m['Close'].iloc[-1]
            killzone = self.is_killzone()
            bias = self.get_bias(df_1h)

            # Liquidity sweeps
            sweep_high = df_15m['High'].iloc[-1] > prev_high
            sweep_low = df_15m['Low'].iloc[-1] < prev_low

            # --- Buy conditions ---
            if (sweep_low and mss['bullish'] and displacement and
                any(f['type'] == 'bullish' for f in fvg_list[-3:]) and
                current_price < equilibrium and bias == 'bullish' and killzone):
                # Find the most recent bullish FVG
                fvg = next(f for f in reversed(fvg_list) if f['type'] == 'bullish')
                entry = fvg['bottom']
                stop = df_15m['Low'].iloc[-1] - STOP_DISTANCE.get(symbol, STOP_DISTANCE['default'])
                tp1 = equilibrium
                tp2 = prev_high
                rr = (tp2 - entry) / (entry - stop) if (entry - stop) != 0 else 0

                gold_tag = "🔥 GOLD SIGNAL" if symbol == "GC=F" else ""
                msg = (f"🚀 *ICT PRO - BUY SIGNAL* {gold_tag}\n"
                       f"📍 {symbol}\n"
                       f"🔥 Sweep PDL + MSS + Displacement\n"
                       f"🎯 Entry: {entry:.5f}\n"
                       f"🛑 Stop: {stop:.5f}\n"
                       f"💰 TP1: {tp1:.5f} | TP2: {tp2:.5f}\n"
                       f"📊 RR: 1:{rr:.1f} | Killzone: ✅")
                self.sender.send_text(msg)

            # --- Sell conditions ---
            elif (sweep_high and mss['bearish'] and displacement and
                  any(f['type'] == 'bearish' for f in fvg_list[-3:]) and
                  current_price > equilibrium and bias == 'bearish' and killzone):
                fvg = next(f for f in reversed(fvg_list) if f['type'] == 'bearish')
                entry = fvg['top']
                stop = df_15m['High'].iloc[-1] + STOP_DISTANCE.get(symbol, STOP_DISTANCE['default'])
                tp1 = equilibrium
                tp2 = prev_low
                rr = (entry - tp2) / (stop - entry) if (stop - entry) != 0 else 0

                gold_tag = "🔥 GOLD SIGNAL" if symbol == "GC=F" else ""
                msg = (f"📉 *ICT PRO - SELL SIGNAL* {gold_tag}\n"
                       f"📍 {symbol}\n"
                       f"🔥 Sweep PDH + MSS + Displacement\n"
                       f"🎯 Entry: {entry:.5f}\n"
                       f"🛑 Stop: {stop:.5f}\n"
                       f"💰 TP1: {tp1:.5f} | TP2: {tp2:.5f}\n"
                       f"📊 RR: 1:{rr:.1f} | Killzone: ✅")
                self.sender.send_text(msg)

        except Exception as e:
            logging.error(f"Error analyzing {symbol}: {e}")

    def run(self):
        """Main loop: analyze all pairs every 15 minutes."""
        self.sender.send_text("✅ *ICT Pro Bot started on Railway*\n🔥 Gold + Forex 24/7 (Text only mode)")
        while True:
            try:
                for pair in PAIRS:
                    self.analyze_pair(pair)
                time.sleep(SLEEP_INTERVAL)
            except Exception as e:
                logging.error(f"Main loop error: {e}")
                time.sleep(60)  # wait before retry

# ====================== Start the Bot ======================
if __name__ == "__main__":
    bot = ICTBot()
    bot.run()
