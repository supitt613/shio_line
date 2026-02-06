import os
import sys
import pandas as pd
import shioaji as sj
import requests
import pytz
from datetime import datetime, date
from dotenv import load_dotenv

# ==============================
# 0) 環境與基礎設定
# ==============================
load_dotenv()
TZ = pytz.timezone("Asia/Taipei")

SHIOAJI_API_KEY = os.getenv("SHIOAJI_API_KEY")
SHIOAJI_SECRET_KEY = os.getenv("SHIOAJI_SECRET_KEY")
LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")
LINE_USER_ID = os.getenv("LINE_USER_ID")

def send_line_msg(text):
    if not LINE_ACCESS_TOKEN: return
    url = "https://api.line.me/v2/bot/message/push"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"}
    payload = {"to": LINE_USER_ID, "messages": [{"type": "text", "text": text}]}
    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except: pass

# ==============================
# 1) 策略部署類別 (精準對齊圖片邏輯)
# ==============================
class ProTrader:
    def __init__(self, api, code):
        self.api = api
        self.code = code
        self.contract = getattr(self.api.Contracts.Futures.MXF, code, None)

    def get_config(self):
        now = datetime.now(TZ)
        h = now.hour
        # 日盤：突破 74, 階點 89 | 夜盤：突破 61, 階點 68
        if 8 <= h < 14:
            return "早盤", "05:00:00", 74, 89
        else:
            return "NIGHT", "13:45:00", 61, 68

    def fetch_base_ma(self, target_time_str):
        try:
            query_date = date.today().strftime("%Y-%m-%d")
            ticks = self.api.ticks(self.contract, query_date)
            df = pd.DataFrame({**ticks})
            if df.empty: return None
            df['ts'] = pd.to_datetime(df['ts'], errors='coerce')
            df = df.dropna(subset=['ts'])
            df['ts'] = df['ts'].dt.tz_localize('UTC').dt.tz_convert(TZ) if df['ts'].dt.tz is None else df['ts'].dt.tz_convert(TZ)
            df = df.set_index('ts', drop=True)
            price_col = 'close' if 'close' in df.columns else 'price'
            ohlc_5m = df[price_col].resample('5min', label='right', closed='right').last().ffill().to_frame()
            ohlc_5m['ma21'] = ohlc_5m[price_col].rolling(window=21).mean()
            target_rows = ohlc_5m[ohlc_5m.index.strftime('%H:%M:%S') == target_time_str]
            return round(target_rows['ma21'].iloc[-1], 2) if not target_rows.empty else None
        except: return None

    def generate_report(self):
        session, b_time, gap, step = self.get_config()
        base = self.fetch_base_ma(b_time)
        snap = self.api.snapshots([self.contract])[0]
        curr_p = snap.close
        
        if not base: return f"【{self.code}】目前無法抓取基準線。"

        long_entry = round(base + gap, 2)
        short_entry = round(base - gap, 2)

        # 計算圖片中的移動停利階點
        l1, l2, l3 = long_entry + step, long_entry + (step*2), long_entry + (step*3)
        s1, s2, s3 = short_entry - step, short_entry - (step*2), short_entry - (step*3)

        report = (
            f"📊 {self.code} 策略部署 ({session})\n"
            f"━━━━━━━━━━━━━━\n"
            f"📌 基準 21MA：{base}\n"
            f"🔴 多頭突破點：{long_entry} (↑{gap})\n"
            f"🟢 空頭突破點：{short_entry} (↓{gap})\n"
            f"━━━━━━━━━━━━━━\n"
            f"💰 移動停利預計階點：\n"
            f"1階：±{step} | 2階：±{step*2} | 3階：±{step*3}\n"
            f"【多單獲利目標】\n"
            f"L1: {round(l1)} | L2: {round(l2)} | L3: {round(l3)}\n"
            f"【空單獲利目標】\n"
            f"S1: {round(s1)} | S2: {round(s2)} | S3: {round(s3)}\n"
            f"━━━━━━━━━━━━━━\n"
            f"🔔 當前價：{curr_p}"
        )
        return report

if __name__ == "__main__":
    api = sj.Shioaji(simulation=True)
    api.login(api_key=SHIOAJI_API_KEY, secret_key=SHIOAJI_SECRET_KEY)
    
    # 修改合約為您截圖中的月份 (202604, 202606)
    targets = ["MXF202604", "MXF202606"]
    final_msg = f"🚀 策略巡航部署啟動\n{datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}\n"
    
    for code in targets:
        trader = ProTrader(api, code)
        final_msg += "\n" + trader.generate_report() + "\n"
    
    send_line_msg(final_msg)
