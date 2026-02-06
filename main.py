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

# ==============================
# 0) 環境與基礎設定
# ==============================
load_dotenv()
TZ = pytz.timezone("Asia/Taipei")

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
    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except: pass

# ==============================
# 1) 雲端交易員類別
# ==============================
class CloudTrader:
    def __init__(self, api, code):
        self.api = api
        self.code = code
        self.contract = getattr(self.api.Contracts.Futures.MXF, code, None)

    def get_config(self):
        """自動判斷日夜盤參數"""
        now = datetime.now(TZ)
        h = now.hour
        # 日盤：基準 05:00, Gap 74, 止損 89
        if (h >= 8 and h < 14):
            return "DAY", "05:00:00", 74, 89
        # 夜盤：基準 13:45, Gap 61, 止損 68
        else:
            return "NIGHT", "13:45:00", 61, 68

    def fetch_base_ma(self, target_time_str):
        """強健版：Ticks 轉 5分K 並精算基準線"""
        try:
            query_date = date.today().strftime("%Y-%m-%d")
            ticks = self.api.ticks(self.contract, query_date)
            df = pd.DataFrame({**ticks})
            if df.empty: return None

            # 修正 Datetime 轉換與時區
            df['ts'] = pd.to_datetime(df['ts'], errors='coerce')
            df = df.dropna(subset=['ts'])
            if df['ts'].dt.tz is None:
                df['ts'] = df['ts'].dt.tz_localize('UTC').dt.tz_convert(TZ)
            else:
                df['ts'] = df['ts'].dt.tz_convert(TZ)
            
            df = df.set_index('ts', drop=True)

            # 補值並計算 21MA
            price_col = 'close' if 'close' in df.columns else 'price'
            ohlc_5m = df[price_col].resample('5min', label='right', closed='right').last().ffill().to_frame()
            ohlc_5m['ma21'] = ohlc_5m[price_col].rolling(window=21).mean()

            # 鎖定時間點
            target_rows = ohlc_5m[ohlc_5m.index.strftime('%H:%M:%S') == target_time_str]
            if not target_rows.empty:
                val = target_rows['ma21'].iloc[-1]
                return round(val, 2) if pd.notnull(val) else None
            return None
        except Exception as e:
            print(f"[{self.code}] 基準線計算異常: {e}")
            return None

    def get_active_position(self):
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
                pos = self.get_active_position()
                if pos: supabase.table("sim_orders").update({"status": "closed"}).eq("id", pos["id"]).execute()
            else:
                supabase.table("sim_orders").insert({
                    "code": self.code, "action": action, "price": price, 
                    "status": "open", "remark": remark
                }).execute()
        
        send_line_msg(f"✅ 【交易執行：{self.code}】\n動作: {action}\n價格: {price}\n原因: {remark}")

    def execute_logic(self, cmd):
        session, base_time, gap, stop_loss = self.get_config()
        snap = self.api.snapshots([self.contract])[0]
        curr_p = snap.close
        pos = self.get_active_position()

        if cmd == "entry":
            if pos: return print(f"[{self.code}] 已有持倉，跳過。")
            base = self.fetch_base_ma(base_time)
            print(f"🔍 [{self.code}] 基準線: {base}, 目前價: {curr_p}")
            if base:
                if curr_p >= (base + gap): self.place_order("Buy", curr_p, f"{session}突破進場")
                elif curr_p <= (base - gap): self.place_order("Sell", curr_p, f"{session}跌破進場")

        elif cmd == "monitor":
            if not pos: return
            entry_p = float(pos["price"])
            side = pos["action"]
            loss = (entry_p - curr_p) if side == "Buy" else (curr_p - entry_p)
            print(f"⚖️ [{self.code}] 目前損益: {-loss} pt")
            if loss >= stop_loss:
                exit_act = "Sell" if side == "Buy" else "Buy"
                self.place_order(exit_act, curr_p, f"{session}觸及止損", is_closing=True)

        elif cmd == "exit":
            if not pos: return
            exit_act = "Sell" if pos["action"] == "Buy" else "Buy"
            self.place_order(exit_act, curr_p, f"{session}時間截止強制平倉", is_closing=True)

# ==============================
# 2) 主程式啟動器
# ==============================
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "monitor"
    
    api = sj.Shioaji(simulation=True) 
    api.login(api_key=SHIOAJI_API_KEY, secret_key=SHIOAJI_SECRET_KEY)
    
    targets = ["MXF202603", "MXF202604"] 
    for code in targets:
        trader = CloudTrader(api, code)
        trader.execute_logic(mode)
