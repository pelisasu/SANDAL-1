#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LEAD QUANTITATIVE & ALGORITHMIC TRADING SYSTEMS ARCHITECT
Production-Ready XAUUSD Deriv Bot - Multi-Timeframe (MTF) Advanced Quant Engine (24/5 Ready)
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
    format="%(asctime)s [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("QuantBotAdvancedMTF")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")

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
        self.cooldown_period = 300  # Cooldown 5 menit anti-spam

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
# MODUL KUANTITATIF & MTF ENGINE
# ==========================================
class AdvancedQuantitativeEngine:
    @staticmethod
    def calculate_atr(candles: list, period: int = 14) -> float:
        if len(candles) < period + 1:
            return 0.0
        true_ranges = []
        for i in range(1, len(candles)):
            high = float(candles[i]['high'])
            low = float(candles[i]['low'])
            close_prev = float(candles[i-1]['close'])
            tr = max(high - low, abs(high - close_prev), abs(low - close_prev))
            true_ranges.append(tr)
        return sum(true_ranges[-period:]) / period

    @staticmethod
    def calculate_sma(candles: list, period: int = 50) -> float:
        if len(candles) < period:
            return float(candles[-1]['close']) if candles else 0.0
        closes = [float(c['close']) for c in candles[-period:]]
        return sum(closes) / period

    @staticmethod
    def kalman_filter_update(price: float, state_estimate: float, error_covariance: float) -> tuple:
        q = 1e-5  
        r = 1e-2  
        state_predict = state_estimate
        cov_predict = error_covariance + q
        
        kalman_gain = cov_predict / (cov_predict + r)
        state_estimate = state_predict + kalman_gain * (price - state_predict)
        error_covariance = (1 - kalman_gain) * cov_predict
        
        return state_estimate, error_covariance

    @staticmethod
    def check_higher_timeframe_trend(candles: list) -> str:
        """
        Simulasi Multi-Timeframe (MTF) Trend Filter:
        Menganalisis blok candle agregat besar untuk menentukan arah tren makro (BULLISH / BEARISH).
        """
        if len(candles) < 60:
            return "NEUTRAL"
        
        # Agregasi data M5 menjadi struktur tren yang lebih besar
        macro_sma_fast = sum([float(c['close']) for c in candles[-20:]]) / 20
        macro_sma_slow = sum([float(c['close']) for c in candles[-60:]]) / 60
        
        if macro_sma_fast > macro_sma_slow:
            return "BULLISH"
        elif macro_sma_fast < macro_sma_slow:
            return "BEARISH"
        return "NEUTRAL"

    @staticmethod
    def evaluate_confidence(candles: list, deviation: float, atr: float) -> float:
        if len(candles) < 10:
            return 0.0
        recent = candles[-5:]
        body_momentum = sum([abs(float(c['close']) - float(c['open'])) for c in recent]) / 5
        
        volatility_score = min(1.0, atr / 5.0)
        deviation_score = min(1.0, abs(deviation) / (atr * 1.5))
        momentum_score = min(1.0, body_momentum / atr) if atr > 0 else 0.5
        
        confidence = (deviation_score * 0.4) + (momentum_score * 0.4) + (volatility_score * 0.2)
        return round(confidence * 100, 2)

# ==========================================
# FILTER SESI LIKUIDITAS TINGGI (WIB)
# ==========================================
def is_market_active_and_liquid() -> bool:
    now_utc = datetime.now(timezone.utc)
    now_wib = now_utc.astimezone(timezone(timedelta(hours=7)))
    weekday = now_wib.weekday()
    hour = now_wib.hour
    
    if weekday == 5 and hour >= 5:
        return False
    if weekday == 6:
        return False
    if weekday == 0 and hour < 5:
        return False
        
    # Sesi Likuiditas Optimal XAUUSD (13:00 - 23:00 WIB)
    if 13 <= hour < 23:
        return True
    return False

