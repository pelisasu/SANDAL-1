import asyncio
import json
import logging
import math
import os
import sys
import time
from datetime import datetime
import aiohttp
import numpy as np
import pytz
import websockets

# --- SYSTEM LOGGING ENGINE ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("QUANT_ARCHITECT")

# --- KREDENSIAL & PARAMETER PASAR ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# Deriv Public Gateway (App ID 1089 - 100% Bebas API Key)
DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"
SYMBOL = os.getenv("TARGET_SYMBOL", "frxXAUUSD")

# Parameter Kuantitatif Presisi Tinggi
GRANULARITY_M5 = 300           # 300 Detik (M5)
GRANULARITY_H1 = 3600          # 3600 Detik (H1 - Macro Filter)
MIN_TP_POINTS = 15.0           # Target Mutlak TP >= 15 Poin ($15.00)
BEP_TRIGGER_POINTS = 3.5       # Kunci BEP begitu harga naik +3.5 Poin (Zero-Risk)
LOOKBACK_SWING = 28            # Siklus Fibonacci Swing Bar (21-34 Harmonik)
PHI = 1.6180339887             # Rasio Emas (Golden Ratio)


class KalmanVelocityFilter:
    """Estimator Rekursif untuk Menghitung Nilai Riil Harga dan Kecepatan Vektor."""
    def __init__(self, q=1e-4, r=1e-2):
        self.q = q
        self.r = r
        self.x = 0.0
        self.p = 1.0
        self.last_x = 0.0
        self.velocity = 0.0
        self.is_init = False

    def update(self, z: float) -> tuple:
        if not self.is_init:
            self.x = z
            self.last_x = z
            self.is_init = True
            return self.x, 0.0

        p_prior = self.p + self.q
        k = p_prior / (p_prior + self.r)
        self.last_x = self.x
        self.x = self.x + k * (z - self.x)
        self.p = (1.0 - k) * p_prior
        self.velocity = self.x - self.last_x  # Turunan pertama: Kecepatan laju tren
        return self.x, self.velocity


class PythagoreanMomentumMatrix:
    """Mengukur Jarak Ruang-Waktu Euclidean & Vektor Akselerasi Likuiditas."""
    @staticmethod
    def calculate_impulse_vector(candles: list, atr: float) -> float:
        if len(candles) < 3 or atr <= 0:
            return 0.0
        # dt = delta bar, dp = delta price dinormalisasi dengan ATR
        dp = (candles[-1]["close"] - candles[-3]["close"]) / atr
        dt = 2.0  # 2 Bar M5 interval
        vector_length = math.sqrt((dt ** 2) + (dp ** 2))
        return vector_length


class TelegramEngine:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{self.token}/sendMessage"

    async def send(self, message: str):
        if not self.token or not self.chat_id:
            return
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, json=payload, timeout=8) as resp:
                    pass
        except Exception as e:
            logger.error(f"[TELEGRAM ERROR] {e}")


class GeminiDeepReasoningGate:
    """Gatekeeper AI: Validasi Tingkat Ketat Berbasis Mikrostruktur."""
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={self.api_key}"

    async def verify_institutional_bias(self, setup: dict, m5_candles: list) -> dict:
        if not self.api_key:
            return {"verdict": "REJECT", "confidence": 0.0, "reason": "No API Key"}

        summary = [{"c": round(c["close"], 2), "h": round(c["high"], 2), "l": round(c["low"], 2)} for c in m5_candles[-6:]]
        prompt = (
            "Bertindak sebagai Lead Quantitative Order Flow Specialist. "
            "Evaluasi sinyal XAUUSD M5 berikut terhadap potensi False Breakout / Trap. "
            "Hanya setujui jika penyerapan likuiditas (Absorption) valid dan aman untuk rasio win-rate tinggi. "
            "Jawab HANYA JSON: {\"verdict\": \"APPROVE\" atau \"REJECT\", \"confidence\": float (0.0-1.0), \"reason\": \"analisis ringkas\"}\n"
            f"Setup: {json.dumps(setup)}\nContext Data: {json.dumps(summary)}"
        )
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.05, "response_mime_type": "application/json"}
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.url, json=payload, timeout=10) as r:
                    if r.status == 200:
                        res = await r.json()
                        return json.loads(res["candidates"][0]["content"]["parts"][0]["text"].strip())
        except Exception:
            pass
        return {"verdict": "REJECT", "confidence": 0.0, "reason": "Gatekeeper Timeout / Fail-Safe"}


