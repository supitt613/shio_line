import os
import sys
import time
import pandas as pd
import shioaji as sj
import requests
import pytz
from datetime import datetime, date, timedelta
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
TZ = pytz.timezone("Asia/Taipei")

# 環境變數
SHIOAJI_API_KEY = os.getenv("SHIOAJI_API_KEY")
SHIOAJI_SECRET_KEY = os.getenv("SHIOAJI_SECRET_KEY")
LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")
LINE_USER_ID = os.getenv("LINE_USER_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except:
    supabase = None

def send_line_msg(text):
    if not LINE_ACCESS_TOKEN: return
    url = "https://api.line.me/v2/bot/message/push"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"}
    payload = {"to": LINE_USER_ID, "messages": [{"type": "text", "text": text}]}
    requests.post(url, headers=headers, json=payload, timeout=10)

class CloudTrader:
    def __init__(self, api, code):
        self.api = api
        self.code = code
        self.contract = getattr(self.api.Contracts.Futures.MXF, code, None)

    def get_config(self):
        now = datetime.now(TZ)
        h = now.hour
        if 8 <= h < 14: # 日盤
            return "DAY", "05:00:00", 74, 89  # 模式, 基準時間, 進場Gap, 止損點
        else: # 夜盤
            return "NIGHT", "13:45:00", 61, 68

     def fetch_base_ma(self, target_time_str):
        """利用 Ticks 補值邏輯精算基準線 (修正版)"""
        try:
            query_date = date.today().strftime("%Y-%m-%d")
            ticks = self.api.ticks(self.contract, query_date)
            df = pd.DataFrame({**ticks})
            if df.empty: return None

            # 修正點 1: 確保 ts 轉換為 Datetime 格式，並處理異常值
            df['ts'] = pd.to_datetime(df['ts'], errors='coerce')
            df = df.dropna(subset=['ts']) # 移除無法轉換的時間列

            # 修正點 2: 使用更安全的方式處理時區
            if df['ts'].dt.tz is None:
                df['ts'] = df['ts'].dt.tz_localize('UTC').dt.tz_convert(TZ)
            else:
                df['ts'] = df['ts'].dt.tz_convert(TZ)

            # 設定索引
            df = df.set_index('ts', drop=True)

            # 3. 5分鐘K線轉換與補值
            # 確保欄位名稱正確 (Shioaji Ticks 有時是大寫 'Close' 有時是小寫 'close')
            price_col = 'close' if 'close' in df.columns else 'price'
            ohlc_5m = df[price_col].resample('5min', label='right', closed='right').last().ffill().to_frame()
            ohlc_5m['21MA'] = ohlc_5m[price_col].rolling(window=21).mean()

            # 4. 鎖定時間點
            target_rows = ohlc_5m[ohlc_5m.index.strftime('%H:%M:%S') == target_time_str]
            if not target_rows.empty:
                val = target_rows['21MA'].iloc[-1]
                return round(val, 2) if pd.notnull(val) else None
            return None
        except Exception as e:
            print(f"[{self.code}] 基準線計算異常: {e}")
            return None

    def get_active_position(self):
        """從 Supabase 取得尚未平倉的部位"""
        if not supabase: return None
        res = supabase.table("sim_orders").select("*").eq("code", self.code).eq("status", "open").execute()
        return res.data[0] if res.data else None

    def place_order(self, action, price, remark, is_closing=False):
        order = self.api.Order(
            action=action, price=0, quantity=1,
            order_type=sj.constant.OrderType.Market,
            price_type=sj.constant.OrderType.Market,
            oct=sj.constant.FuturesOCT.Auto, code=self.code
        )
        self.api.place_order(self.contract, order)
        
        if supabase:
            if is_closing:
                # 更新原本的進場單為 closed
                pos = self.get_active_position()
                if pos:
                    supabase.table("sim_orders").update({"status": "closed"}).eq("id", pos["id"]).execute()
            else:
                # 建立新部位
                supabase.table("sim_orders").insert({
                    "code": self.code, "action": action, "price": price, 
                    "status": "open", "remark": remark
                }).execute()
        
        send_line_msg(f"🔔 【交易執行】\n合約: {self.code}\n動作: {action}\n價格: {price}\n原因: {remark}")

    def execute_logic(self, cmd):
        session, base_time, gap, stop_loss = self.get_config()
        snap = self.api.snapshots([self.contract])[0]
        curr_p = snap.close
        pos = self.get_active_position()

        if cmd == "entry":
            if pos: return print(f"{self.code} 今日已有持倉。")
            base = self.fetch_base_ma(base_time)
            if base:
                if curr_p >= (base + gap): self.place_order("Buy", curr_p, f"{session}進場")
                elif curr_p <= (base - gap): self.place_order("Sell", curr_p, f"{session}進場")

        elif cmd == "monitor":
            if not pos: return
            entry_p = float(pos["price"])
            side = pos["action"]
            loss = (entry_p - curr_p) if side == "Buy" else (curr_p - entry_p)
            if loss >= stop_loss:
                exit_action = "Sell" if side == "Buy" else "Buy"
                self.place_order(exit_action, curr_p, f"{session}止損出場", is_closing=True)

        elif cmd == "exit":
            if not pos: return
            exit_action = "Sell" if pos["action"] == "Buy" else "Buy"
            self.place_order(exit_action, curr_p, f"{session}收盤強制平倉", is_closing=True)

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "report"
    api = sj.Shioaji(simulation=True) # 建議先用模擬
    api.login(api_key=SHIOAJI_API_KEY, secret_key=SHIOAJI_SECRET_KEY)
    
    targets = ["MXF202603", "MXF202604"] # 設定合約
    for code in targets:
        trader = CloudTrader(api, code)
        trader.execute_logic("entry")
