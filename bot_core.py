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
GRANULARITY_M5 = 300            # 300 Detik (M5)
GRANULARITY_H1 = 3600           # 3600 Detik (H1 - Macro Filter)
MIN_TP_POINTS = 15.0            # Target Mutlak TP >= 15 Poin ($15.00)
BEP_TRIGGER_POINTS = 3.5        # Kunci BEP begitu harga naik +3.5 Poin (Zero-Risk)
LOOKBACK_SWING = 28             # Siklus Fibonacci Swing Bar (21-34 Harmonik)
PHI = 1.6180339887              # Rasio Emas (Golden Ratio) - dipakai sbg label, bukan threshold vector

# Threshold minimum "energi" impuls, dalam satuan ATR (bukan gabungan bar-count + ATR
# seperti versi lama yang membuat filter itu secara matematis tidak pernah aktif).
MIN_IMPULSE_VECTOR = float(os.getenv("MIN_IMPULSE_VECTOR", "0.6"))

# Refresh data H1 (macro filter) setiap N detik, supaya tidak basi selama koneksi
# WS bertahan lama tanpa reconnect.
H1_REFRESH_SECONDS = 1800  # 30 menit

# req_id untuk mencocokkan response WS dengan request yang dikirim
REQ_ID_ACTIVE_SYMBOLS = 100
REQ_ID_H1 = 101
REQ_ID_M5_SEED = 102

STATE_FILE = os.getenv("STATE_FILE", "active_trade_state.json")


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
    """Mengukur akselerasi harga 2-langkah (dalam satuan ATR) sebagai vektor 2D."""
    @staticmethod
    def calculate_impulse_vector(candles: list, atr: float) -> float:
        if len(candles) < 3 or atr <= 0:
            return 0.0
        # dp1: perubahan harga bar (t-2 -> t-1), dp2: perubahan harga bar (t-1 -> t),
        # keduanya dinormalisasi dengan ATR sehingga skalanya sebanding antar simbol.
        dp1 = (candles[-2]["close"] - candles[-3]["close"]) / atr
        dp2 = (candles[-1]["close"] - candles[-2]["close"]) / atr
        return math.sqrt(dp1 ** 2 + dp2 ** 2)


class TelegramEngine:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{self.token}/sendMessage"

    async def send(self, message: str):
        if not self.token or not self.chat_id:
            logger.warning("[TELEGRAM] Token/Chat ID kosong, pesan tidak terkirim: %s", message[:80])
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
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(f"[TELEGRAM ERROR] status={resp.status} body={body[:200]}")
        except Exception as e:
            logger.error(f"[TELEGRAM ERROR] {e}")


class GeminiDeepReasoningGate:
    """Gatekeeper AI: Validasi Tingkat Ketat Berbasis Mikrostruktur."""
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={self.api_key}"
        if not self.api_key:
            logger.warning(
                "[GEMINI] GEMINI_API_KEY tidak diset. Semua setup akan otomatis REJECT "
                "sampai key ini diisi — bot tidak akan pernah mengirim sinyal."
            )

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
                        raw_text = res["candidates"][0]["content"]["parts"][0]["text"].strip()
                        parsed = json.loads(raw_text)
                        if "verdict" not in parsed:
                            raise ValueError(f"Response Gemini tidak berisi 'verdict': {raw_text[:200]}")
                        return parsed
                    else:
                        body = await r.text()
                        logger.error(f"[GEMINI ERROR] status={r.status} body={body[:200]}")
        except Exception as e:
            logger.error(f"[GEMINI ERROR] Gagal memvalidasi setup: {e}")
        return {"verdict": "REJECT", "confidence": 0.0, "reason": "Gatekeeper Timeout / Fail-Safe"}