# ==========================================
# DERIV WEBSOCKET CLIENT & MAIN EXECUTION
# ==========================================
class DerivTradingBotMTF:
    def __init__(self):
        self.ws_url = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
        self.symbol = "frxXAUUSD"
        self.granularity = 300  # M5
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
                
                self.candles_cache.append({
                    "time": candle.get("open_time"),
                    "open": float(candle.get("open", 0)),
                    "high": high_price,
                    "low": low_price,
                    "close": close_price
                })
                
                if len(self.candles_cache) > 120:
                    self.candles_cache.pop(0)
                    
                self.run_quantitative_analysis(close_price)
                
            elif msg_type == "candles":
                self.candles_cache = data.get("candles", [])
                logger.info(f"Berhasil memuat {len(self.candles_cache)} data historis candle.")
                if not self.is_initialized:
                    self.is_initialized = True
                    notifier.send_message(
                        "🟢 *STARTUP NOTIFICATION (MTF QUANT ELITE)*\n"
                        "Sistem Bot XAUUSD M5 Aktif dengan Multi-Timeframe Trend & Kalman Engine.",
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
        logger.warning("WebSocket terputus. Melakukan auto-reconnect dalam 5 detik...")
        notifier.send_message(
            "⚠️ *ERROR / SYSTEM FAILURE ALERT*\n"
            "Koneksi WebSocket terputus dari server Deriv. Melakukan auto-reconnect bersih...",
            force=True
        )
        time.sleep(5)
        self.start()

    def on_open(self, ws):
        logger.info("Websocket connected to Deriv server.")
        sub_payload = {
            "ticks_history": self.symbol,
            "adjust_start_time": 1,
            "count": 120,
            "end": "latest",
            "granularity": self.granularity,
            "style": "candles"
        }
        ws.send(json.dumps(sub_payload))
        
        sub_stream = {
            "ohlc": self.symbol,
            "granularity": self.granularity
        }
        ws.send(json.dumps(sub_stream))

    def run_quantitative_analysis(self, current_close: float):
        if not is_market_active_and_liquid():
            return

        if len(self.candles_cache) < 60:
            return

        # 1. Kalman Filter Update
        if self.kalman_state == 0.0:
            self.kalman_state = current_close
        self.kalman_state, self.kalman_cov = AdvancedQuantitativeEngine.kalman_filter_update(
            current_close, self.kalman_state, self.kalman_cov
        )

        # 2. Indikator & Multi-Timeframe Check
        atr = AdvancedQuantitativeEngine.calculate_atr(self.candles_cache, period=14)
        sma_50 = AdvancedQuantitativeEngine.calculate_sma(self.candles_cache, period=50)
        mtf_trend = AdvancedQuantitativeEngine.check_higher_timeframe_trend(self.candles_cache)
        confidence = AdvancedQuantitativeEngine.evaluate_confidence(self.candles_cache, current_close - self.kalman_state, atr)
        
        if atr == 0:
            return

        deviation = current_close - self.kalman_state
        calculated_tp_points = max(15.0, atr * 1.5)
        ultra_tight_sl_points = max(5.0, atr * 0.5)
        min_ai_threshold = 60.0

        # Sinyal BUY Valid (Didukung MTF Bullish)
        if deviation < -(atr * 0.85) and current_close >= sma_50 and mtf_trend == "BULLISH" and confidence >= min_ai_threshold:
            entry_price = current_close
            tp_price = entry_price + calculated_tp_points
            sl_price = entry_price - ultra_tight_sl_points
            
            signal_msg = (
                f"🚀 *MTF VALID SIGNAL (BUY)*\n"
                f"• *Instrumen:* XAUUSD (M5)\n"
                f"• *MTF Macro Trend:* `BULLISH`\n"
                f"• *Confidence Score:* `{confidence}%`\n"
                f"• *Entry Price:* `{entry_price:.2f}`\n"
                f"• *Take Profit (TP):* `{tp_price:.2f}` (+{calculated_tp_points:.1f} Poin)\n"
                f"• *Stop Loss (SL):* `{sl_price:.2f}` (-{ultra_tight_sl_points:.1f} Poin)"
            )
            notifier.send_message(signal_msg)

        # Sinyal SELL Valid (Didukung MTF Bearish)
        elif deviation > (atr * 0.85) and current_close <= sma_50 and mtf_trend == "BEARISH" and confidence >= min_ai_threshold:
            entry_price = current_close
            tp_price = entry_price - calculated_tp_points
            sl_price = entry_price + ultra_tight_sl_points
            
            signal_msg = (
                f"🚀 *MTF VALID SIGNAL (SELL)*\n"
                f"• *Instrumen:* XAUUSD (M5)\n"
                f"• *MTF Macro Trend:* `BEARISH`\n"
                f"• *Confidence Score:* `{confidence}%`\n"
                f"• *Entry Price:* `{entry_price:.2f}`\n"
                f"• *Take Profit (TP):* `{tp_price:.2f}` (-{calculated_tp_points:.1f} Poin)\n"
                f"• *Stop Loss (SL):* `{sl_price:.2f}` (+{ultra_tight_sl_points:.1f} Poin)"
            )
            notifier.send_message(signal_msg)

    def start(self):
        while True:
            try:
                if not is_market_active_and_liquid():
                    time.sleep(1800)
                    continue

                logger.info("Menghubungkan ke server WebSocket Deriv (MTF Engine)...")
                ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_open=self.on_open,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close
                )
                ws.run_forever(ping_interval=15, ping_timeout=10)
                
            except Exception as e:
                logger.error(f"Critical error in main loop: {e}")
                time.sleep(5)

if __name__ == "__main__":
    bot = DerivTradingBotMTF()
    bot.start()
