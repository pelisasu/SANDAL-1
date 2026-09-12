import asyncio
import json
import logging
import math
import os
import sys
import time
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import numpy as np
import websockets
from websockets.exceptions import ConnectionClosed

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("TITAN_V3_LIVE")

DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"
SYMBOL = os.getenv("TARGET_SYMBOL", "frxXAUUSD").strip()
STATE_FILE = Path(os.getenv("STATE_FILE", "titan_v3_state.json"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
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
SIGNAL_COOLDOWN = 1200

REQ_H1 = 101
REQ_M5 = 102
REQ_SYMBOLS = 100

class Kalman2D:
    def __init__(self, q=1e-5, r=0.8):
        self.x = np.array([0.0, 0.0])
        self.P = np.eye(2) * 1.0
        self.Q = np.array([[q, 0],[0, q*15]])
        self.R = r
        self.F = np.array([[1., 1.],[0., 1.]])
        self.H = np.array([[1., 0.]])
        self.init = False
    def update(self, z: float):
        if not self.init:
            self.x[0] = z
            self.init = True
            return z, 0.0
        self.x = self.F @ self.x
        self.P = self.F @ self.P @ self.F.T + self.Q
        y = z - (self.H @ self.x)[0]
        S = (self.H @ self.P @ self.H.T)[0,0] + self.R
        K = (self.P @ self.H.T) / S
        self.x = self.x + K.flatten() * y
        self.P = (np.eye(2) - K @ self.H) @ self.P
        return float(self.x[0]), float(self.x[1])

class MarketMath:
    @staticmethod
    def wilder_atr(h, l, c, p=14):
        if len(c) < p+1: return float(np.mean(h - l))
        tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
        atr = np.zeros(len(tr))
        atr[p-1] = np.mean(tr[:p])
        for i in range(p, len(tr)):
            atr[i] = (atr[i-1]*(p-1) + tr[i]) / p
        return float(atr[-1])
    @staticmethod
    def choppiness(h, l, c, p=14):
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

class Telegram:
    def __init__(self, token, chat_id):
        self.token, self.chat_id = token, chat_id
        self.url = f"https://api.telegram.org/bot{token}/sendMessage" if token else None
        self.session = None
    async def _sess(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session
    async def send(self, txt):
        if not self.url: return
        for i in range(3):
            try:
                s = await self._sess()
                async with s.post(self.url, json={"chat_id": self.chat_id, "text": txt, "parse_mode": "HTML"}, timeout=10) as r:
                    if r.status == 200: return
            except Exception as e:
                logger.warning(f"TG err {e}")
                await asyncio.sleep(1)

class GeminiGate:
    def __init__(self, key):
        self.key = key
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={key}" if key else None
    async def verify(self, setup, candles):
        if not self.key:
            return {"verdict": "APPROVE", "confidence": 0.80, "reason": "No Key Bypass"}
        ctx = [{"o": round(c["open"],2), "h": round(c["high"],2), "l": round(c["low"],2), "c": round(c["close"],2)} for c in candles[-8:]]
        prompt = f"Act as strict XAUUSD trader. Reject traps. Setup:{json.dumps(setup)} Context:{json.dumps(ctx)} Return ONLY JSON verdict APPROVE/REJECT confidence reason"
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(self.url, json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.05, "response_mime_type": "application/json"}}, timeout=10) as r:
                    if r.status == 200:
                        j = await r.json()
                        txt = j["candidates"][0]["content"]["parts"][0]["text"]
                        p = json.loads(txt)
                        return p if "verdict" in p else {"verdict": "REJECT", "confidence": 0, "reason": "Bad JSON"}
        except Exception as e:
            logger.error(f"Gemini {e}")
        return {"verdict": "REJECT", "confidence": 0, "reason": "Fail-Safe Reject"}

class TitanV3:
    def __init__(self):
        self.tg = Telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self.ai = GeminiGate(GEMINI_API_KEY)
        self.kalman_m5 = Kalman2D()
        self.kalman_h1 = Kalman2D()
        self.m5 = []
        self.h1 = []
        self.active = None
        self.last_h1 = 0
        self.last_signal = 0
        self.daily_loss = 0
        self.last_day = datetime.now(timezone.utc).day
        self._load()

    def _load(self):
        try:
            if STATE_FILE.exists():
                d = json.loads(STATE_FILE.read_text())
                self.active = d.get("active_trade")
                self.daily_loss = d.get("daily_loss", 0)
                self.last_day = d.get("last_day", self.last_day)
        except Exception as e:
            logger.error(f"Load err {e}")

    def _save(self):
        try:
            data = {"active_trade": self.active, "daily_loss": self.daily_loss, "last_day": self.last_day}
            with tempfile.NamedTemporaryFile(mode='w', delete=False, dir=".") as tf:
                json.dump(data, tf)
                tmp_name = tf.name
            os.replace(tmp_name, STATE_FILE)
        except Exception as e:
            logger.error(f"Save err {e}")

    def market_open(self):
        now = datetime.now(timezone.utc)
        if now.day!= self.last_day:
            self.daily_loss = 0
            self.last_day = now.day
            self._save()
        if now.weekday() == 5 and now.hour >= 21: return False
        if now.weekday() == 6 and now.hour < 22: return False
        if now.weekday() == 6: return False
        if self.daily_loss >= 3: return False
        return True

    def macro(self):
        if len(self.h1) < 12: return "NEUTRAL"
        closes = [c["close"] for c in self.h1]
        _, vel = self.kalman_h1.update(closes[-1])
        ema5 = np.mean(closes[-5:])
        ema12 = np.mean(closes[-12:])
        if ema5 > ema12 and vel > 0.05: return "BULLISH"
        if ema5 < ema12 and vel < -0.05: return "BEARISH"
        return "NEUTRAL"

    def scan(self):
        if len(self.m5) < LOOKBACK + 15: return None
        closes = np.array([c["close"] for c in self.m5])
        highs = np.array([c["high"] for c in self.m5])
        lows = np.array([c["low"] for c in self.m5])
        opens = np.array([c["open"] for c in self.m5])

        atr = MarketMath.wilder_atr(highs, lows, closes, 14)
        chop = MarketMath.choppiness(highs, lows, closes, 14)
        if atr < 0.9: return None
        if chop > 58: return None

        vec = MarketMath.impulse(self.m5, atr)
        if vec < MIN_VECTOR: return None

        swing_h = np.max(highs[-LOOKBACK-1:-1])
        swing_l = np.min(lows[-LOOKBACK-1:-1])

        o,h,l,c = opens[-1], highs[-1], lows[-1], closes[-1]
        rng = max(h-l, 0.1)
        body = abs(c-o)
        if body/rng < 0.45: return None

        lower_wick = min(o,c) - l
        upper_wick = h - max(o,c)
        macro = self.macro()
        _, vel = self.kalman_m5.update(c)

        bullish = (l < swing_l and c > swing_l and lower_wick/rng >= 0.38 and body/rng >= 0.45 and macro in ("BULLISH","NEUTRAL") and vel > -0.15)
        bearish = (h > swing_h and c < swing_h and upper_wick/rng >= 0.38 and body/rng >= 0.45 and macro in ("BEARISH","NEUTRAL") and vel < 0.15)

        if bullish:
            entry = round(c,2)
            sl = round(l - max(0.65*atr, 1.3), 2)
            risk = entry - sl
            if risk <= 0.6 or risk > MAX_RISK_PER_TRADE: return None
            tp = round(entry + max(MIN_TP, risk*3.0), 2)
            return {"action": "BUY", "entry": entry, "sl": sl, "tp": tp, "risk": round(risk,2), "reward": round(tp-entry,2), "rrr": 3.0, "vector": round(vec,2), "macro": macro, "atr": round(atr,2), "chop": round(chop,1)}
        if bearish:
            entry = round(c,2)
            sl = round(h + max(0.65*atr, 1.3), 2)
            risk = sl - entry
            if risk <= 0.6 or risk > MAX_RISK_PER_TRADE: return None
            tp = round(entry - max(MIN_TP, risk*3.0), 2)
            return {"action": "SELL", "entry": entry, "sl": sl, "tp": tp, "risk": round(risk,2), "reward": round(entry-tp,2), "rrr": 3.0, "vector": round(vec,2), "macro": macro, "atr": round(atr,2), "chop": round(chop,1)}
        return None

    async def manage(self, bar):
        if not self.active: return
        t = self.active
        h = bar["high"]
        l = bar["low"]

        if "BUY" in t["action"]:
            if not t.get("bep") and h >= t["entry"] + BEP_TRIGGER:
                t["sl"] = round(t["entry"] + BEP_PLUS, 2)
                t["bep"] = True
                self._save()
                msg_bep = f"🛡️ BEP LOCKED BUY {SYMBOL} Entry {t['entry']} SL -> {t['sl']} (+{BEP_PLUS})"
                await self.tg.send(msg_bep)
            if h >= t["tp"]:
                reward = t.get("reward", 0)
                tp_val = t.get("tp", 0)
                await self.tg.send(f"✅ TP HIT BUY +{reward} Pts @ {tp_val} | RR 1:3")
                self.active = None
                self._save()
                return
            if l <= t["sl"]:
                is_bep = t.get("bep", False)
                if not is_bep:
                    self.daily_loss += 1
                risk_val = t.get("risk", 0)
                sl_val = t.get("sl", 0)
                if is_bep:
                    close_msg = "BEP"
                else:
                    close_msg = f"SL -{risk_val}"
                await self.tg.send(f"❌ CLOSE {close_msg} @ {sl_val} | Loss streak: {self.daily_loss}/3")
                self.active = None
                self._save()
                return
        else:
            if not t.get("bep") and l <= t["entry"] - BEP_TRIGGER:
                t["sl"] = round(t["entry"] - BEP_PLUS, 2)
                t["bep"] = True
                self._save()
                msg_bep = f"🛡️ BEP LOCKED SELL {SYMBOL} Entry {t['entry']} SL -> {t['sl']} (+{BEP_PLUS})"
                await self.tg.send(msg_bep)
            if l <= t["tp"]:
                reward = t.get("reward", 0)
                tp_val = t.get("tp", 0)
                await self.tg.send(f"✅ TP HIT SELL +{reward} Pts @ {tp_val} | RR 1:3")
                self.active = None
                self._save()
                return
            if h >= t["sl"]:
                is_bep = t.get("bep", False)
                if not is_bep:
                    self.daily_loss += 1
                risk_val = t.get("risk", 0)
                sl_val = t.get("sl", 0)
                if is_bep:
                    close_msg = "BEP"
                else:
                    close_msg = f"SL -{risk_val}"
                await self.tg.send(f"❌ CLOSE {close_msg} @ {sl_val} | Loss streak: {self.daily_loss}/3")
                self.active = None
                self._save()
                return

    async def broadcast(self, s):
        action = s.get("action", "")
        entry = s.get("entry", 0)
        sl = s.get("sl", 0)
        tp = s.get("tp", 0)
        risk = s.get("risk", 0)
        reward = s.get("reward", 0)
        rrr = s.get("rrr", 0)
        atr = s.get("atr", 0)
        chop = s.get("chop", 0)
        vector = s.get("vector", 0)
        macro = s.get("macro", "")
        ai_conf = s.get("ai_conf", 0)

        text = (
            f"⚡ {action} IMPULSE V3\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Symbol: {SYMBOL} M5\n"
            f"Entry: {entry:.2f}\n"
            f"SL: {sl:.2f} (-{risk}) | TP: {tp:.2f} (+{reward})\n"
            f"RR: 1:{rrr} | ATR: {atr} | CHOP: {chop}\n"
            f"Vector: {vector} | Macro: {macro} | AI: {ai_conf}%\n"
            f"Auto BEP +{BEP_PLUS} @ +{BEP_TRIGGER}pts\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Live signal only"
        )
        await self.tg.send(text)

    async def run(self):
        await self.tg.send(f"🔱 TITAN V3.1 FIX ONLINE {SYMBOL} | RR 1:3 | Vector>{MIN_VECTOR} | Ready Senin")
        while True:
            try:
                if not self.market_open():
                    await asyncio.sleep(120)
                    continue
                async with websockets.connect(DERIV_WS_URL, ping_interval=20, ping_timeout=20) as ws:
                    logger.info("WS Connected")
                    await ws.send(json.dumps({"active_symbols": "brief", "req_id": REQ_SYMBOLS}))
                    await ws.send(json.dumps({"ticks_history": SYMBOL, "count": 50, "end": "latest", "style": "candles", "granularity": GRAN_H1, "req_id": REQ_H1}))
                    await ws.send(json.dumps({"ticks_history": SYMBOL, "count": LOOKBACK+40, "end": "latest", "style": "candles", "granularity": GRAN_M5, "req_id": REQ_M5}))
                    last_poll = time.time()
                    while self.market_open():
                        if time.time() - last_poll >= 15:
                            await ws.send(json.dumps({"ticks_history": SYMBOL, "count": LOOKBACK+40, "end": "latest", "style": "candles", "granularity": GRAN_M5, "req_id": REQ_M5}))
                            last_poll = time.time()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        except asyncio.TimeoutError:
                            continue
                        msg = json.loads(raw)
                        if msg.get("error"): continue
                        if msg.get("msg_type")!= "candles": continue
                        if msg.get("req_id") == REQ_H1:
                            self.h1 = msg.get("candles", [])
                        elif msg.get("req_id") == REQ_M5:
                            candles = msg.get("candles", [])
                            if len(candles) < 20: continue
                            closed = candles[:-1]
                            if not self.m5:
                                self.m5 = closed[-80:]
                                continue
                            known = {c["epoch"] for c in self.m5}
                            new_bars = [c for c in closed if c["epoch"] not in known]
                            new_bars.sort(key=lambda x: x["epoch"])
                            self.m5 = closed[-80:]
                            for bar in new_bars:
                                await self.manage(bar)
                                if self.active: continue
                                if time.time() - self.last_signal < SIGNAL_COOLDOWN: continue
                                setup = self.scan()
                                if setup:
                                    ai = await self.ai.verify(setup, self.m5)
                                    logger.info(f"Scan {setup['action']} Vec {setup['vector']} Chop {setup['chop']} AI {ai}")
                                    if ai["verdict"] == "APPROVE" and ai.get("confidence",0) >= 0.72:
                                        setup["ai_conf"] = int(ai["confidence"]*100)
                                        setup["bep"] = False
                                        self.active = setup
                                        self.last_signal = time.time()
                                        self._save()
                                        await self.broadcast(setup)
            except ConnectionClosed:
                await asyncio.sleep(3)
            except Exception as e:
                logger.error(f"Loop Error {e}", exc_info=True)
                await asyncio.sleep(5)

if __name__ == "__main__":
    bot = TitanV3()
    asyncio.run(bot.run())
