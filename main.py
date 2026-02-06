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
# 1) 交易機器人核心類別
# ==============================
class CloudTrader:
    def __init__(self, api, code):
        self.api = api
        self.code = code
        self.contract = getattr(self.api.Contracts.Futures.MXF, code, None)

    def get_config(self):
        now = datetime.now(TZ)
        h = now.hour
        if (h >= 8 and h < 14):
            return "DAY", "05:00:00", 74, 89
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
            if df['ts'].dt.tz is None:
                df['ts'] = df['ts'].dt.tz_localize('UTC').dt.tz_convert(TZ)
            else:
                df['ts'] = df['ts'].dt.tz_convert(TZ)
            
            df = df.set_index('ts', drop=True)
            price_col = 'close' if 'close' in df.columns else 'price'
            ohlc_5m = df[price_col].resample('5min', label='right', closed='right').last().ffill().to_frame()
            ohlc_5m['ma21'] = ohlc_5m[price_col].rolling(window=21).mean()

            target_rows = ohlc_5m[ohlc_5m.index.strftime('%H:%M:%S') == target_time_str]
            return round(target_rows['ma21'].iloc[-1], 2) if not target_rows.empty else None
        except Exception as e:
            print(f"[{self.code}] 基準線計算異常: {e}")
            return None

    def get_active_position(self):
        if not supabase: return None
        res = supabase.table("sim_orders").select("*").eq("code", self.code).eq("status", "open").execute()
        return res.data[0] if res.data else None

    def place_order(self, action, price, remark, is_closing=False):
        """下單邏輯：加入帳號檢查以避免 'not provide account' 錯誤"""
        try:
            # 確保使用當前登入的期貨帳號
            if not self.api.fut_account:
                raise Exception("API 尚未綁定期貨帳號")

            # 兼容性參數處理
            try:
                p_type = getattr(sj.constant.FuturesPriceType, 'MKT', 'Market')
                oct_val = getattr(sj.constant.FuturesOCT, 'Auto', 'Auto')
            except:
                p_type = 'MKT'; oct_val = 'Auto'

            order = self.api.Order(
                action=action, price=0, quantity=1,
                order_type=sj.constant.OrderType.ROD,
                price_type=p_type, oct=oct_val, code=self.code
            )
            
            self.api.place_order(self.contract, order)
            print(f"📡 {self.code} 委託成功: {action}")
        except Exception as e:
            print(f"❌ {self.code} 下單執行失敗: {e}")
            send_line_msg(f"⚠️ 下單失敗: {self.code}\n原因: {e}")
            return # 下單失敗時終止，不寫入資料庫

        # 下單成功才執行資料庫同步
        if supabase:
            if is_closing:
                pos = self.get_active_position()
                if pos: supabase.table("sim_orders").update({"status": "closed"}).eq("id", pos["id"]).execute()
            else:
                supabase.table("sim_orders").insert({
                    "code": self.code, "action": action, "price": price, 
                    "status": "open", "remark": remark
                }).execute()
        
        send_line_msg(f"✅ 【交易執行：{self.code}】\n動作: {action}\n成交價: {price}\n原因: {remark}")

    def execute_logic(self, cmd):
        session, base_time, gap, stop_loss = self.get_config()
        snap = self.api.snapshots([self.contract])[0]
        curr_p = snap.close
        pos = self.get_active_position()

        if cmd == "entry":
            if pos: return print(f"[{self.code}] 已有持倉。")
            base = self.fetch_base_ma(base_time)
            print(f"🔍 [{self.code}] 基準: {base}, 現價: {curr_p}")
            if base:
                if curr_p >= (base + gap): self.place_order("Buy", curr_p, f"{session}突破進場")
                elif curr_p <= (base - gap): self.place_order("Sell", curr_p, f"{session}跌破進場")

        elif cmd == "monitor":
            if not pos: return
            entry_p = float(pos["price"])
            side = pos["action"]
            loss = (entry_p - curr_p) if side == "Buy" else (curr_p - entry_p)
            print(f"⚖️ [{self.code}] 浮動點數: {-loss}")
            if loss >= stop_loss:
                exit_act = "Sell" if side == "Buy" else "Buy"
                self.place_order(exit_act, curr_p, f"{session}止損", is_closing=True)

        elif cmd == "exit":
            if not pos: return
            exit_act = "Sell" if pos["action"] == "Buy" else "Buy"
            self.place_order(exit_act, curr_p, f"{session}收盤平倉", is_closing=True)

# ==============================
# 2) 主程式啟動器 (修復帳號問題)
# ==============================
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "monitor"
    
    # 登入 API
    api = sj.Shioaji(simulation=True) 
    api.login(api_key=SHIOAJI_API_KEY, secret_key=SHIOAJI_SECRET_KEY)
    
    # === 關鍵修正：自動關聯期貨帳號 ===
    try:
        # 確保帳號資訊已載入
        api.set_default_index() 
        account_id = api.fut_account.account_id if api.fut_account else "None"
        print(f"✅ 成功關聯帳號: {account_id}")
    except Exception as e:
        print(f"⚠️ 帳號關聯警告: {e}")

    # 發送啟動訊號
    send_line_msg(f"📢 雲端機器人驗證\n帳號: {api.fut_account}\n模式: {mode}")
    
    targets = ["MXF202603", "MXF202604"] 
    for code in targets:
        CloudTrader(api, code).execute_logic(mode)