class QuantSignalEngine:
    """
    Bot ini adalah SIGNAL / NOTIFIER engine: ia memindai pasar dan mengirim
    sinyal ke Telegram beserta manajemen BEP/TP secara simulatif. Bot ini
    TIDAK mengeksekusi order apa pun ke broker/exchange manapun.
    """

    def __init__(self):
        self.notifier = TelegramEngine(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self.ai = GeminiDeepReasoningGate(GEMINI_API_KEY)
        self.kalman_m5 = KalmanVelocityFilter()
        self.kalman_h1 = KalmanVelocityFilter()
        self.tz_wib = pytz.timezone("Asia/Jakarta")

        self.m5_candles = []
        self.h1_candles = []
        self.forming_candle = None      # candle M5 yang sedang berjalan (belum closed)
        self.active_trade = None
        self.last_h1_refresh = 0.0

        self._load_state()

    # ---------------- STATE PERSISTENCE ----------------
    def _load_state(self):
        """Muat active_trade dari disk agar bot tidak 'lupa' posisi yang sedang
        dipantau jika proses ter-restart/crash."""
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r") as f:
                    data = json.load(f)
                self.active_trade = data.get("active_trade")
                if self.active_trade:
                    logger.info(f"[STATE] Memuat active_trade dari disk: {self.active_trade}")
        except Exception as e:
            logger.error(f"[STATE] Gagal memuat state: {e}")

    def _save_state(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({"active_trade": self.active_trade}, f)
        except Exception as e:
            logger.error(f"[STATE] Gagal menyimpan state: {e}")

    # ---------------- MARKET SESSION ----------------
    def is_market_active_wib(self) -> bool:
        now = datetime.now(self.tz_wib)
        wd, h, m = now.weekday(), now.hour, now.minute
        if wd == 0:
            return h >= 5
        elif 1 <= wd <= 4:
            return True
        elif wd == 5:
            return h < 5 or (h == 5 and m == 0)
        return False

    # ---------------- MACRO FILTER ----------------
    def check_macro_h1_alignment(self) -> str:
        """Filter Makro: arah M5 harus sejalan dengan aliran modal H1.

        Dibandingkan terhadap estimasi PRIOR (sebelum update dengan close
        terbaru) agar tidak sirkular — versi lama membandingkan close
        dengan estimasi yang baru saja dihitung dari close yang sama.
        """
        if len(self.h1_candles) < 5:
            return "NEUTRAL"
        closes = [c["close"] for c in self.h1_candles]
        prior_estimate = self.kalman_h1.x if self.kalman_h1.is_init else closes[-1]
        _, h1_vel = self.kalman_h1.update(closes[-1])
        if closes[-1] > prior_estimate and h1_vel > 0:
            return "BULLISH"
        elif closes[-1] < prior_estimate and h1_vel < 0:
            return "BEARISH"
        return "NEUTRAL"

    # ---------------- SETUP SCANNER ----------------
    def scan_precision_setup(self):
        if len(self.m5_candles) < LOOKBACK_SWING + 10:
            return None

        closes = np.array([c["close"] for c in self.m5_candles])
        highs = np.array([c["high"] for c in self.m5_candles])
        lows = np.array([c["low"] for c in self.m5_candles])
        opens = np.array([c["open"] for c in self.m5_candles])

        _, kalman_vel = self.kalman_m5.update(closes[-1])

        # Hitung True ATR M5
        tr = np.maximum(highs[1:] - lows[1:], np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])))
        if len(tr) >= 14:
            atr = float(np.mean(tr[-14:]))
        else:
            # Fallback adaptif: rata-rata range high-low yang tersedia,
            # lebih representatif untuk simbol ini daripada angka magic tetap.
            atr = float(np.mean(highs - lows)) if len(highs) > 0 else 1.5
            atr = max(atr, 0.1)

        # 1. Impulse Vector (akselerasi 2-langkah dalam satuan ATR)
        pythagoras_vector = PythagoreanMomentumMatrix.calculate_impulse_vector(self.m5_candles, atr)
        if pythagoras_vector < MIN_IMPULSE_VECTOR:  # Tolak pergerakan lambat tanpa energi
            return None

        # 2. Structural High / Low (Fibonacci Harmonik 28 Lookback)
        swing_h = np.max(highs[-LOOKBACK_SWING - 1:-1])
        swing_l = np.min(lows[-LOOKBACK_SWING - 1:-1])

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
            if risk <= 0:
                return None
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
                "macro": macro_bias,
            }

        elif bearish_sweep:
            entry = round(c_close, 2)
            sl = round(c_high + max(0.4 * atr, 0.7), 2)
            risk = sl - entry
            if risk <= 0:
                return None
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
                "macro": macro_bias,
            }

        return None

    # ---------------- TRADE MANAGEMENT ----------------
    async def manage_active_trade(self, current_bar: dict):
        """Zero-Loss Manager: Mengunci BEP & Memotong Trade Jika Momentum Mati."""
        if not self.active_trade:
            return

        t = self.active_trade
        c_h, c_l, c_c = current_bar["high"], current_bar["low"], current_bar["close"]
        self.kalman_m5.update(c_c)

        if "BUY" in t["action"]:
            if not t["bep_locked"] and c_h >= (t["entry"] + BEP_TRIGGER_POINTS):
                t["sl"] = t["entry"] + 0.50
                t["bep_locked"] = True
                self._save_state()
                await self.notifier.send(
                    f"🛡️ <b>ZERO-RISK PROTECTION ENGAGED</b>\n"
                    f"Asset: {SYMBOL} | Posisi BUY @ {t['entry']}\n"
                    f"Harga melonjak +{BEP_TRIGGER_POINTS} Pts. <b>Stop Loss dinaikkan ke {t['sl']} (BEP Profit)!</b>\n"
                    f"Posisi sekarang BEBAS RESIKO 100%."
                )

            if c_h >= t["tp"]:
                await self.notifier.send(f"👑 <b>ABSOLUTE TARGET REACHED (TP HIT): +{t['reward']} Pts</b> @ {t['tp']}")
                self.active_trade = None
                self._save_state()
                return

            if c_l <= t["sl"]:
                res = "BEP / PROFIT MIKRO" if t["bep_locked"] else f"CUT (-{t['risk']} Pts)"
                await self.notifier.send(f"⚠️ <b>TRADE SELESAI: {res}</b> @ {t['sl']}")
                self.active_trade = None
                self._save_state()
                return

        elif "SELL" in t["action"]:
            if not t["bep_locked"] and c_l <= (t["entry"] - BEP_TRIGGER_POINTS):
                t["sl"] = t["entry"] - 0.50
                t["bep_locked"] = True
                self._save_state()
                await self.notifier.send(
                    f"🛡️ <b>ZERO-RISK PROTECTION ENGAGED</b>\n"
                    f"Asset: {SYMBOL} | Posisi SELL @ {t['entry']}\n"
                    f"Harga turun +{BEP_TRIGGER_POINTS} Pts. <b>Stop Loss diturunkan ke {t['sl']} (BEP Profit)!</b>\n"
                    f"Posisi sekarang BEBAS RESIKO 100%."
                )

            if c_l <= t["tp"]:
                await self.notifier.send(f"👑 <b>ABSOLUTE TARGET REACHED (TP HIT): +{t['reward']} Pts</b> @ {t['tp']}")
                self.active_trade = None
                self._save_state()
                return

            if c_h >= t["sl"]:
                res = "BEP / PROFIT MIKRO" if t["bep_locked"] else f"CUT (-{t['risk']} Pts)"
                await self.notifier.send(f"⚠️ <b>TRADE SELESAI: {res}</b> @ {t['sl']}")
                self.active_trade = None
                self._save_state()
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
            f"<b>Impulse Vector:</b> {sig['vector']} (Momentum Shock Valid, min {MIN_IMPULSE_VECTOR})\n"
            f"<b>Macro Trend H1:</b> 🟢 {sig['macro']}\n"
            f"<b>AI Decision Score:</b> 🟢 {sig.get('ai_conf', 100)}%\n"
            f"<b>Risk Engine:</b> Auto-BEP Lock aktif pada +{BEP_TRIGGER_POINTS} Poin.\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"ℹ️ <i>Ini adalah sinyal informasional, bukan eksekusi otomatis ke broker.</i>"
        )
        await self.notifier.send(msg)

    # ---------------- WS REQUEST HELPERS ----------------
    async def _check_symbol_availability(self, ws):
        """Preflight check: minta daftar active_symbols dan cari mana yang
        cocok dengan SYMBOL / mengandung 'XAU'. app_id publik tertentu bisa
        saja tidak punya akses ke instrumen komoditas tergantung landing
        company/region yang terikat padanya, sehingga simbol yang secara
        sintaks benar (mis. frxXAUUSD) tetap bisa ditolak dengan
        InvalidSymbol. Hasil pengecekan ini hanya di-log, tidak mengubah
        alur program."""
        req = {"active_symbols": "brief", "product_type": "basic", "req_id": REQ_ID_ACTIVE_SYMBOLS}
        await ws.send(json.dumps(req))

    def _handle_active_symbols_response(self, msg):
        symbols = msg.get("active_symbols", [])
        names = {s.get("symbol") for s in symbols}
        if SYMBOL in names:
            logger.info(f"[SYMBOL CHECK] '{SYMBOL}' TERSEDIA untuk app_id ini.")
        else:
            gold_like = sorted(
                s.get("symbol") for s in symbols
                if "XAU" in (s.get("symbol") or "") or "gold" in (s.get("display_name") or "").lower()
            )
            logger.error(
                f"[SYMBOL CHECK] '{SYMBOL}' TIDAK ADA di daftar active_symbols untuk app_id/region ini. "
                f"Kandidat simbol emas yang tersedia: {gold_like if gold_like else '(tidak ditemukan satupun)'}. "
                f"Set env TARGET_SYMBOL ke salah satu kandidat di atas, atau gunakan app_id/akun dengan akses komoditas."
            )

    async def _fetch_h1_candles(self, ws):
        req = {
            "ticks_history": SYMBOL,
            "adjust_start_time": 1,
            "count": 30,
            "end": "latest",
            "style": "candles",
            "granularity": GRANULARITY_H1,
            "req_id": REQ_ID_H1,
        }
        await ws.send(json.dumps(req))

    async def _fetch_m5_seed_and_subscribe(self, ws):
        req = {
            "ticks_history": SYMBOL,
            "adjust_start_time": 1,
            "count": LOOKBACK_SWING + 20,
            "end": "latest",
            "style": "candles",
            "granularity": GRANULARITY_M5,
            "subscribe": 1,
            "req_id": REQ_ID_M5_SEED,
        }
        await ws.send(json.dumps(req))

    # ---------------- MAIN LOOP ----------------
    async def run(self):
        await self.notifier.send(
            f"🔱 <b>TITAN SUPREME ARCHITECTURE ONLINE</b>\n"
            f"<b>Sistem:</b> High-Precision Confluence Engine (Signal/Notifier only)\n"
            f"<b>Fitur:</b> H1 Macro Filter (auto-refresh) | Impulse Vector | Auto-BEP Protection"
        )

        while True:
            try:
                if not self.is_market_active_wib():
                    await asyncio.sleep(60)
                    continue

                async with websockets.connect(DERIV_WS_URL, ping_interval=20, ping_timeout=15) as ws:
                    logger.info("[WS] Terhubung ke Gateway Publik Deriv.")

                    self.forming_candle = None
                    await self._check_symbol_availability(ws)
                    await self._fetch_h1_candles(ws)
                    await self._fetch_m5_seed_and_subscribe(ws)
                    self.last_h1_refresh = time.time()

                    async for raw_msg in ws:
                        if not self.is_market_active_wib():
                            break

                        msg = json.loads(raw_msg)
                        req_id = msg.get("req_id")

                        if msg.get("error"):
                            which = {
                                REQ_ID_ACTIVE_SYMBOLS: "active_symbols (preflight)",
                                REQ_ID_H1: "H1 candles fetch",
                                REQ_ID_M5_SEED: "M5 seed + subscribe",
                            }.get(req_id, f"req_id={req_id} (tidak dikenal)")
                            logger.error(f"[DERIV API ERROR] request='{which}' -> {msg['error']}")
                            continue

                        msg_type = msg.get("msg_type")

                        if msg_type == "active_symbols":
                            self._handle_active_symbols_response(msg)
                            continue

                        if msg_type == "candles":
                            if req_id == REQ_ID_H1:
                                self.h1_candles = msg.get("candles", [])
                                logger.info(f"[H1] Data makro diperbarui ({len(self.h1_candles)} candle).")
                            elif req_id == REQ_ID_M5_SEED:
                                self.m5_candles = msg.get("candles", [])
                                self.forming_candle = None
                                logger.info(f"[M5] Seed data dimuat ({len(self.m5_candles)} candle).")
                            continue

                        if msg_type == "ohlc" or "ohlc" in msg:
                            ohlc = msg["ohlc"]
                            epoch = int(ohlc["open_time"])
                            o, h, l, c = (
                                float(ohlc["open"]), float(ohlc["high"]),
                                float(ohlc["low"]), float(ohlc["close"]),
                            )

                            if self.forming_candle is None:
                                self.forming_candle = {"epoch": epoch, "open": o, "high": h, "low": l, "close": c}

                            elif epoch == self.forming_candle["epoch"]:
                                # Masih candle yang sama sedang terbentuk -> update in-place
                                self.forming_candle["high"] = h
                                self.forming_candle["low"] = l
                                self.forming_candle["close"] = c

                            else:
                                # Epoch berubah -> candle forming sebelumnya BENAR-BENAR closed.
                                closed_candle = self.forming_candle
                                self.m5_candles.append(closed_candle)
                                if len(self.m5_candles) > 80:
                                    self.m5_candles.pop(0)

                                # Mulai candle baru yang sedang terbentuk
                                self.forming_candle = {"epoch": epoch, "open": o, "high": h, "low": l, "close": c}

                                await self.manage_active_trade(closed_candle)

                                if not self.active_trade:
                                    setup = self.scan_precision_setup()
                                    if setup:
                                        eval_res = await self.ai.verify_institutional_bias(setup, self.m5_candles)
                                        if eval_res.get("verdict") == "APPROVE" and eval_res.get("confidence", 0) >= 0.75:
                                            setup["ai_conf"] = int(eval_res.get("confidence", 0) * 100)
                                            setup["bep_locked"] = False
                                            self.active_trade = setup
                                            self._save_state()
                                            await self.broadcast_signal(setup)

                                # Refresh H1 macro filter secara berkala agar tidak basi
                                if time.time() - self.last_h1_refresh >= H1_REFRESH_SECONDS:
                                    await self._fetch_h1_candles(ws)
                                    self.last_h1_refresh = time.time()

            except Exception as e:
                logger.error(f"[RECONNECTING] WebSocket: {e}")
                await asyncio.sleep(5)


if __name__ == "__main__":
    bot = QuantSignalEngine()
    asyncio.run(bot.run())
