"""
market_data.py — MarketDataEngine

Responsibilities:
 1. Stream real-time BTC prices from Binance + Coinbase via WebSocket (<100ms latency).
 2. Discover active BTC up/down markets on Polymarket via CLOB client.
 3. Fetch Polymarket orderbook / midpoint for active markets.
 4. Maintain a rolling window of BTC price ticks for momentum detection.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Optional

import websockets
from loguru import logger

from py_clob_client.client import ClobClient

from .config import BotConfig
from .models import MarketInfo, PriceTick


class MarketDataEngine:
    """Provides real-time BTC prices via WebSocket and Polymarket market data."""

    MAX_TICKS = 200

    # Binance aggTrade: every single trade on the order book, ~10-50ms latency
    _WS_BINANCE = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
    # Coinbase Exchange (stable public endpoint, no auth required)
    _WS_COINBASE = "wss://ws-feed.exchange.coinbase.com"

    def __init__(self, cfg: BotConfig, clob_client: Optional[ClobClient] = None):
        self.cfg = cfg
        self.clob = clob_client
        self.ticks: deque[PriceTick] = deque(maxlen=self.MAX_TICKS)
        # Signal ticks — one sample per second, used by SignalEngine for momentum
        self.signal_ticks: deque[PriceTick] = deque(maxlen=self.MAX_TICKS)
        self._active_markets: dict[str, MarketInfo] = {}
        self._ws_running = False
        self._ws_tasks: list[asyncio.Task] = []
        self._tick_event: asyncio.Event = asyncio.Event()

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self):
        self._ws_running = True
        self._ws_tasks = [
            asyncio.create_task(self._binance_ws_loop(), name="ws-binance"),
            asyncio.create_task(self._coinbase_ws_loop(), name="ws-coinbase"),
            asyncio.create_task(self._sampler_loop(), name="ws-sampler"),
        ]
        logger.info("MarketDataEngine started (WebSocket mode — Binance + Coinbase)")

    async def stop(self):
        self._ws_running = False
        for task in self._ws_tasks:
            task.cancel()
        # return_exceptions=True suppresses CancelledError propagation
        results = await asyncio.gather(*self._ws_tasks, return_exceptions=True)
        self._ws_tasks.clear()
        logger.info("MarketDataEngine stopped")

    # ── WebSocket loops ───────────────────────────────────────────────

    async def _sampler_loop(self):
        """
        Sample BTC price at a fixed interval into signal_ticks.
        Paper/test default: 1s — fires frequently for quick feedback.
        Live production: set TICK_SAMPLE_SECONDS=60 (1-minute bars, like Kiro).
        """
        interval = self.cfg.strategy.tick_sample_seconds
        while self._ws_running:
            await asyncio.sleep(interval)
            price = self.latest_btc_price()
            if price is not None:
                self.signal_ticks.append(PriceTick(
                    source="sampled",
                    symbol="BTC/USDT",
                    price=price,
                    timestamp=time.time(),
                ))

    async def _binance_ws_loop(self):
        """Stream Binance aggTrade ticks. Reconnects with exponential backoff."""
        backoff = 1.0
        while self._ws_running:
            try:
                async with websockets.connect(
                    self._WS_BINANCE,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    logger.info("Binance WebSocket connected")
                    backoff = 1.0
                    async for raw in ws:
                        if not self._ws_running:
                            return
                        msg = json.loads(raw)
                        self.ticks.append(PriceTick(
                            source="binance",
                            symbol="BTC/USDT",
                            price=float(msg["p"]),
                            timestamp=time.time(),
                        ))
                        self._tick_event.set()
            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._ws_running:
                    return
                logger.warning(f"Binance WS ({type(e).__name__}): {e} — retry in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _coinbase_ws_loop(self):
        """Stream Coinbase ticker ticks. Reconnects with exponential backoff."""
        subscribe = json.dumps({
            "type": "subscribe",
            "product_ids": ["BTC-USD"],
            "channels": ["ticker"],
        })
        backoff = 1.0
        while self._ws_running:
            try:
                async with websockets.connect(
                    self._WS_COINBASE,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    await ws.send(subscribe)
                    logger.info("Coinbase WebSocket connected")
                    backoff = 1.0
                    async for raw in ws:
                        if not self._ws_running:
                            return
                        msg = json.loads(raw)
                        # Coinbase Exchange ticker message has 'price' at top level
                        if msg.get("type") == "ticker" and msg.get("price"):
                            self.ticks.append(PriceTick(
                                source="coinbase",
                                symbol="BTC/USD",
                                price=float(msg["price"]),
                                timestamp=time.time(),
                            ))
                            self._tick_event.set()
                        for event in msg.get("events", []):
                            for ticker in event.get("tickers", []):
                                price_str = ticker.get("price")
                                if price_str:
                                    self.ticks.append(PriceTick(
                                        source="coinbase",
                                        symbol="BTC/USD",
                                        price=float(price_str),
                                        timestamp=time.time(),
                                    ))
                                    self._tick_event.set()
            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._ws_running:
                    return
                logger.warning(f"Coinbase WS ({type(e).__name__}): {e} — retry in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    # ── tick access ───────────────────────────────────────────────────

    async def wait_for_tick(self, timeout: float = 2.0) -> bool:
        """
        Event-driven replacement for polling sleep.
        Blocks until a new tick arrives from either WebSocket, or timeout.
        Returns True if a tick arrived, False on timeout.
        """
        self._tick_event.clear()
        try:
            await asyncio.wait_for(self._tick_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def latest_btc_price(self) -> Optional[float]:
        """Return the most recent BTC price across all sources."""
        if not self.ticks:
            return None
        return self.ticks[-1].price

    def recent_ticks(self, n: int = 20) -> list[PriceTick]:
        """
        Return the last n 1-second sampled ticks (used by SignalEngine).
        Each entry represents the BTC price at the end of a 1-second window,
        making momentum detection equivalent to evaluating on 1s bars.
        """
        return list(self.signal_ticks)[-n:]

    # ── Polymarket markets ────────────────────────────────────────────

    def discover_btc_markets(self) -> list[MarketInfo]:
        """
        Scan Polymarket for active BTC up/down markets.
        These are short-duration markets (5min / 15min) asking
        'Will BTC go up or down?'
        """
        if self.clob is None:
            return []

        try:
            markets_resp = self.clob.get_simplified_markets()
            markets_data = markets_resp.get("data", []) if isinstance(markets_resp, dict) else []
        except Exception as e:
            logger.error(f"Failed to fetch Polymarket markets: {e}")
            return []

        found: list[MarketInfo] = []
        now = time.time()

        for m in markets_data:
            question = (m.get("question") or "").lower()
            # Filter for BTC up/down short-term markets
            is_btc = "bitcoin" in question or "btc" in question
            is_updown = "up or down" in question or "higher or lower" in question
            tokens = m.get("tokens", [])

            if not (is_btc and is_updown and len(tokens) >= 2):
                continue

            end_ts = float(m.get("end_date_iso", 0) or 0)
            if end_ts == 0:
                # Try parsing ISO date
                try:
                    from datetime import datetime
                    end_ts = datetime.fromisoformat(
                        m.get("end_date_iso", "").replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    continue

            if end_ts < now:
                continue

            yes_token = ""
            no_token = ""
            for tok in tokens:
                outcome = (tok.get("outcome") or "").lower()
                if outcome in ("yes", "up"):
                    yes_token = tok.get("token_id", "")
                elif outcome in ("no", "down"):
                    no_token = tok.get("token_id", "")

            if not yes_token or not no_token:
                continue

            mi = MarketInfo(
                condition_id=m.get("condition_id", ""),
                question=m.get("question", ""),
                yes_token_id=yes_token,
                no_token_id=no_token,
                end_time=end_ts,
                active=True,
                neg_risk=m.get("neg_risk", False),
                tick_size=m.get("minimum_tick_size", "0.01"),
            )
            found.append(mi)
            self._active_markets[mi.condition_id] = mi

        logger.info(f"Discovered {len(found)} active BTC up/down markets")
        return found

    def get_active_markets(self) -> list[MarketInfo]:
        """Return currently cached active markets, pruning expired ones."""
        now = time.time()
        expired = [k for k, v in self._active_markets.items() if v.end_time < now]
        for k in expired:
            del self._active_markets[k]
        return list(self._active_markets.values())

    # ── paper price simulation ────────────────────────────────────────

    def _paper_price(self, token_id: str) -> float:
        """
        Simulate a Polymarket YES/NO token price for paper trading.
        Base: 0.50 (max-entropy market).
        Adjusted ±0.06 based on recent BTC momentum so execution
        prices feel realistic and vary with market conditions.
        """
        ticks = self.recent_ticks(6)
        if len(ticks) < 2:
            return 0.50

        ups = sum(1 for i in range(1, len(ticks)) if ticks[i].price > ticks[i - 1].price)
        downs = len(ticks) - 1 - ups
        bias = (ups - downs) / max(len(ticks) - 1, 1)  # -1.0 … +1.0

        # YES tokens appreciate when BTC goes up, NO tokens when BTC goes down
        is_no = "no" in token_id.lower()
        direction = -bias if is_no else bias

        price = 0.50 + direction * 0.06
        return round(min(0.64, max(0.36, price)), 3)  # always below MAX_BUY_PRICE

    # ── Polymarket orderbook ──────────────────────────────────────────

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get midpoint price for a token on Polymarket."""
        if self.clob is None:
            return self._paper_price(token_id)
        try:
            mid = self.clob.get_midpoint(token_id)
            return float(mid) if mid else None
        except Exception as e:
            logger.warning(f"Failed to get midpoint for {token_id}: {e}")
            return None

    def get_best_ask(self, token_id: str) -> Optional[float]:
        """Get best ask (lowest sell price) for a token."""
        if self.clob is None:
            # Paper: half-cent spread (realistic for active Polymarket BTC markets)
            return round(self._paper_price(token_id) + 0.005, 3)
        try:
            book = self.clob.get_order_book(token_id)
            asks = book.asks if hasattr(book, "asks") else []
            if asks:
                return float(asks[0].price)
        except Exception as e:
            logger.warning(f"Failed to get order book for {token_id}: {e}")
        return None

    def get_best_bid(self, token_id: str) -> Optional[float]:
        """Get best bid (highest buy price) for a token."""
        if self.clob is None:
            return round(self._paper_price(token_id) - 0.005, 3)
        try:
            book = self.clob.get_order_book(token_id)
            bids = book.bids if hasattr(book, "bids") else []
            if bids:
                return float(bids[0].price)
        except Exception as e:
            logger.warning(f"Failed to get order book for {token_id}: {e}")
        return None