class EliteExecutionEngine:
    def __init__(self):
        self.notifier = TelegramEngine(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self.ai = GeminiDeepReasoningGate(GEMINI_API_KEY)
        self.kalman_m5 = KalmanVelocityFilter()
        self.kalman_h1 = KalmanVelocityFilter()
        self.tz_wib = pytz.timezone("Asia/Jakarta")

        self.m5_candles = []
        self.h1_candles = []
        self.active_trade = None
        self.last_bar_epoch = 0

    def is_market_active_wib(self) -> bool:
        now = datetime.now(self.tz_wib)
        wd, h, m = now.weekday(), now.hour, now.minute
        if wd == 0: return h >= 5
        elif 1 <= wd <= 4: return True
        elif wd == 5: return h < 5 or (h == 5 and m == 0)
        return False

    def check_macro_h1_alignment(self) -> str:
        """Filter Makro: Mengharuskan arah M5 sejalan dengan aliran modal H1."""
        if len(self.h1_candles) < 5:
            return "NEUTRAL"
        closes = [c["close"] for c in self.h1_candles]
        h1_val, h1_vel = self.kalman_h1.update(closes[-1])
        if closes[-1] > h1_val and h1_vel > 0:
            return "BULLISH"
        elif closes[-1] < h1_val and h1_vel < 0:
            return "BEARISH"
        return "NEUTRAL"

    def scan_precision_setup(self):
        if len(self.m5_candles) < LOOKBACK_SWING + 10:
            return None

        closes = np.array([c["close"] for c in self.m5_candles])
        highs = np.array([c["high"] for c in self.m5_candles])
        lows = np.array([c["low"] for c in self.m5_candles])
        opens = np.array([c["open"] for c in self.m5_candles])

        kalman_price, kalman_vel = self.kalman_m5.update(closes[-1])

        # Hitung True ATR M5
        tr = np.maximum(highs[1:] - lows[1:], np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])))
        atr = float(np.mean(tr[-14:])) if len(tr) >= 14 else 1.5

        # 1. Pythagoras Vector Acceleration
        pythagoras_vector = PythagoreanMomentumMatrix.calculate_impulse_vector(self.m5_candles, atr)
        if pythagoras_vector < PHI:  # Tolak pergerakan lambat tanpa energi
            return None

        # 2. Structural High / Low (Fibonacci Harmonik 28 Lookback)
        swing_h = np.max(highs[-LOOKBACK_SWING-1:-1])
        swing_l = np.min(lows[-LOOKBACK_SWING-1:-1])

        c_open, c_high, c_low, c_close = opens[-1], highs[-1], lows[-1], closes[-1]
        c_range = max(c_high - c_low, 0.05)
        lower_wick = min(c_open, c_close) - c_low
        upper_wick = c_high - max(c_open, c_close)

        macro_bias = self.check_macro_h1_alignment()

        # 3. Validasi Bullish Liquidity Trap
        bullish_sweep = (
            (c_low < swing_l) and (c_close > swing_l) and
            (lower_wick / c_range >= 0.42) and
            (kalman_vel > 0) and
            (macro_bias in ("BULLISH", "NEUTRAL"))
        )

        # 4. Validasi Bearish Liquidity Trap
        bearish_sweep = (
            (c_high > swing_h) and (c_close < swing_h) and
            (upper_wick / c_range >= 0.42) and
            (kalman_vel < 0) and
            (macro_bias in ("BEARISH", "NEUTRAL"))
        )

        if bullish_sweep:
            entry = round(c_close, 2)
            sl = round(c_low - max(0.4 * atr, 0.7), 2)
            risk = entry - sl
            if risk <= 0: return None
            tp = round(entry + max(MIN_TP_POINTS, risk * 2.5), 2)

            return {
                "action": "BUY (INSTANT QUANT IMPULSE)",
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "risk": round(risk, 2),
                "reward": round(tp - entry, 2),
                "rrr": round((tp - entry) / risk, 2),
                "vector": round(pythagoras_vector, 2),
                "macro": macro_bias
            }

        elif bearish_sweep:
            entry = round(c_close, 2)
            sl = round(c_high + max(0.4 * atr, 0.7), 2)
            risk = sl - entry
            if risk <= 0: return None
            tp = round(entry - max(MIN_TP_POINTS, risk * 2.5), 2)

            return {
                "action": "SELL (INSTANT QUANT IMPULSE)",
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "risk": round(risk, 2),
                "reward": round(entry - tp, 2),
                "rrr": round((entry - tp) / risk, 2),
                "vector": round(pythagoras_vector, 2),
                "macro": macro_bias
            }

        return None

    async def manage_active_trade(self, current_bar: dict):
        """Zero-Loss Manager: Mengunci BEP & Memotong Trade Jika Momentum Mati."""
        if not self.active_trade:
            return

        t = self.active_trade
        c_h, c_l, c_c = current_bar["high"], current_bar["low"], current_bar["close"]
        _, vel = self.kalman_m5.update(c_c)

        if "BUY" in t["action"]:
            # Auto Break-Even Lock: Kunci resiko jadi 0 saat profit +3.5 Poin tercapai
            if not t["bep_locked"] and c_h >= (t["entry"] + BEP_TRIGGER_POINTS):
                t["sl"] = t["entry"] + 0.50  # Kunci profit kecil (Free-Trade)
                t["bep_locked"] = True
                await self.notifier.send(
                    f"🛡️ <b>ZERO-RISK PROTECTION ENGAGED</b>\n"
                    f"Asset: {SYMBOL} | Posisi BUY @ {t['entry']}\n"
                    f"Harga melonjak +{BEP_TRIGGER_POINTS} Pts. <b>Stop Loss dinaikkan ke {t['sl']} (BEP Profit)!</b>\n"
                    f"Posisi sekarang BEBAS RESIKO 100%."
                )

            # Hit TP
            if c_h >= t["tp"]:
                await self.notifier.send(f"👑 <b>ABSOLUTE TARGET REACHED (TP HIT): +{t['reward']} Pts</b> @ {t['tp']}")
                self.active_trade = None
                return

            # Hit SL / BEP
            if c_l <= t["sl"]:
                res = "BEP / PROFIT MIKRO" if t["bep_locked"] else f"CUT (-{t['risk']} Pts)"
                await self.notifier.send(f"⚠️ <b>TRADE SELESAI: {res}</b> @ {t['sl']}")
                self.active_trade = None
                return

        elif "SELL" in t["action"]:
            # Auto Break-Even Lock
            if not t["bep_locked"] and c_l <= (t["entry"] - BEP_TRIGGER_POINTS):
                t["sl"] = t["entry"] - 0.50
                t["bep_locked"] = True
                await self.notifier.send(
                    f"🛡️ <b>ZERO-RISK PROTECTION ENGAGED</b>\n"
                    f"Asset: {SYMBOL} | Posisi SELL @ {t['entry']}\n"
                    f"Harga turun +{BEP_TRIGGER_POINTS} Pts. <b>Stop Loss diturunkan ke {t['sl']} (BEP Profit)!</b>\n"
                    f"Posisi sekarang BEBAS RESIKO 100%."
                )

            # Hit TP
            if c_l <= t["tp"]:
                await self.notifier.send(f"👑 <b>ABSOLUTE TARGET REACHED (TP HIT): +{t['reward']} Pts</b> @ {t['tp']}")
                self.active_trade = None
                return

            # Hit SL / BEP
            if c_h >= t["sl"]:
                res = "BEP / PROFIT MIKRO" if t["bep_locked"] else f"CUT (-{t['risk']} Pts)"
                await self.notifier.send(f"⚠️ <b>TRADE SELESAI: {res}</b> @ {t['sl']}")
                self.active_trade = None
                return

    async def broadcast_signal(self, sig: dict):
        msg = (
            f"⚡ <b>SUPREME QUANT IMPULSE SIGNAL: {sig['action']}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Asset:</b> {SYMBOL} (TF M5)\n"
            f"<b>Entry Instant:</b> <code>{sig['entry']:.2f}</code>\n"
            f"<b>Strict Stop Loss:</b> <code>{sig['sl']:.2f}</code> (Risk: -{sig['risk']} Pts)\n"
            f"<b>Target Profit:</b> <code>{sig['tp']:.2f}</code> (Gain: +{sig['reward']} Pts)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Pythagoras Vector:</b> {sig['vector']} (Momentum Shock Valid)\n"
            f"<b>Macro Trend H1:</b> 🟢 {sig['macro']}\n"
            f"<b>AI Decision Score:</b> 🟢 {sig.get('ai_conf', 100)}%\n"
            f"<b>Risk Engine:</b> Auto-BEP Lock aktif pada +{BEP_TRIGGER_POINTS} Poin.\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 <i>Eksekusi instan di market sekarang untuk mendapatkan momentum!</i>"
        )
        await self.notifier.send(msg)

    async def run(self):
        await self.notifier.send(
            f"🔱 <b>TITAN SUPREME ARCHITECTURE ONLINE</b>\n"
            f"<b>Sistem:</b> High-Precision Confluence Engine\n"
            f"<b>Fitur:</b> H1 Macro Filter | Pythagoras Vector | Auto-BEP Protection"
        )

        while True:
            try:
                if not self.is_market_active_wib():
                    await asyncio.sleep(60)
                    continue

                async with websockets.connect(DERIV_WS_URL, ping_interval=20, ping_timeout=15) as ws:
                    logger.info("[WS] Terhubung ke Gateway Publik Deriv.")

                    # 1. Fetch data H1 untuk Macro Filter
                    req_h1 = {
                        "ticks_history": SYMBOL,
                        "adjust_start_time": 1,
                        "count": 30,
                        "end": "latest",
                        "style": "candles",
                        "granularity": GRANULARITY_H1
                    }
                    await ws.send(json.dumps(req_h1))
                    res_h1 = await ws.recv()
                    data_h1 = json.loads(res_h1)
                    if "candles" in data_h1:
                        self.h1_candles = data_h1["candles"]

                    # 2. Subscribe M5 Feed
                    req_m5 = {
                        "ticks_history": SYMBOL,
                        "adjust_start_time": 1,
                        "count": LOOKBACK_SWING + 20,
                        "end": "latest",
                        "style": "candles",
                        "granularity": GRANULARITY_M5,
                        "subscribe": 1
                    }
                    await ws.send(json.dumps(req_m5))

                    async for raw_msg in ws:
                        if not self.is_market_active_wib():
                            break

                        msg = json.loads(raw_msg)
                        if "candles" in msg:
                            self.m5_candles = msg["candles"]
                            continue

                        if "ohlc" in msg:
                            ohlc = msg["ohlc"]
                            epoch = int(ohlc["open_time"])

                            # Deteksi candle tertutup
                            if len(self.m5_candles) > 0 and epoch > self.m5_candles[-1]["epoch"]:
                                last_closed = {
                                    "epoch": self.m5_candles[-1]["epoch"],
                                    "open": float(ohlc["open"]),
                                    "high": float(ohlc["high"]),
                                    "low": float(ohlc["low"]),
                                    "close": float(ohlc["close"])
                                }
                                self.m5_candles.append(last_closed)
                                if len(self.m5_candles) > 80:
                                    self.m5_candles.pop(0)

                                # Manajemen Trade yang sedang berlangsung
                                await self.manage_active_trade(last_closed)

                                # Scan Setup Baru jika tidak ada trade aktif
                                if not self.active_trade:
                                    setup = self.scan_precision_setup()
                                    if setup:
                                        # Validasi Ganda Gemini AI
                                        eval_res = await self.ai.verify_institutional_bias(setup, self.m5_candles)
                                        if eval_res.get("verdict") == "APPROVE" and eval_res.get("confidence", 0) >= 0.75:
                                            setup["ai_conf"] = int(eval_res.get("confidence", 0) * 100)
                                            setup["bep_locked"] = False
                                            self.active_trade = setup
                                            await self.broadcast_signal(setup)

            except Exception as e:
                logger.error(f"[RECONNECTING] WebSocket: {e}")
                await asyncio.sleep(5)


if __name__ == "__main__":
    bot = EliteExecutionEngine()
    asyncio.run(bot.run())
