import os
import sys
import pandas as pd
import shioaji as sj
import requests
import pytz
from datetime import datetime, date
from dotenv import load_dotenv

# ==============================
# 0) 基礎設定與環境變數
# ==============================
load_dotenv()
TZ = pytz.timezone("Asia/Taipei")

class ProTrader:
    def __init__(self, api, code):
        self.api = api
        self.code = code
        # 動態取得合約物件
        self.contract = self.api.Contracts.Futures.MXF[code]

    def get_config(self):
        """
        根據當前時間返回對應的策略參數
        早盤：突破 110, 階點 89, 停損 89
        夜盤：突破 68,  階點 68, 停損 89
        """
        now = datetime.now(TZ)
        h = now.hour
        
        # 判斷盤別邏輯 (08:00 - 13:59 定義為早盤)
        if 8 <= h < 14:
            return {
                "session": "早盤",
                "base_time": "05:00:00", # 取前一盤結尾 MA
                "gap": 110,
                "step": 89,
                "sl_dist": 89
            }
        else:
            return {
                "session": "NIGHT",
                "base_time": "13:45:00", # 取日盤結尾 MA
                "gap": 68,
                "step": 68,
                "sl_dist": 89
            }

    def fetch_base_ma(self, target_time_str):
        """精準抓取特定時間點的 21MA"""
        try:
            # 抓取當日 Ticks
            ticks = self.api.ticks(self.contract, date.today().strftime("%Y-%m-%d"))
            df = pd.DataFrame({**ticks})
            if df.empty: return None
            
            # 時間轉換與格式化
            df['ts'] = pd.to_datetime(df['ts']).dt.tz_localize('UTC').dt.tz_convert(TZ)
            df.set_index('ts', inplace=True)
            
            # 重新取樣為 5 分鐘 K 線
            price_col = 'close' if 'close' in df.columns else 'price'
            ohlc_5m = df[price_col].resample('5min', label='right', closed='right').last().ffill()
            ma21 = ohlc_5m.rolling(window=21).mean()
            
            # 取得指定時間的 MA 值
            target_ma = ma21[ma21.index.strftime('%H:%M:%S') == target_time_str]
            return round(target_ma.iloc[-1], 2) if not target_ma.empty else None
        except Exception as e:
            print(f"MA 抓取失敗: {e}")
            return None

    def execute_strategy(self):
        conf = self.get_config()
        base = self.fetch_base_ma(conf['base_time'])
        
        if not base:
            return f"❌ 【{self.code}】無法取得基準線，請確認資訊源。"

        # 取得最新快照
        snap = self.api.snapshots([self.contract])[0]
        curr_p = snap.close

        # 計算點位
        long_entry = round(base + conf['gap'], 2)
        short_entry = round(base - conf['gap'], 2)
        
        # 停損點位 (進場價 ± 89)
        long_sl = round(long_entry - conf['sl_dist'], 2)
        short_sl = round(short_entry + conf['sl_dist'], 2)

        # 獲利階點
        l_targets = [round(long_entry + conf['step'] * i) for i in range(1, 4)]
        s_targets = [round(short_entry - conf['step'] * i) for i in range(1, 4)]

        # 模擬下單邏輯觸發 (簡單範例：突破即發報/模擬買進)
        # if curr_p >= long_entry: self.place_sim_order(...)

        report = (
            f"📊 {self.code} 策略部署 ({conf['session']})\n"
            f"━━━━━━━━━━━━━━\n"
            f"📌 基準 21MA：{base}\n"
            f"🔴 多頭進場：{long_entry} (損:{long_sl})\n"
            f"🟢 空頭進場：{short_entry} (損:{short_sl})\n"
            f"━━━━━━━━━━━━━━\n"
            f"💰 預計獲利階點 (Step: {conf['step']})\n"
            f"多單：{' ➔ '.join(map(str, l_targets))}\n"
            f"空單：{' ➔ '.join(map(str, s_targets))}\n"
            f"━━━━━━━━━━━━━━\n"
            f"🔔 當前市價：{curr_p}"
        )
        return report

# ==============================
# 2) 主程式執行與 Line 通知
# ==============================
def send_line_msg(text):
    token = os.getenv("LINE_ACCESS_TOKEN")
    uid = os.getenv("LINE_USER_ID")
    if not token or not uid: return
    
    url = "https://api.line.me/v2/bot/message/push"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    payload = {"to": uid, "messages": [{"type": "text", "text": text}]}
    requests.post(url, headers=headers, json=payload)

if __name__ == "__main__":
    # 檢查是否為週末
    if datetime.now(TZ).weekday() >= 5:
        print("今日為週末，不執行策略。")
        sys.exit()

    api = sj.Shioaji(simulation=True)
    api.login(os.getenv("SHIOAJI_API_KEY"), os.getenv("SHIOAJI_SECRET_KEY"))
    
    targets = ["MXF202604", "MXF202606"]
    final_msg = f"🚀 策略巡航啟動 ({datetime.now(TZ).strftime('%H:%M')})\n"
    
    for code in targets:
        trader = ProTrader(api, code)
        final_msg += "\n" + trader.execute_strategy() + "\n"
    
    send_line_msg(final_msg)
    api.logout()
