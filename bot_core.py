"""
🔱🌾 TITAN V4 TANI HYBRID FINAL SEMPURNA
Gabungan:
- TITAN V3 LIVE: Async Deriv WS, Kalman 2D, ATR Wilder, Choppiness, Impulse Vector, BEP Lock, Daily Loss 3x, Gemini AI Gate, RR 1:3
- TANI V8.8.2: OFI Binance, Colony 25 Petani, DNA Evolusi, Gudang $, Weekend Evaluasi, Foto Sawah, Quorum, AntiSpam

BUANG: yfinance, offset -2.25 random, logic random.choice
AMBIL: Yang live & deterministik saja
"""
import asyncio, json, logging, math, os, sys, time, tempfile, hashlib, requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import deque
import numpy as np
import websockets
from websockets.exceptions import ConnectionClosed
import aiohttp

# === CONFIG ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("TITAN_V4_HYBRID")

DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"
SYMBOL = os.getenv("TARGET_SYMBOL", "frxXAUUSD").strip()
STATE_FILE = Path(os.getenv("STATE_FILE", "titan_v4_state.json"))
DNA_FILE = Path(".dna_tani_v4.json")
MEMORY_FILE = Path(".memory_tani_v4.json")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or os.getenv("TELEGRAM_TOKEN","").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

GRAN_M5 = 300
GRAN_H1 = 3600
LOOKBACK = 28
MIN_TP = 18.0
BEP_TRIGGER = 5.0
BEP_PLUS = 1.0
MIN_VECTOR = 0.40
MAX_RISK_PER_TRADE = 15.0
SIGNAL_COOLDOWN = 900
QUORUM_KECIL = 32
QUORUM_RAYA = 55
MAX_SPREAD = 9.0

REQ_H1 = 101
REQ_M5 = 102

# === KALMAN & MATH dari TITAN V3 (PERTAHANKAN) ===
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

# === OFI dari TANI V8.8.2 (BAIKNYA DIAMBIL) ===
class AntiBlokirFetcher:
    def __init__(self):
        self.urls = [
            "https://api.binance.com/api/v3/depth?symbol=PAXGUSDT&limit=20",
            "https://api1.binance.com/api/v3/depth?symbol=PAXGUSDT&limit=20",
            "https://data-api.binance.vision/api/v3/depth?symbol=PAXGUSDT&limit=20"
        ]
    def get_ofi_sync(self):
        for url in self.urls:
            try:
                r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=5)
                if r.status_code==200:
                    d=r.json(); bids=d['bids']; asks=d['asks']
                    bv=sum(float(q) for _,q in bids[:5]); av=sum(float(q) for _,q in asks[:5])
                    ofi=(bv-av)/(bv+av+1e-9)
                    price=(float(bids[0][0])+float(asks[0][0]))/2
                    return price, float(ofi)
            except: continue
        return None, 0.0

# === TELEGRAM dari TITAN V3 + FOTO dari TANI ===
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
        if not self.url_photo:
            print(caption); return
        try:
            s = await self._sess()
            payload = {"chat_id": self.chat_id, "photo": "https://images.unsplash.com/photo-1500382017468-9049fed747ef?w=800", "caption": caption}
            async with s.post(self.url_photo, json=payload, timeout=15) as r:
                if r.status==200: logger.info("Foto sinyal terkirim")
        except Exception as e:
            logger.error(f"Foto fail {e}")
            await self.send(caption)

class GeminiGate:
    def __init__(self, key):
        self.key = key
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={key}" if key else None
    async def verify(self, setup, candles):
        if not self.key: return {"verdict":"APPROVE","confidence":0.85,"reason":"No Key Bypass - Colony + Vector kuat"}
        ctx = [{"o":round(c["open"],2),"h":round(c["high"],2),"l":round(c["low"],2),"c":round(c["close"],2)} for c in candles[-8:]]
        prompt = f"Act as strict XAUUSD trader. Setup:{json.dumps(setup)} Context:{json.dumps(ctx)} Return ONLY JSON verdict APPROVE/REJECT confidence reason"
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(self.url, json={"contents":[{"parts":[{"text":prompt}]}], "generationConfig":{"temperature":0.05, "response_mime_type":"application/json"}}, timeout=10) as r:
                    if r.status==200:
                        j=await r.json(); txt=j["candidates"][0]["content"]["parts"][0]["text"]; return json.loads(txt)
        except: pass
        return {"verdict":"REJECT","confidence":0,"reason":"Fail-Safe"}

