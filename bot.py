#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LEAD QUANTITATIVE & ALGORITHMIC TRADING SYSTEMS ARCHITECT
Production-Ready XAUUSD Deriv Bot for GitHub Actions with Telegram Anti-Spam Alerts.
"""

import os
import sys
import time
import json
import math
import logging
import websocket
import requests
from datetime import datetime, timezone, timedelta

# ==========================================
# KONFIGURASI LOGGING & ENVIRONMENT
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("QuantBot")

# Ambil kredensial dari Environment Variables (GitHub Secrets)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")

# Validasi Variabel Penting
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    logger.critical("FATAL: Telegram Token atau Chat ID belum disetel di environment variables!")
    sys.exit(1)

# ==========================================
# SISTEM TELEGRAM NOTIFIKASI ANTI-SPAM
# ==========================================
class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        self.last_signal_time = 0
        self.cooldown_period = 300  # Cooldown 5 menit antar sinyal untuk anti-spam

    def send_message(self, text: str, force: bool = False) -> bool:
        current_time = time.time()
        if not force and (current_time - self.last_signal_time < self.cooldown_period):
            logger.info("Notifikasi ditahan oleh sistem Anti-Spam (Cooldown aktif).")
            return False

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown"
        }
        try:
            response = requests.post(self.base_url, json=payload, timeout=10)
            if response.status_code == 200:
                if not force:
                    self.last_signal_time = current_time
                logger.info("Notifikasi Telegram berhasil dikirim.")
                return True
            else:
                logger.error(f"Gagal mengirim Telegram: {response.text}")
                return False
        except Exception as e:
            logger.error(f"Exception saat mengirim pesan Telegram: {e}")
            return False

notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

# ==========================================
# MODUL MATEMATIKA & KUANTITATIF TINGKAT LANJUT
# ==========================================
class QuantitativeEngine:
    @staticmethod
    def calculate_atr(candles: list, period: int = 14) -> float:
        """Menghitung Average True Range (ATR) untuk volatilitas aktual."""
        if len(candles) < period + 1:
            return 0.0
        
        true_ranges = []
        for i in range(1, len(candles)):
            high = float(candles[i]['high'])
            low = float(candles[i]['low'])
            close_prev = float(candles[i-1]['close'])
            
            tr = max(high - low, abs(high - close_prev), abs(low - close_prev))
            true_ranges.append(tr)
            
        # Simple Moving Average dari True Range
        atr_value = sum(true_ranges[-period:]) / period
        return atr_value

    @staticmethod
    def kalman_filter_update(price: float, state_estimate: float, error_covariance: float) -> tuple:
        """Penyaringan noise harga menggunakan Kalman Filter sederhana (Mean Reversion)."""
        # Parameter proses
        q = 1e-5  # Process variance
        r = 1e-2  # Measurement variance
        
        # Prediksi
        state_predict = state_estimate
        cov_predict = error_covariance + q
        
        # Update / Koreksi
        kalman_gain = cov_predict / (cov_predict + r)
        state_estimate = state_predict + kalman_gain * (price - state_predict)
        error_covariance = (1 - kalman_gain) * cov_predict
        
        return state_estimate, error_covariance

    @staticmethod
    def fibonacci_golden_ratio_check(price_swing_low: float, price_swing_high: float) -> dict:
        """Menghitung level retracement rasio emas Fibonacci (φ ≈ 1.618)."""
        diff = price_swing_high - price_swing_low
        phi = (1 + math.sqrt(5)) / 2
        return {
            "level_382": price_swing_high - (diff * 0.382),
            "level_500": price_swing_high - (diff * 0.500),
            "level_618": price_swing_high - (diff * (1 / phi)), # 0.618 rasio emas
        }

# ==========================================
# JADWAL OPERASIONAL WAKTU (SENIN 05:00 - SABTU 05:00 WIB)
# ==========================================
def is_market_active() -> bool:
    """Memeriksa apakah waktu saat ini berada di dalam jendela operasional bot."""
    now_utc = datetime.now(timezone.utc)
    # Konversi ke WIB (UTC+7)
    now_wib = now_utc.astimezone(timezone(timedelta(hours=7)))
    
    weekday = now_wib.weekday() # Senin=0, Selasa=1, ..., Sabtu=5, Minggu=6
    hour = now_wib.hour
    minute = now_wib.minute
    
    # Berhenti: Sabtu Jam 05.01 s.d Senin Jam 04.59 WIB
    if weekday == 5: # Sabtu
        if hour >= 5 and minute >= 1:
            return False
        elif hour > 5:
            return False
    if weekday == 6: # Minggu (Libur penuh)
        return False
    if weekday == 0: # Senin
        if hour < 5:
            return False
            
    return True

# ==========================================
# DERIV WEBSOCKET CLIENT & MAIN EXECUTION
# ==========================================
class DerivTradingBot:
    def __init__(self):
        self.ws_url = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
        self.symbol = "frxXAUUSD"
        self.granularity = 300  # M5 (300 detik)
        self.candles_cache = []
        self.kalman_state = 0.0
        self.kalman_cov = 1.0
        self.is_initialized = False

    def on_message(self, ws, message):
        try:
            data = json.loads(message)
            msg_type = data.get("msg_type")
            
            if msg_type == "ohlc":
                candle = data.get("ohlc", {})
                close_price = float(candle.get("close", 0))
                high_price = float(candle.get("high", 0))
                low_price = float(candle.get("low", 0))
                
                # Simpan ke cache lokal
                self.candles_cache.append({
                    "time": candle.get("open_time"),
                    "open": float(candle.get("open", 0)),
                    "high": high_price,
                    "low": low_price,
                    "close": close_price
                })
                
                if len(self.candles_cache) > 50:
                    self.candles_cache.pop(0) # Batasi memori cache
                    
                # Jalankan Logika Analisis Kuantitatif
                self.run_quantitative_analysis(close_price, high_price, low_price)
                
            elif msg_type == "candles":
                self.candles_cache = data.get("candles", [])
                logger.info(f"Berhasil memuat {len(self.candles_cache)} data historis candle.")
                if not self.is_initialized:
                    self.is_initialized = True
                    notifier.send_message(
                        "🟢 *STARTUP NOTIFICATION*\n"
                        "Sistem Bot Trading XAUUSD Deriv M5 berhasil diinisialisasi dan terhubung ke server WebSocket.",
                        force=True
                    )
        except Exception as e:
            logger.error(f"Error processing message: {e}")

    def on_error(self, ws, error):
        logger.error(f"WebSocket Error: {error}")
        notifier.send_message(
            "⚠️ *ERROR / SYSTEM FAILURE ALERT*\n"
            f"Koneksi WebSocket mengalami gangguan error: `{error}`",
            force=True
        )

    def on_close(self, ws, close_status_code, close_msg):
        logger.warning("WebSocket terputus. Mencoba melakukan rekoneksi dalam 5 detik...")
        notifier.send_message(
            "⚠️ *ERROR / SYSTEM FAILURE ALERT*\n"
            "Koneksi WebSocket terputus dari server Deriv. Melakukan auto-reconnect...",
            force=True
        )
        time.sleep(5)
        self.start()

    def on_open(self, ws):
        logger.info("Berhasil terhubung ke WebSocket Deriv. Mengirim subskripsi data OHLC...")
        sub_payload = {
            "ticks_history": self.symbol,
            "adjust_start_time": 1,
            "count": 50,
            "end": "latest",
            "granularity": self.granularity,
            "style": "candles"
        }
        ws.send(json.dumps(sub_payload))
        
        # Subscribe streaming real-time candle
        sub_stream = {
            "ohlc": self.symbol,
            "granularity": self.granularity
        }
        ws.send(json.dumps(sub_stream))

    def run_quantitative_analysis(self, current_close: float, high: float, low: float):
        """Inti model kuantitatif: Kalman Filter, ATR Volatility, dan Filter Sinyal Presisi."""
        if not is_market_active():
            logger.info("Pasar di luar jam operasional aktif. Bot dalam status standby.")
            return

        if len(self.candles_cache) < 15:
            return

        # 1. Update Kalman Filter
        if self.kalman_state == 0.0:
            self.kalman_state = current_close
        self.kalman_state, self.kalman_cov = QuantitativeEngine.kalman_filter_update(
            current_close, self.kalman_state, self.kalman_cov
        )

        # 2. Hitung ATR untuk TP Dinamis dan SL Ultra-Tipis
        atr = QuantitativeEngine.calculate_atr(self.candles_cache, period=14)
        if atr == 0:
            return

        # Syarat Mutlak TP minimal 15 Poin (1.5 USD pergerakan XAUUSD bersih)
        calculated_tp_points = max(15.0, atr * 1.5)
        ultra_tight_sl_points = max(5.0, atr * 0.5)

        # 3. Logika Deteksi Sinyal Berbasis Deviasi Mean Reversion & Order Flow Slices
        deviation = current_close - self.kalman_state
        
        # Sinyal BUY Valid (Deviasi negatif ekstrem dari Kalman Mean)
        if deviation < -(atr * 0.8):
            entry_price = current_close
            tp_price = entry_price + calculated_tp_points
            sl_price = entry_price - ultra_tight_sl_points
            
            signal_msg = (
                f"🚀 *VALID SIGNAL ALERT (BUY)*\n"
                f"• *Instrumen:* XAUUSD (M5)\n"
                f"• *Entry Price:* `{entry_price:.2f}`\n"
                f"• *Take Profit (TP):* `{tp_price:.2f}` (+{calculated_tp_points:.1f} Poin)\n"
                f"• *Stop Loss (SL):* `{sl_price:.2f}` (-{ultra_tight_sl_points:.1f} Poin)\n"
                f"• *Model Filter:* Kalman Mean Reversion & ATR Volatility Gatekeeper"
            )
            notifier.send_message(signal_msg)

        # Sinyal SELL Valid (Deviasi positif ekstrem dari Kalman Mean)
        elif deviation > (atr * 0.8):
            entry_price = current_close
            tp_price = entry_price - calculated_tp_points
            sl_price = entry_price + ultra_tight_sl_points
            
            signal_msg = (
                f"🚀 *VALID SIGNAL ALERT (SELL)*\n"
                f"• *Instrumen:* XAUUSD (M5)\n"
                f"• *Entry Price:* `{entry_price:.2f}`\n"
                f"• *Take Profit (TP):* `{tp_price:.2f}` (-{calculated_tp_points:.1f} Poin)\n"
                f"• *Stop Loss (SL):* `{sl_price:.2f}` (+{ultra_tight_sl_points:.1f} Poin)\n"
                f"• *Model Filter:* Kalman Mean Reversion & ATR Volatility Gatekeeper"
            )
            notifier.send_message(signal_msg)

    def start(self):
        while True:
            try:
                if not is_market_active():
                    logger.info("Pasar libur (Sabtu-Senin dini hari). Bot masuk mode tidur selama 1 jam...")
                    time.sleep(3600)
                    continue

                logger.info("Menghubungkan ke server WebSocket Deriv...")
                ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_open=self.on_open,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close
                )
                ws.run_forever()
            except Exception as e:
                logger.error(f"Critical error in main loop: {e}")
                time.sleep(10)

if __name__ == "__main__":
    bot = DerivTradingBot()
    bot.start()
