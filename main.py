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
# 1) 策略部署類別
# ==============================
class StrategyReporter:
    def __init__(self, api, code):
        self.api = api
        self.code = code
        self.contract = getattr(self.api.Contracts.Futures.MXF, code, None)

    def get_strategy_params(self):
        now = datetime.now(TZ)
        h = now.hour
        # 日盤：08:00-14:00 | 夜盤：其他
        if 8 <= h < 14:
            return "早盤", "05:00:00", 74, 89
        else:
            return "夜盤", "13:45:00", 61, 68

    def fetch_base_ma(self, target_time_str):
        try:
            query_date = date.today().strftime("%Y-%m-%d")
            ticks = self.api.ticks(self.contract, query_date)
            df = pd.DataFrame({**ticks})
            if df.empty: return None

            # 時間處理
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

    def check_and_report(self):
        session_name, base_time, gap, sl = self.get_strategy_params()
        base = self.fetch_base_ma(base_time)
        snap = self.api.snapshots([self.contract])[0]
        curr_p = snap.close
        
        if not base:
            return f"【{self.code}】目前無法取得基準線數據。"

        diff = round(curr_p - base, 2)
        status = "無訊號"
        if curr_p >= (base + gap): status = "🚩 突破進場訊號 (做多)"
        elif curr_p <= (base - gap): status = "💀 跌破進場訊號 (放空)"

        report = (
            f"📊 {session_name}策略部署報告\n"
            f"合約：{self.code}\n"
            f"基準線(21MA)：{base}\n"
            f"當前價：{curr_p} (價差: {diff})\n"
            f"進場門檻：{gap} | 停損設定：{sl}\n"
            f"判定結果：{status}"
        )
        return report

# ==============================
# 2) 主程式啟動
# ==============================
if __name__ == "__main__":
    api = sj.Shioaji(simulation=True)
    api.login(api_key=SHIOAJI_API_KEY, secret_key=SHIOAJI_SECRET_KEY)
    
    targets = ["MXF202603", "MXF202604"]
    full_report = f"🔔 雲端策略巡航啟動\n時間: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}\n"
    
    for code in targets:
        reporter = StrategyReporter(api, code)
        full_report += "\n---\n" + reporter.check_and_report()
    
    send_line_msg(full_report)