# === COLONY 25 PETANI dari TANI (DETERMINISTIK, TANPA RANDOM) ===
def run_colony_deterministik(m5_closes, m5_high, m5_low):
    if len(m5_closes)<50: return 50,50, {"BUY":0,"SELL":0}
    closes=np.array(m5_closes); ma20=np.mean(closes[-20:]); ma50=np.mean(closes[-50:])
    trend="BUY" if ma20>ma50 else "SELL"
    # 10 Pemetik = ikuti MA
    pemetik = {"BUY":10 if trend=="BUY" else 0, "SELL":10 if trend=="SELL" else 0}
    # 5 Mandor = ikuti Pemetik
    mandor = {"BUY":5 if pemetik["BUY"]>pemetik["SELL"] else 0, "SELL":5 if pemetik["SELL"]>pemetik["BUY"] else 0}
    # 5 Pembajak = cek spread
    spread = m5_high[-1]-m5_low[-1]
    pembajak = {"BUY":0,"SELL":0,"BLOCK":0}
    if spread>MAX_SPREAD: pembajak["BLOCK"]=5
    else: pembajak["BUY" if mandor["BUY"]>mandor["SELL"] else "SELL"]=5
    # 5 Penuai = final
    penuai = {"BUY":5 if pembajak.get("BUY",0)>pembajak.get("SELL",0) else 0, "SELL":5 if pembajak.get("SELL",0)>pembajak.get("BUY",0) else 0}
    if pembajak["BLOCK"]>=3: return 50,50, {"BUY":0,"SELL":0,"BLOCK":5}
    buy = pemetik["BUY"]+mandor["BUY"]+pembajak.get("BUY",0)+penuai["BUY"]
    sell = pemetik["SELL"]+mandor["SELL"]+pembajak.get("SELL",0)+penuai["SELL"]
    return buy/25*100, sell/25*100, {"BUY":buy,"SELL":sell}

def load_json_file(path, default):
    if os.path.exists(path):
        try: return json.loads(Path(path).read_text())
        except: return default
    return default
def save_json_atomic(path, data):
    try:
        with tempfile.NamedTemporaryFile(mode='w', delete=False, dir=".") as tf:
            json.dump(data, tf); tmp=tf.name
        os.replace(tmp, path)
    except Exception as e: logger.error(f"Save err {e}")

