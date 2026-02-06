import os
import time
import threading
import pandas as pd
import shioaji as sj
import requests
import pytz
import matplotlib
# 強制使用非交互式後端，防止 GUI 衝突
matplotlib.use('Agg')
from datetime import datetime, date, timedelta
from dotenv import load_dotenv

# ==============================
# 0) 環境與常數設定
# ==============================
load_dotenv()
TZ = pytz.timezone("Asia/Taipei")

# 從 .env 讀取設定
SHIOAJI_API_KEY = os.getenv("SHIOAJI_API_KEY")
SHIOAJI_SECRET_KEY = os.getenv("SHIOAJI_SECRET_KEY")
LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")
LINE_USER_ID = os.getenv("LINE_USER_ID")

# ==============================
# 1) 通知模組
# ==============================
def send_line_msg(text):
    if not LINE_ACCESS_TOKEN: return
    url = "https://api.line.me/v2/bot/message/push"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"}
    payload = {"to": LINE_USER_ID, "messages": [{"type": "text", "text": text}]}
    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e: 
        print(f"LINE 發送失敗: {e}")

# ==============================
# 2) 策略監控機器人核心
# ==============================
class FVDStepBot(threading.Thread):
    def __init__(self, api, code):
        threading.Thread.__init__(self)
        self.api = api
        self.code = code
        self.contract = getattr(self.api.Contracts.Futures.MXF, self.code, None)
        
        self.base_ma = None           # 基準 21MA
        self.current_session = ""      # DAY (日) / NIGHT (夜)
        self.last_notify_price = None  # 移動停利追蹤點
        self.is_trend_triggered = False # 突破觸發狀態

    def get_session_config(self):
        """自動判斷盤別與策略參數"""
        now = datetime.now(TZ)
        h, m = now.hour, now.minute
        # 日盤: 08:45 ~ 13:45
        if (h == 8 and m >= 45) or (9 <= h < 14):
            return "DAY", "05:00:00", 74, 110
        else:
            return "NIGHT", "13:45:00", 61, 68

    def fetch_base_ma(self, target_time_str):
        """利用 Ticks 重組 5 分 K 並補值，精算 21MA 基準"""
        try:
            # 抓取該交易日 ticks
            query_date = date.today().strftime("%Y-%m-%d")
            ticks = self.api.ticks(self.contract, query_date)
            df = pd.DataFrame({**ticks})
            if df.empty: return None

            # 時間處理與時區對齊
            df.ts = pd.to_datetime(df.ts)
            df.ts = df.ts.dt.tz_localize(TZ) if df.ts.dt.tz is None else df.ts.dt.tz_convert(TZ)
            df = df.set_index('ts', drop=True)

            # 重採樣 5 分 K 並進行向前填充 (FFill)，確保夜盤成交稀疏時 MA 依然穩定
            ohlc_5m = df['close'].resample('5min', label='right', closed='right').last().ffill().to_frame()
            ohlc_5m['21MA'] = ohlc_5m['close'].rolling(window=21).mean()

            # 鎖定基準時間 (如 05:00:00)
            target_rows = ohlc_5m[ohlc_5m.index.strftime('%H:%M:%S') == target_time_str]
            if not target_rows.empty:
                return round(target_rows['21MA'].iloc[-1], 2)
            return None
        except Exception as e:
            print(f"[{self.code}] 基準線計算異常: {e}")
            return None

    def format_strategy_report(self, session, base, gap, trail):
        """生成當前盤別的作戰價位地圖"""
        long_trigger = base + gap
        short_trigger = base - gap
        
        report = (
            f"📊 {self.code} 策略部署 ({session})\n"
            f"━━━━━━━━━━━━━━\n"
            f"📌 基準 21MA：{base}\n"
            f"🔴 多頭突破點：{long_trigger:.0f} (↑{gap})\n"
            f"🟢 空頭突破點：{short_trigger:.0f} (↓{gap})\n"
            f"━━━━━━━━━━━━━━\n"
            f"💰 移動停利預計階點：\n"
            f"1階：±{trail} | 2階：±{trail*2} | 3階：±{trail*3}\n"
            f"【多單獲利目標】\n"
            f"L1: {long_trigger+trail:.0f} | L2: {long_trigger+trail*2:.0f} | L3: {long_trigger+trail*3:.0f}\n"
            f"【空單獲利目標】\n"
            f"S1: {short_trigger-trail:.0f} | S2: {short_trigger-trail*2:.0f} | S3: {short_trigger-trail*3:.0f}"
        )
        return report

    def run(self):
        print(f"🤖 Bot {self.code} 已上線。")
        while not stop_event.is_set():
            try:
                session, target_time, gap, trail = self.get_session_config()
                
                # 盤別切換檢查
                if session != self.current_session:
                    new_base = self.fetch_base_ma(target_time)
                    if new_base:
                        self.base_ma = new_base
                        self.current_session = session
                        self.is_trend_triggered = False
                        self.last_notify_price = None
                        
                        # 發送 LINE 策略報告
                        send_line_msg(self.format_strategy_report(session, new_base, gap, trail))
                        print(f"✅ {self.code} 基準更新: {new_base}")

                # 即時監控報價
                if self.base_ma:
                    snap = self.api.snapshots([self.contract])[0]
                    curr_price = snap.close
                    diff = curr_price - self.base_ma
                    
                    # 突破偵測
                    if not self.is_trend_triggered and abs(diff) >= gap:
                        side = "🔴 多方強勢" if diff > 0 else "🟢 空方強勢"
                        send_line_msg(f"🚀 {self.code} 趨勢啟動!\n目前價: {curr_price}\n偏向: {side}")
                        self.is_trend_triggered = True
                        self.last_notify_price = curr_price

                    # 移動停利偵測
                    if self.is_trend_triggered and self.last_notify_price:
                        if abs(curr_price - self.last_notify_price) >= trail:
                            send_line_msg(f"💰 {self.code} 達成移動停利階點\n最新報價: {curr_price}")
                            self.last_notify_price = curr_price

                time.sleep(30)
            except Exception as e:
                print(f"[{self.code}] 循環異常: {e}")
                time.sleep(10)

# ==============================
# 3) 主程式與資源管理
# ==============================
stop_event = threading.Event()

# ... 前面 Bot 類別定義不變 ...

if __name__ == "__main__":
    import sys
    
    main_api = sj.Shioaji()
    main_api.login(api_key=SHIOAJI_API_KEY, secret_key=SHIOAJI_SECRET_KEY)
    
    targets = ["MXF202602", "MXF202603", "MXF202604", "MXF202606"]
    
    # 檢查是否為 GitHub Actions 的單次執行模式
    once_mode = "--once" in sys.argv

    bots = []
    for code in targets:
        bot = FVDStepBot(main_api, code)
        if once_mode:
            # 單次模式：直接執行核心邏輯不開執行緒
            session, target_time, gap, trail = bot.get_session_config()
            new_base = bot.fetch_base_ma(target_time)
            if new_base:
                send_line_msg(bot.format_strategy_report(session, new_base, gap, trail))
                print(f"✅ {code} 報告發送成功")
        else:
            # 正常模式：開執行緒持續監控
            bot.daemon = True
            bot.start()
            bots.append(bot)
            time.sleep(2)

    if not once_mode:
        print("🚀 持續監控模式運行中...")
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt:
            stop_event.set()
