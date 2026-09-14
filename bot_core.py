"""
🔱🌾 TITAN V4.1 MODE PANEN - FIX NO SIGNAL DARI KEMARIN
Perubahan: Vector 0.22, Quorum 24%, Chop 62, ATR 0.6, OFI +-0.5, Cooldown 3 menit
+ Debug log biar tau kenapa ke-block
"""
import asyncio, json, logging, math, os, sys, time, tempfile, requests
from datetime import datetime, timezone
from pathlib import Path
from collections import deque
import numpy as np
import websockets
from websockets.exceptions import ConnectionClosed
import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("TITAN_V4.1")

DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"
SYMBOL = os.getenv("TARGET_SYMBOL", "frxXAUUSD").strip()
STATE_FILE = Path(os.getenv("STATE_FILE", "titan_v4_state.json"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or os.getenv("TELEGRAM_TOKEN","").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# === FIX MODE PANEN (LONGGARIN) ===
GRAN_M5 = 300
GRAN_H1 = 3600
LOOKBACK = 24
MIN_TP = 12.0  # dari 18 -> 12 biar gampang TP
BEP_TRIGGER = 4.0 # dari 5 -> 4 biar cepet BEP
BEP_PLUS = 0.8
MIN_VECTOR = 0.22  # FIX UTAMA: dari 0.40 -> 0.22
MAX_RISK_PER_TRADE = 18.0 # dari 15 -> 18
SIGNAL_COOLDOWN = 180 # dari 900 -> 180 = 3 menit
QUORUM_KECIL = 24 # dari 32 -> 24 = 6 petani cukup
QUORUM_RAYA = 48
MAX_SPREAD = 12.0 # dari 9 -> 12
MAX_CHOP = 62 # dari 58 -> 62
MIN_ATR = 0.6 # dari 0.9 -> 0.6
MIN_BODY_RATIO = 0.35 # dari 0.45 -> 0.35
MIN_WICK_RATIO = 0.28 # dari 0.38 -> 0.28

REQ_H1, REQ_M5 = 101, 102

class Kalman2D:
    def __init__(self, q=1e-5, r=0.8):
        self.x = np.array([0.0, 0.0]); self.P = np.eye(2) * 1.0
        self.Q = np.array([[q, 0],[0, q*15]]); self.R = r
        self.F = np.array([[1., 1.],[0., 1.]]); self.H = np.array([[1., 0.]])
        self.init = False
    def update(self, z: float):
        if not self.init:
            self.x[0] = z; self.init = True; return z, 0.0
        self.x = self.F @ self.x; self.P = self.F @ self.P @ self.F.T + self.Q
        y = z - (self.H @ self.x)[0]; S = (self.H @ self.P @ self.H.T)[0,0] + self.R
        K = (self.P @ self.H.T) / S; self.x = self.x + K.flatten() * y
        self.P = (np.eye(2) - K @ self.H) @ self.P
        return float(self.x[0]), float(self.x[1])

class MarketMath:
    @staticmethod
    def wilder_atr(h,l,c,p=14):
        if len(c) < p+1: return float(np.mean(h - l))
        tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
        atr = np.zeros(len(tr)); atr[p-1] = np.mean(tr[:p])
        for i in range(p, len(tr)): atr[i] = (atr[i-1]*(p-1) + tr[i]) / p
        return float(atr[-1])
    @staticmethod
    def choppiness(h,l,c,p=14):
        if len(c) < p+2: return 50
        tr_sum = np.sum(np.maximum(h[-p:] - l[-p:], np.maximum(np.abs(h[-p:] - c[-p-1:-1]), np.abs(l[-p:] - c[-p-1:-1]))))
        hl_range = np.max(h[-p:]) - np.min(l[-p:])
        if hl_range == 0: return 100
        return 100 * math.log10(tr_sum / hl_range) / math.log10(p)
    @staticmethod
    def impulse(candles, atr):
        if len(candles) < 3 or atr <= 0: return 0
        dp1 = (candles[-2]["close"] - candles[-3]["close"]) / atr
        dp2 = (candles[-1]["close"] - candles[-2]["close"]) / atr
        return math.sqrt(dp1*dp1 + dp2*dp2)

class AntiBlokirFetcher:
    def __init__(self):
        self.urls = ["https://api.binance.com/api/v3/depth?symbol=PAXGUSDT&limit=20","https://api1.binance.com/api/v3/depth?symbol=PAXGUSDT&limit=20","https://data-api.binance.vision/api/v3/depth?symbol=PAXGUSDT&limit=20"]
    def get_ofi_sync(self):
        for url in self.urls:
            try:
                r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=5)
                if r.status_code==200:
                    d=r.json(); bids=d['bids']; asks=d['asks']
                    bv=sum(float(q) for _,q in bids[:5]); av=sum(float(q) for _,q in asks[:5])
                    ofi=(bv-av)/(bv+av+1e-9); price=(float(bids[0][0])+float(asks[0][0]))/2
                    return price, float(ofi)
            except: continue
        return None, 0.0

class Telegram:
    def __init__(self, token, chat_id):
        self.token, self.chat_id = token, chat_id
        self.url = f"https://api.telegram.org/bot{token}/sendMessage" if token else None
        self.url_photo = f"https://api.telegram.org/bot{token}/sendPhoto" if token else None
        self.session = None
    async def _sess(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session
    async def send(self, txt):
        if not self.url: print(txt); return
        for _ in range(3):
            try:
                s = await self._sess()
                async with s.post(self.url, json={"chat_id": self.chat_id, "text": txt, "parse_mode": "HTML"}, timeout=10) as r:
                    if r.status == 200: return
            except Exception as e:
                logger.warning(f"TG err {e}"); await asyncio.sleep(1)
    async def send_photo(self, caption):
        if not self.url_photo: await self.send(caption); return
        try:
            s = await self._sess()
            payload = {"chat_id": self.chat_id, "photo": "https://images.unsplash.com/photo-1500382017468-9049fed747ef?w=800", "caption": caption}
            async with s.post(self.url_photo, json=payload, timeout=15) as r:
                if r.status!=200: await self.send(caption)
        except: await self.send(caption)

class GeminiGate:
    def __init__(self, key):
        self.key = key
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={key}" if key else None
    async def verify(self, setup, candles):
        if not self.key: return {"verdict":"APPROVE","confidence":0.80,"reason":"Panen Mode Bypass"}
        ctx = [{"o":round(c["open"],2),"h":round(c["high"],2),"l":round(c["low"],2),"c":round(c["close"],2)} for c in candles[-6:]]
        prompt = f"XAUUSD trader. Setup:{json.dumps(setup)} Context:{json.dumps(ctx)} Return JSON verdict APPROVE/REJECT confidence reason"
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(self.url, json={"contents":[{"parts":[{"text":prompt}]}], "generationConfig":{"temperature":0.05, "response_mime_type":"application/json"}}, timeout=10) as r:
                    if r.status==200:
                        j=await r.json(); txt=j["candidates"][0]["content"]["parts"][0]["text"]; return json.loads(txt)
        except: pass
        return {"verdict":"APPROVE","confidence":0.75,"reason":"Fail-Safe Panen"}

def run_colony(m5_closes, m5_high, m5_low):
    if len(m5_closes)<40: return 50,50, {"BUY":0,"SELL":0}
    closes=np.array(m5_closes); ma20=np.mean(closes[-20:]); ma40=np.mean(closes[-40:])
    trend="BUY" if ma20>ma40 else "SELL"
    pemetik = {"BUY":10 if trend=="BUY" else 0, "SELL":10 if trend=="SELL" else 0}
    mandor = {"BUY":5 if pemetik["BUY"]>pemetik["SELL"] else 0, "SELL":5 if pemetik["SELL"]>pemetik["BUY"] else 0}
    spread = m5_high[-1]-m5_low[-1]
    pembajak = {"BUY":0,"SELL":0,"BLOCK":0}
    if spread>MAX_SPREAD: pembajak["BLOCK"]=5
    else: pembajak["BUY" if mandor["BUY"]>mandor["SELL"] else "SELL"]=5
    penuai = {"BUY":5 if pembajak.get("BUY",0)>pembajak.get("SELL",0) else 0, "SELL":5 if pembajak.get("SELL",0)>pembajak.get("BUY",0) else 0}
    if pembajak["BLOCK"]>=3: return 50,50, {"BUY":0,"SELL":0,"BLOCK":5}
    buy = pemetik["BUY"]+mandor["BUY"]+pembajak.get("BUY",0)+penuai["BUY"]
    sell = pemetik["SELL"]+mandor["SELL"]+pembajak.get("SELL",0)+penuai["SELL"]
    return buy/25*100, sell/25*100, {"BUY":buy,"SELL":sell}

def load_json(path, default):
    if os.path.exists(path):
        try: return json.loads(Path(path).read_text())
        except: return default
    return default
def save_atomic(path, data):
    try:
        with tempfile.NamedTemporaryFile(mode='w', delete=False, dir=".") as tf:
            json.dump(data, tf); tmp=tf.name
        os.replace(tmp, path)
    except Exception as e: logger.error(f"Save err {e}")

class TitanV41:
    def __init__(self):
        self.tg = Telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self.ai = GeminiGate(GEMINI_API_KEY)
        self.fetcher = AntiBlokirFetcher()
        self.kalman_m5 = Kalman2D(); self.kalman_h1 = Kalman2D()
        self.m5 = []; self.h1 = []; self.active=None
        self.last_signal=0; self.daily_loss=0; self.last_day=datetime.now(timezone.utc).day
        self.no_signal_count=0; self.last_debug=0
        self.dna = load_json(".dna_tani_v4.json", {})
        self.memory = load_json(".memory_tani_v4.json", {"gudang":0,"panen_kecil":0,"panen_raya":0})
        self._load_state()

    def _load_state(self):
        try:
            if STATE_FILE.exists():
                d=json.loads(STATE_FILE.read_text())
                self.active=d.get("active_trade"); self.daily_loss=d.get("daily_loss",0)
        except: pass
    def _save_state(self):
        save_atomic(STATE_FILE, {"active_trade":self.active,"daily_loss":self.daily_loss,"last_day":self.last_day})
        save_atomic(".dna_tani_v4.json", self.dna); save_atomic(".memory_tani_v4.json", self.memory)

    def market_open(self):
        now=datetime.now(timezone.utc)
        if now.day!=self.last_day: self.daily_loss=0; self.last_day=now.day; self._save_state()
        wd=now.weekday(); h=now.hour
        if wd==4 and h>=21: return False
        if wd==5: return False
        if wd==6 and h<22: return False
        if self.daily_loss>=3: return False
        return True

    def macro_kalman(self):
        if len(self.h1)<12: return "NEUTRAL"
        closes=[c["close"] for c in self.h1]; _, vel=self.kalman_h1.update(closes[-1])
        ema5=np.mean(closes[-5:]); ema12=np.mean(closes[-12:])
        if ema5>ema12 and vel>0.03: return "BULLISH"
        if ema5<ema12 and vel<-0.03: return "BEARISH"
        return "NEUTRAL"

    def scan_debug(self, ofi):
        if len(self.m5)<LOOKBACK+10:
            logger.info(f"DEBUG SKIP len {len(self.m5)} < {LOOKBACK+10}")
            return None
        closes=np.array([c["close"] for c in self.m5]); highs=np.array([c["high"] for c in self.m5])
        lows=np.array([c["low"] for c in self.m5]); opens=np.array([c["open"] for c in self.m5])
        atr=MarketMath.wilder_atr(highs,lows,closes,14); chop=MarketMath.choppiness(highs,lows,closes,14)
        vec=MarketMath.impulse(self.m5, atr)
        swing_h=np.max(highs[-LOOKBACK-1:-1]); swing_l=np.min(lows[-LOOKBACK-1:-1])
        o,h,l,c=opens[-1],highs[-1],lows[-1],closes[-1]; rng=max(h-l,0.1); body=abs(c-o)
        buy_pct, sell_pct, colony = run_colony(closes.tolist(), highs.tolist(), lows.tolist())
        macro=self.macro_kalman(); _, vel=self.kalman_m5.update(c)

        # DEBUG kenapa ke-block (ini yang bikin lu gak ada sinyal dari kemarin)
        if atr < MIN_ATR:
            if self.no_signal_count%20==0: logger.info(f"BLOCK ATR {atr:.2f} < {MIN_ATR}")
            return None
        if chop > MAX_CHOP:
            if self.no_signal_count%20==0: logger.info(f"BLOCK CHOP {chop:.1f} > {MAX_CHOP}")
            return None
        if vec < MIN_VECTOR:
            if self.no_signal_count%20==0: logger.info(f"BLOCK VECTOR {vec:.2f} < {MIN_VECTOR} c={c:.2f}")
            return None
        if body/rng < MIN_BODY_RATIO:
            if self.no_signal_count%20==0: logger.info(f"BLOCK BODY {body/rng:.2f} < {MIN_BODY_RATIO}")
            return None

        bullish = (l < swing_l and c > swing_l and min(o,c)-l >= MIN_WICK_RATIO*rng and body/rng>=MIN_BODY_RATIO and vel>-0.20)
        bearish = (h > swing_h and c < swing_h and h-max(o,c) >= MIN_WICK_RATIO*rng and body/rng>=MIN_BODY_RATIO and vel<0.20)

        super_buy = buy_pct>=90 and vec>=0.7
        super_sell = sell_pct>=90 and vec>=0.7

        # MODE PANEN: OFI lebih longgar +-0.5
        if bullish and (buy_pct>=QUORUM_KECIL or super_buy) and ofi>-0.50:
            entry=round(c,2); sl=round(l - max(0.60*atr,1.0),2); risk=entry-sl
            if risk<=0.5 or risk>MAX_RISK_PER_TRADE: return None
            tp=round(entry+max(MIN_TP, risk*2.5),2) # RR 1:2.5 biar gampang TP
            return {"action":"BUY","entry":entry,"sl":sl,"tp":tp,"risk":round(risk,2),"reward":round(tp-entry,2),"rrr":2.5,"vector":round(vec,2),"macro":macro,"atr":round(atr,2),"chop":round(chop,1),"colony_buy":buy_pct,"colony_sell":sell_pct,"ofi":ofi,"super":super_buy}
        if bearish and (sell_pct>=QUORUM_KECIL or super_sell) and ofi<0.50:
            entry=round(c,2); sl=round(h + max(0.60*atr,1.0),2); risk=sl-entry
            if risk<=0.5 or risk>MAX_RISK_PER_TRADE: return None
            tp=round(entry-max(MIN_TP, risk*2.5),2)
            return {"action":"SELL","entry":entry,"sl":sl,"tp":tp,"risk":round(risk,2),"reward":round(entry-tp,2),"rrr":2.5,"vector":round(vec,2),"macro":macro,"atr":round(atr,2),"chop":round(chop,1),"colony_buy":buy_pct,"colony_sell":sell_pct,"ofi":ofi,"super":super_sell}
        if self.no_signal_count%30==0:
            logger.info(f"NO SETUP c={c:.2f} vec={vec:.2f} chop={chop:.1f} atr={atr:.2f} colony B{buy_pct:.0f}/S{sell_pct:.0f} ofi={ofi:+.2f} swingH={swing_h:.2f} swingL={swing_l:.2f}")
        self.no_signal_count+=1
        return None

    async def manage(self, bar):
        if not self.active: return
        t=self.active; h=bar["high"]; l=bar["low"]
        if "BUY" in t["action"]:
            if not t.get("bep") and h >= t["entry"] + BEP_TRIGGER:
                t["sl"]=round(t["entry"]+BEP_PLUS,2); t["bep"]=True; self._save_state()
                await self.tg.send(f"🛡️ BEP BUY {SYMBOL} -> {t['sl']}")
            if h>=t["tp"]:
                await self.tg.send(f"✅ TP BUY +{t['reward']} Pts RR 1:{t['rrr']} | Gudang +{15 if not t.get('super') else 32}$")
                self.memory["gudang"]+=32 if t.get("super") else 15; self.active=None; self._save_state(); return
            if l<=t["sl"]:
                if not t.get("bep"): self.daily_loss+=1
                await self.tg.send(f"❌ CLOSE {'BEP' if t.get('bep') else f'SL -{t['risk']}'} Loss {self.daily_loss}/3")
                self.active=None; self._save_state(); return
        else:
            if not t.get("bep") and l <= t["entry"] - BEP_TRIGGER:
                t["sl"]=round(t["entry"]-BEP_PLUS,2); t["bep"]=True; self._save_state()
                await self.tg.send(f"🛡️ BEP SELL {SYMBOL} -> {t['sl']}")
            if l<=t["tp"]:
                await self.tg.send(f"✅ TP SELL +{t['reward']} Pts RR 1:{t['rrr']} | Gudang +{15 if not t.get('super') else 32}$")
                self.memory["gudang"]+=32 if t.get("super") else 15; self.active=None; self._save_state(); return
            if h>=t["sl"]:
                if not t.get("bep"): self.daily_loss+=1
                await self.tg.send(f"❌ CLOSE {'BEP' if t.get('bep') else f'SL -{t['risk']}'} Loss {self.daily_loss}/3")
                self.active=None; self._save_state(); return

    async def broadcast(self, s):
        jenis="PANEN RAYA 👑" if s.get("super") else "PANEN KECIL"
        caption=(
            f"🌾 {jenis} {s['action']} {s['colony_buy']:.0f}% vs {s['colony_sell']:.0f}%\n"
            f"{'BUY 🟢' if s['action']=='BUY' else 'SELL 🔴'} Vec {s['vector']} OFI {s['ofi']:+.2f} CHOP {s['chop']} ATR {s['atr']}\n"
            f"Entry {s['entry']:.2f} SL {s['sl']:.2f} TP {s['tp']:.2f} RR 1:{s['rrr']}\n"
            f"Auto BEP +{BEP_PLUS} @ +{BEP_TRIGGER} | MIN_TP {MIN_TP}\n"
            f"🏚️ Gudang {self.memory['gudang']}$"
        )
        await self.tg.send_photo(caption)

    async def run(self):
        await self.tg.send(f"🔱 TITAN V4.1 MODE PANEN ONLINE {SYMBOL} | Vec>{MIN_VECTOR} Quorum>{QUORUM_KECIL}% Chop<{MAX_CHOP} RR 1:2.5 | Fix No Signal")
        while True:
            try:
                if not self.market_open():
                    await asyncio.sleep(60*30); continue
                async with websockets.connect(DERIV_WS_URL, ping_interval=20, ping_timeout=20) as ws:
                    logger.info("WS Connected V4.1 PANEN")
                    await ws.send(json.dumps({"ticks_history": SYMBOL, "count": 50, "end": "latest", "style": "candles", "granularity": GRAN_H1, "req_id": REQ_H1}))
                    await ws.send(json.dumps({"ticks_history": SYMBOL, "count": 40, "end": "latest", "style": "candles", "granularity": GRAN_M5, "req_id": REQ_M5}))
                    last_poll=time.time(); last_ofi=time.time(); _, ofi = self.fetcher.get_ofi_sync(); ofi=ofi or 0.0
                    while self.market_open():
                        if time.time()-last_ofi>=10:
                            _, new_ofi = self.fetcher.get_ofi_sync()
                            if new_ofi is not None: ofi=new_ofi; last_ofi=time.time()
                        if time.time()-last_poll>=15:
                            await ws.send(json.dumps({"ticks_history": SYMBOL, "count": 40, "end": "latest", "style": "candles", "granularity": GRAN_M5, "req_id": REQ_M5})); last_poll=time.time()
                        try: raw=await asyncio.wait_for(ws.recv(), timeout=10)
                        except asyncio.TimeoutError:
                            if time.time()-self.last_debug>1800: # tiap 30 menit debug kalau gak ada sinyal
                                self.last_debug=time.time()
                                await self.tg.send(f"⏳ STANDBY {SYMBOL} No Signal {self.no_signal_count}x | OFI {ofi:+.2f} | Loss {self.daily_loss}/3 | Mode Panen V4.1")
                            continue
                        msg=json.loads(raw)
                        if msg.get("error"): continue
                        if msg.get("msg_type")!="candles": continue
                        if msg.get("req_id")==REQ_H1: self.h1=msg.get("candles",[])
                        elif msg.get("req_id")==REQ_M5:
                            candles=msg.get("candles",[])
                            if len(candles)<20: continue
                            closed=candles[:-1]
                            if not self.m5: self.m5=closed[-80:]; continue
                            known={c["epoch"] for c in self.m5}; new_bars=[c for c in closed if c["epoch"] not in known]; new_bars.sort(key=lambda x: x["epoch"])
                            self.m5=closed[-80:]
                            for bar in new_bars:
                                await self.manage(bar)
                                if self.active: continue
                                if time.time()-self.last_signal<SIGNAL_COOLDOWN: continue
                                setup=self.scan_debug(ofi)
                                if setup:
                                    ai=await self.ai.verify(setup, self.m5)
                                    logger.info(f"SETUP {setup['action']} V{setup['vector']} COL B{setup['colony_buy']:.0f} S{setup['colony_sell']:.0f} OFI {ofi:+.2f} AI {ai}")
                                    if ai["verdict"]=="APPROVE" and ai.get("confidence",0)>=0.65: # dari 0.72 -> 0.65
                                        setup["ai_conf"]=int(ai["confidence"]*100); setup["bep"]=False
                                        self.active=setup; self.last_signal=time.time(); self.no_signal_count=0; self._save_state()
                                        await self.broadcast(setup)
            except ConnectionClosed: await asyncio.sleep(3)
            except Exception as e:
                logger.error(f"Loop Error {e}", exc_info=True); await asyncio.sleep(5)

if __name__=="__main__":
    bot=TitanV41()
    asyncio.run(bot.run())