class TitanV4Hybrid:
    def __init__(self):
        self.tg = Telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self.ai = GeminiGate(GEMINI_API_KEY)
        self.fetcher = AntiBlokirFetcher()
        self.kalman_m5 = Kalman2D(); self.kalman_h1 = Kalman2D()
        self.m5 = []; self.h1 = []; self.active=None
        self.last_signal=0; self.daily_loss=0; self.last_day=datetime.now(timezone.utc).day
        self.dna = load_json_file(DNA_FILE, {})
        self.memory = load_json_file(MEMORY_FILE, {"gudang":0,"panen_kecil":0,"panen_raya":0,"evolutions":0})
        self._load_state()

    def _load_state(self):
        try:
            if STATE_FILE.exists():
                d=json.loads(STATE_FILE.read_text())
                self.active=d.get("active_trade"); self.daily_loss=d.get("daily_loss",0); self.last_day=d.get("last_day",self.last_day)
        except: pass
    def _save_state(self):
        save_json_atomic(STATE_FILE, {"active_trade":self.active,"daily_loss":self.daily_loss,"last_day":self.last_day})
        save_json_atomic(DNA_FILE, self.dna); save_json_atomic(MEMORY_FILE, self.memory)

    def market_open(self):
        now=datetime.now(timezone.utc)
        if now.day!=self.last_day: self.daily_loss=0; self.last_day=now.day; self._save_state()
        # Weekend OFF dari TANI - PERBAIKAN TANI (MANTAP)
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
        if ema5>ema12 and vel>0.05: return "BULLISH"
        if ema5<ema12 and vel<-0.05: return "BEARISH"
        return "NEUTRAL"

    def scan_titan_plus_tani(self, ofi_price, ofi):
        if len(self.m5)<LOOKBACK+15: return None
        closes=np.array([c["close"] for c in self.m5]); highs=np.array([c["high"] for c in self.m5])
        lows=np.array([c["low"] for c in self.m5]); opens=np.array([c["open"] for c in self.m5])
        atr=MarketMath.wilder_atr(highs,lows,closes,14); chop=MarketMath.choppiness(highs,lows,closes,14)
        if atr<0.9: return None
        if chop>58: return None
        vec=MarketMath.impulse(self.m5, atr)
        if vec<MIN_VECTOR: return None

        swing_h=np.max(highs[-LOOKBACK-1:-1]); swing_l=np.min(lows[-LOOKBACK-1:-1])
        o,h,l,c=opens[-1],highs[-1],lows[-1],closes[-1]; rng=max(h-l,0.1); body=abs(c-o)
        if body/rng<0.45: return None
        lower_wick=min(o,c)-l; upper_wick=h-max(o,c); macro=self.macro_kalman(); _, vel=self.kalman_m5.update(c)

        # Colony TANI
        buy_pct, sell_pct, colony = run_colony_deterministik(closes.tolist(), highs.tolist(), lows.tolist())
        if colony.get("BLOCK",0)>=3: return None

        bullish_titan = (l < swing_l and c > swing_l and lower_wick/rng>=0.38 and body/rng>=0.45 and macro in ("BULLISH","NEUTRAL") and vel>-0.15)
        bearish_titan = (h > swing_h and c < swing_h and upper_wick/rng>=0.38 and body/rng>=0.45 and macro in ("BEARISH","NEUTRAL") and vel<0.15)

        # HYBRID QUORUM (BAIKNYA DARI KEDUA BOT)
        super_buy = buy_pct>=95 and vec>=0.9
        super_sell = sell_pct>=95 and vec>=0.9

        if bullish_titan and (buy_pct>=QUORUM_KECIL or super_buy) and ofi>-0.35:
            entry=round(c,2); sl=round(l - max(0.65*atr,1.3),2); risk=entry-sl
            if risk<=0.6 or risk>MAX_RISK_PER_TRADE: return None
            tp=round(entry+max(MIN_TP, risk*3.0),2)
            return {"action":"BUY","entry":entry,"sl":sl,"tp":tp,"risk":round(risk,2),"reward":round(tp-entry,2),"rrr":3.0,"vector":round(vec,2),"macro":macro,"atr":round(atr,2),"chop":round(chop,1),"colony_buy":buy_pct,"colony_sell":sell_pct,"ofi":ofi,"super":super_buy}
        if bearish_titan and (sell_pct>=QUORUM_KECIL or super_sell) and ofi<0.35:
            entry=round(c,2); sl=round(h + max(0.65*atr,1.3),2); risk=sl-entry
            if risk<=0.6 or risk>MAX_RISK_PER_TRADE: return None
            tp=round(entry-max(MIN_TP, risk*3.0),2)
            return {"action":"SELL","entry":entry,"sl":sl,"tp":tp,"risk":round(risk,2),"reward":round(entry-tp,2),"rrr":3.0,"vector":round(vec,2),"macro":macro,"atr":round(atr,2),"chop":round(chop,1),"colony_buy":buy_pct,"colony_sell":sell_pct,"ofi":ofi,"super":super_sell}
        # SUPER tanpa OFI filter
        if super_buy and bullish_titan:
            entry=round(c,2); sl=round(l - max(0.65*atr,1.3),2); risk=entry-sl; tp=round(entry+max(MIN_TP, risk*3.0),2)
            return {"action":"BUY","entry":entry,"sl":sl,"tp":tp,"risk":round(risk,2),"reward":round(tp-entry,2),"rrr":3.0,"vector":round(vec,2),"macro":macro,"atr":round(atr,2),"chop":round(chop,1),"colony_buy":buy_pct,"colony_sell":sell_pct,"ofi":ofi,"super":True}
        if super_sell and bearish_titan:
            entry=round(c,2); sl=round(h + max(0.65*atr,1.3),2); risk=sl-entry; tp=round(entry-max(MIN_TP, risk*3.0),2)
            return {"action":"SELL","entry":entry,"sl":sl,"tp":tp,"risk":round(risk,2),"reward":round(entry-tp,2),"rrr":3.0,"vector":round(vec,2),"macro":macro,"atr":round(atr,2),"chop":round(chop,1),"colony_buy":buy_pct,"colony_sell":sell_pct,"ofi":ofi,"super":True}
        return None

    async def manage(self, bar):
        if not self.active: return
        t=self.active; h=bar["high"]; l=bar["low"]
        if "BUY" in t["action"]:
            if not t.get("bep") and h >= t["entry"] + BEP_TRIGGER:
                t["sl"]=round(t["entry"]+BEP_PLUS,2); t["bep"]=True; self._save_state()
                await self.tg.send(f"🛡️ BEP LOCKED BUY {SYMBOL} SL->{t['sl']}")
            if h>=t["tp"]:
                await self.tg.send(f"✅ TP HIT BUY +{t['reward']} Pts @ {t['tp']} | RR 1:3 | Colony {t.get('colony_buy',0):.0f}%")
                self.memory["gudang"]+=32 if t.get("super") else 15; self.memory["panen_raya" if t.get("super") else "panen_kecil"]+=1; self.active=None; self._save_state(); return
            if l<=t["sl"]:
                if not t.get("bep"): self.daily_loss+=1
                await self.tg.send(f"❌ CLOSE {'BEP' if t.get('bep') else f'SL -{t['risk']}'} @ {t['sl']} | Loss {self.daily_loss}/3")
                self.active=None; self._save_state(); return
        else:
            if not t.get("bep") and l <= t["entry"] - BEP_TRIGGER:
                t["sl"]=round(t["entry"]-BEP_PLUS,2); t["bep"]=True; self._save_state()
                await self.tg.send(f"🛡️ BEP LOCKED SELL {SYMBOL} SL->{t['sl']}")
            if l<=t["tp"]:
                await self.tg.send(f"✅ TP HIT SELL +{t['reward']} Pts @ {t['tp']} | RR 1:3 | Colony {t.get('colony_sell',0):.0f}%")
                self.memory["gudang"]+=32 if t.get("super") else 15; self.memory["panen_raya" if t.get("super") else "panen_kecil"]+=1; self.active=None; self._save_state(); return
            if h>=t["sl"]:
                if not t.get("bep"): self.daily_loss+=1
                await self.tg.send(f"❌ CLOSE {'BEP' if t.get('bep') else f'SL -{t['risk']}'} @ {t['sl']} | Loss {self.daily_loss}/3")
                self.active=None; self._save_state(); return

    async def broadcast_hybrid(self, s):
        # Foto sawah TANI + data TITAN
        jenis="PANEN RAYA" if s.get("super") else "PANEN KECIL"
        emoji="🌾👑🔥" if jenis=="PANEN RAYA" else "🌿"
        bar_gudang="🟩"*min(10,self.memory["gudang"]//15)+"⬜"*(10-min(10,self.memory["gudang"]//15))
        caption=(
            f"{emoji} TITAN V4 {jenis} {s['action']} {s['colony_buy']:.0f}% vs {s['colony_sell']:.0f}%\n"
            f"{'BUY 🟢' if s['action']=='BUY' else 'SELL 🔴'} Vec {s['vector']} Macro {s['macro']} OFI {s['ofi']:+.2f} CHOP {s['chop']}\n"
            f"Entry {s['entry']:.2f} SL {s['sl']:.2f} (-{s['risk']}) TP {s['tp']:.2f} (+{s['reward']}) RR 1:{s['rrr']}\n"
            f"ATR {s['atr']} | Auto BEP +{BEP_PLUS} @ +{BEP_TRIGGER}\n"
            f"🏚️ GUDANG {bar_gudang} {self.memory['gudang']}$ | Kecil {self.memory['panen_kecil']}x Raya {self.memory['panen_raya']}x\n"
            f"🧬 TITAN Kalman + TANI Colony Hybrid"
        )
        await self.tg.send_photo(caption)

    async def run(self):
        await self.tg.send(f"🔱 TITAN V4 TANI HYBRID ONLINE {SYMBOL} | Vec>{MIN_VECTOR} | Quorum {QUORUM_KECIL}% | RR 1:3")
        while True:
            try:
                if not self.market_open():
                    # Weekend Evaluasi dari TANI (PERTAHANKAN)
                    _, ofi = self.fetcher.get_ofi_sync()
                    if ofi is not None:
                        caption=f"🌴 V4 WEEKEND EVALUASI\nGudang {self.memory['gudang']}$ | OFI {ofi:+.2f} | Loss {self.daily_loss}/3 | Auto ON Senin 05:00 WIB"
                        await self.tg.send(caption)
                    await asyncio.sleep(3600*6); continue
                async with websockets.connect(DERIV_WS_URL, ping_interval=20, ping_timeout=20) as ws:
                    logger.info("WS Connected")
                    await ws.send(json.dumps({"ticks_history": SYMBOL, "count": 50, "end": "latest", "style": "candles", "granularity": GRAN_H1, "req_id": REQ_H1}))
                    await ws.send(json.dumps({"ticks_history": SYMBOL, "count": LOOKBACK+40, "end": "latest", "style": "candles", "granularity": GRAN_M5, "req_id": REQ_M5}))
                    last_poll=time.time(); last_ofi=time.time(); ofi_price, ofi = self.fetcher.get_ofi_sync(); ofi=ofi or 0.0
                    while self.market_open():
                        if time.time()-last_ofi>=10:
                            _, new_ofi = self.fetcher.get_ofi_sync()
                            if new_ofi is not None: ofi=new_ofi; last_ofi=time.time()
                        if time.time()-last_poll>=15:
                            await ws.send(json.dumps({"ticks_history": SYMBOL, "count": LOOKBACK+40, "end": "latest", "style": "candles", "granularity": GRAN_M5, "req_id": REQ_M5})); last_poll=time.time()
                        try: raw=await asyncio.wait_for(ws.recv(), timeout=10)
                        except asyncio.TimeoutError: continue
                        msg=json.loads(raw)
                        if msg.get("error"): continue
                        if msg.get("msg_type")!="candles": continue
                        if msg.get("req_id")==REQ_H1: self.h1=msg.get("candles",[])
                        elif msg.get("req_id")==REQ_M5:
                            candles=msg.get("candles",[]); 
                            if len(candles)<20: continue
                            closed=candles[:-1]
                            if not self.m5: self.m5=closed[-80:]; continue
                            known={c["epoch"] for c in self.m5}; new_bars=[c for c in closed if c["epoch"] not in known]; new_bars.sort(key=lambda x: x["epoch"])
                            self.m5=closed[-80:]
                            for bar in new_bars:
                                await self.manage(bar)
                                if self.active: continue
                                if time.time()-self.last_signal<SIGNAL_COOLDOWN: continue
                                setup=self.scan_titan_plus_tani(ofi_price, ofi)
                                if setup:
                                    ai=await self.ai.verify(setup, self.m5)
                                    logger.info(f"Scan {setup['action']} Vec {setup['vector']} Colony {setup['colony_buy']:.0f}/{setup['colony_sell']:.0f} OFI {ofi:+.2f} AI {ai}")
                                    if ai["verdict"]=="APPROVE" and ai.get("confidence",0)>=0.72:
                                        setup["ai_conf"]=int(ai["confidence"]*100); setup["bep"]=False
                                        self.active=setup; self.last_signal=time.time(); self._save_state()
                                        await self.broadcast_hybrid(setup)
            except ConnectionClosed: await asyncio.sleep(3)
            except Exception as e:
                logger.error(f"Loop Error {e}", exc_info=True); await asyncio.sleep(5)

if __name__=="__main__":
    bot=TitanV4Hybrid()
    asyncio.run(bot.run())
