import pandas as pd
import yfinance as yf
import requests
import time
from datetime import datetime
import pytz
import logging
import mplfinance as mpf
import matplotlib.pyplot as plt
import os

# ====================== إعدادات من Railway Variables ======================
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TOKEN or not CHAT_ID:
    print("❌ TOKEN أو CHAT_ID مش موجودين! أضفهم في Railway Variables")
    exit(1)

# الأزواج (الذهب أولوية قصوى)
PAIRS = ["GC=F", "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X", "NZDUSD=X"]

print("🚀 ICT Pro Bot جاهز على Railway")
print("🔥 الذهب مفعل + 6 أزواج فوركس")

logging.basicConfig(level=logging.INFO, filename='ict_bot.log', format='%(asctime)s - %(levelname)s - %(message)s')

def send_telegram_msg(message):
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"})
    except: pass

def send_chart_with_signal(df, symbol, caption):
    try:
        mpf.plot(df.tail(60), type='candle', style='charles', title=f"{symbol} - ICT Pro Signal", figsize=(16,9), savefig='signal.png')
        url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
        files = {'photo': open('signal.png', 'rb')}
        data = {'chat_id': CHAT_ID, 'caption': caption, 'parse_mode': 'Markdown'}
        requests.post(url, data=data, files=files)
        os.remove('signal.png')
    except: 
        send_telegram_msg(caption)

def get_stop_distance(symbol):
    return 2.0 if symbol == "GC=F" else 0.0005

def get_multi_tf_data(symbol):
    df15 = yf.download(symbol, period="5d", interval="15m", progress=False, auto_adjust=False)
    df1h = yf.download(symbol, period="10d", interval="1h", progress=False, auto_adjust=False)
    return df15, df1h

def detect_fvg(df): 
    fvgs = []
    for i in range(2, len(df)):
        if df['Low'].iloc[i] > df['High'].iloc[i-2]:
            fvgs.append({'type': 'bullish', 'bottom': df['Low'].iloc[i], 'top': df['High'].iloc[i-2]})
        elif df['High'].iloc[i] < df['Low'].iloc[i-2]:
            fvgs.append({'type': 'bearish', 'top': df['High'].iloc[i], 'bottom': df['Low'].iloc[i-2]})
    return fvgs

def detect_mss_bos(df):
    if len(df) < 10: return {'mss_bullish': False, 'mss_bearish': False}
    last_high = df['High'].rolling(5).max().iloc[-3]
    last_low  = df['Low'].rolling(5).min().iloc[-3]
    return {'mss_bullish': df['Low'].iloc[-1] < last_low, 'mss_bearish': df['High'].iloc[-1] > last_high}

def detect_displacement(df):
    if len(df) < 15: return False
    df['body'] = abs(df['Close'] - df['Open'])
    df['range'] = df['High'] - df['Low']
    if df['range'].iloc[-1] == 0: return False
    return (df['body'].iloc[-1] / df['range'].iloc[-1] > 0.65) and (df['body'].iloc[-1] > df['body'].rolling(10).mean().iloc[-1])

def calculate_equilibrium(df):
    return (df['High'].tail(20).max() + df['Low'].tail(20).min()) / 2

def is_killzone():
    ny_tz = pytz.timezone('America/New_York')
    hour = datetime.now(ny_tz).hour
    return (2 <= hour <= 5) or (7 <= hour <= 10)

def analyze_pair(symbol):
    try:
        df15, df1h = get_multi_tf_data(symbol)
        if len(df15) < 30: return

        daily = yf.download(symbol, period="3d", interval="1d", progress=False, auto_adjust=False)
        if len(daily) < 2: return

        prev_high = daily['High'].iloc[-2]
        prev_low  = daily['Low'].iloc[-2]

        fvg_list = detect_fvg(df15)
        structure = detect_mss_bos(df15)
        displacement = detect_displacement(df15)
        equilibrium = calculate_equilibrium(df15)
        killzone = is_killzone()
        bias_bullish = df1h['Close'].iloc[-1] > df1h['Close'].iloc[-20]

        stop_distance = get_stop_distance(symbol)

        # شراء
        if (df15['Low'].iloc[-1] < prev_low and structure['mss_bullish'] and displacement and
            any(f['type'] == 'bullish' for f in fvg_list[-3:]) and
            df15['Close'].iloc[-1] < equilibrium and bias_bullish and killzone):

            fvg = next(f for f in fvg_list[-3:] if f['type'] == 'bullish')
            entry = fvg['bottom']
            stop = df15['Low'].iloc[-1] - stop_distance
            tp1 = equilibrium
            tp2 = prev_high
            rr = (tp2 - entry) / (entry - stop)

            gold_tag = "🔥 GOLD SIGNAL" if symbol == "GC=F" else ""
            msg = f"🚀 *ICT PRO - إشارة شراء قوية* {gold_tag}\n📍 {symbol}\n🔥 Sweep PDL + MSS + Displacement\n🎯 FVG: {entry:.5f}\n🛑 Stop: {stop:.5f}\n💰 TP1: {tp1:.5f} | TP2: {tp2:.5f}\n📊 RR: 1:{rr:.1f} | Killzone: ✅"
            send_chart_with_signal(df15, symbol, msg)

        # بيع
        elif (df15['High'].iloc[-1] > prev_high and structure['mss_bearish'] and displacement and
              any(f['type'] == 'bearish' for f in fvg_list[-3:]) and
              df15['Close'].iloc[-1] > equilibrium and not bias_bullish and killzone):

            fvg = next(f for f in fvg_list[-3:] if f['type'] == 'bearish')
            entry = fvg['top']
            stop = df15['High'].iloc[-1] + stop_distance
            tp1 = equilibrium
            tp2 = prev_low
            rr = (entry - tp2) / (stop - entry)

            gold_tag = "🔥 GOLD SIGNAL" if symbol == "GC=F" else ""
            msg = f"📉 *ICT PRO - إشارة بيع قوية* {gold_tag}\n📍 {symbol}\n🔥 Sweep PDH + MSS + Displacement\n🎯 FVG: {entry:.5f}\n🛑 Stop: {stop:.5f}\n💰 TP1: {tp1:.5f} | TP2: {tp2:.5f}\n📊 RR: 1:{rr:.1f} | Killzone: ✅"
            send_chart_with_signal(df15, symbol, msg)

    except Exception as e:
        logging.error(f"Error {symbol}: {e}")

# ====================== تشغيل البوت ======================
if __name__ == "__main__":
    send_telegram_msg("✅ *ICT Pro Bot تم رفعه على Railway بنجاح*\n🔥 الذهب + الفوركس شغال 24/7")
    while True:
        try:
            for pair in PAIRS:
                analyze_pair(pair)
            time.sleep(900)
        except:
            time.sleep(60)
