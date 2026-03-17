"""
market_data.py — MarketDataEngine

Responsibilities:
 1. Stream real-time BTC prices from Binance + Coinbase via WebSocket (<100ms latency).
 2. Discover active BTC up/down markets on Polymarket (REST API or CLOB client).
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

try:
    from py_clob_client.client import ClobClient
except ImportError:
    ClobClient = None  # py-clob-client not installed — REST API used instead

from .config import BotConfig
from .models import MarketInfo, PriceTick
from .polymarket_api import PolymarketAPI


class MarketDataEngine:
    """Provides real-time BTC prices via WebSocket and Polymarket market data."""

    MAX_TICKS = 200

    # Binance aggTrade: every single trade on the order book, ~10-50ms latency
    _WS_BINANCE = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
    # Coinbase Exchange (stable public endpoint, no auth required)
    _WS_COINBASE = "wss://ws-feed.exchange.coinbase.com"

    def __init__(self, cfg: BotConfig, clob_client=None):
        self.cfg = cfg
        self.clob = clob_client
        self.ticks: deque[PriceTick] = deque(maxlen=self.MAX_TICKS)
        # Signal ticks — one sample per second, used by SignalEngine for momentum
        self.signal_ticks: deque[PriceTick] = deque(maxlen=self.MAX_TICKS)
        self._active_markets: dict[str, MarketInfo] = {}
        self._ws_running = False
        self._ws_tasks: list[asyncio.Task] = []
        self._tick_event: asyncio.Event = asyncio.Event()
        # Polymarket REST API (works without py-clob-client)
        self._poly_api = PolymarketAPI()
        # Cached real prices: token_id -> {mid, ask, bid, ts}
        self._poly_cache: dict[str, dict] = {}
        # Best long-term BTC market used as price reference when no short-term exists
        self._reference_market: Optional[MarketInfo] = None

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self):
        self._ws_running = True
        await self._poly_api.start()
        self._ws_tasks = [
            asyncio.create_task(self._binance_ws_loop(), name="ws-binance"),
            asyncio.create_task(self._coinbase_ws_loop(), name="ws-coinbase"),
            asyncio.create_task(self._sampler_loop(), name="ws-sampler"),
            asyncio.create_task(self._polymarket_poller(), name="poly-poller"),
        ]
        logger.info("MarketDataEngine started (WebSocket mode — Binance + Coinbase)")

    async def stop(self):
        self._ws_running = False
        for task in self._ws_tasks:
            task.cancel()
        # return_exceptions=True suppresses CancelledError propagation
        await asyncio.gather(*self._ws_tasks, return_exceptions=True)
        self._ws_tasks.clear()
        await self._poly_api.stop()
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

    # Maximum market duration to consider "short-term" (24 hours)
    _MAX_MARKET_DURATION = 86400

    async def discover_btc_markets(self) -> list[MarketInfo]:
        """
        Scan Polymarket for active BTC markets.
        Tries REST API first (no auth needed), falls back to CLOB client.
        Returns short-term markets (< 24h to expiry).
        Separates currently-active from future markets for logging.
        Long-term markets stored as _reference_market for price data.
        """
        now = time.time()

        # 1. Try REST API (works without py-clob-client)
        all_markets = await self._poly_api.discover_btc_markets()

        # 2. Fall back to CLOB client if available
        if not all_markets and self.clob is not None:
            all_markets = self._discover_via_clob()

        if not all_markets:
            return []

        # Separate short-term (tradeable) from long-term (reference only)
        short_term = [m for m in all_markets if m.end_time <= now + self._MAX_MARKET_DURATION]
        long_term = [m for m in all_markets if m.end_time > now + self._MAX_MARKET_DURATION]

        if short_term:
            # Further classify: currently active vs future
            active_now = [m for m in short_term if m.start_time <= now]
            future = [m for m in short_term if m.start_time > now]

            for mi in short_term:
                self._active_markets[mi.condition_id] = mi

            logger.info(
                f"Discovered {len(short_term)} 15-min BTC Up/Down markets "
                f"({len(active_now)} active now, {len(future)} upcoming)"
            )
            if future and not active_now:
                # Find nearest future market
                nearest = min(future, key=lambda m: m.start_time)
                mins_until = (nearest.start_time - now) / 60
                logger.info(
                    f"Next market starts in {mins_until:.0f}min: "
                    f"{nearest.question[:60]}"
                )
            return short_term

        # No short-term markets — store best long-term as reference for real prices
        if long_term:
            self._reference_market = long_term[0]
            # Kick off a background check to find the most liquid reference
            asyncio.create_task(self._pick_best_reference(long_term))
            logger.info(
                f"Found {len(long_term)} long-term BTC markets (no short-term). "
                f"Polling reference prices."
            )

        return []

    async def _pick_best_reference(self, candidates: list[MarketInfo]):
        """Pick the long-term BTC market with the tightest spread as reference."""
        best_market = candidates[0]
        best_spread = float("inf")

        for m in candidates[:5]:  # check top 5 to limit requests
            book = await self._poly_api.get_orderbook(m.yes_token_id)
            asks = book.get("asks", [])
            bids = book.get("bids", [])
            if asks and bids:
                spread = float(asks[0]["price"]) - float(bids[0]["price"])
                if spread < best_spread:
                    best_spread = spread
                    best_market = m

        self._reference_market = best_market
        logger.info(
            f"Reference market: '{best_market.question[:50]}' "
            f"(spread={best_spread:.3f})"
        )

        return []

    def _discover_via_clob(self) -> list[MarketInfo]:
        """Legacy discovery using py-clob-client (requires auth)."""
        try:
            markets_resp = self.clob.get_simplified_markets()
            markets_data = markets_resp.get("data", []) if isinstance(markets_resp, dict) else []
        except Exception as e:
            logger.error(f"Failed to fetch Polymarket markets via CLOB: {e}")
            return []

        found: list[MarketInfo] = []
        now = time.time()

        for m in markets_data:
            question = (m.get("question") or "").lower()
            is_btc = "bitcoin" in question or "btc" in question
            is_updown = "up or down" in question or "higher or lower" in question
            tokens = m.get("tokens", [])

            if not (is_btc and is_updown and len(tokens) >= 2):
                continue

            end_ts = float(m.get("end_date_iso", 0) or 0)
            if end_ts == 0:
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

        logger.info(f"Discovered {len(found)} BTC markets via CLOB client")
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

    # ── Polymarket price poller ────────────────────────────────────────

    async def _polymarket_poller(self):
        """
        Background task: poll real Polymarket orderbooks every 3 seconds
        for all active (non-synthetic) tokens.  Populates _poly_cache.
        """
        while self._ws_running:
            await asyncio.sleep(3.0)
            tokens: set[str] = set()
            for market in self._active_markets.values():
                # Skip synthetic paper tokens
                if market.yes_token_id.startswith("paper_"):
                    continue
                tokens.add(market.yes_token_id)
                tokens.add(market.no_token_id)

            # Also poll reference market (for hybrid paper pricing)
            if self._reference_market:
                tokens.add(self._reference_market.yes_token_id)
                tokens.add(self._reference_market.no_token_id)

            for token_id in tokens:
                try:
                    book = await self._poly_api.get_orderbook(token_id)
                    asks = book.get("asks", [])
                    bids = book.get("bids", [])
                    ask = float(asks[0]["price"]) if asks else None
                    bid = float(bids[0]["price"]) if bids else None
                    mid = None
                    if ask is not None and bid is not None:
                        mid = round((ask + bid) / 2, 4)
                    elif ask is not None:
                        mid = ask
                    elif bid is not None:
                        mid = bid
                    self._poly_cache[token_id] = {
                        "mid": mid, "ask": ask, "bid": bid,
                        "ts": time.time(),
                    }
                except asyncio.CancelledError:
                    return
                except Exception:
                    pass  # logged at API layer

    # ── Polymarket orderbook ──────────────────────────────────────────

    def _resolve_token(self, token_id: str) -> str:
        """Map paper token IDs to reference market token IDs when available.
        Only does so if the reference market has reasonable liquidity."""
        if self._reference_market and token_id.startswith("paper_"):
            ref_id = (
                self._reference_market.yes_token_id
                if "yes" in token_id
                else self._reference_market.no_token_id
            )
            # Validate: only use reference if spread < 0.20 (reasonable liquidity)
            cached = self._poly_cache.get(ref_id)
            if cached:
                ask = cached.get("ask")
                bid = cached.get("bid")
                if ask is not None and bid is not None and (ask - bid) < 0.20:
                    return ref_id
            # Spread too wide or no cache yet — will fall back to paper price
        return token_id

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get midpoint price — real cache > CLOB client > paper simulation."""
        # 1. Real cached price from REST poller (also checks reference market)
        real_id = self._resolve_token(token_id)
        cached = self._poly_cache.get(real_id)
        if cached and cached.get("mid") is not None:
            if time.time() - cached["ts"] < 30:  # fresh enough
                return cached["mid"]

        # 2. Live CLOB client (if available)
        if self.clob is not None:
            try:
                mid = self.clob.get_midpoint(token_id)
                return float(mid) if mid else None
            except Exception:
                pass

        # 3. Paper price fallback
        return self._paper_price(token_id)

    def get_best_ask(self, token_id: str) -> Optional[float]:
        """Get best ask — real cache > CLOB client > paper simulation."""
        real_id = self._resolve_token(token_id)
        cached = self._poly_cache.get(real_id)
        if cached and cached.get("ask") is not None:
            if time.time() - cached["ts"] < 30:
                return cached["ask"]

        if self.clob is not None:
            try:
                book = self.clob.get_order_book(token_id)
                asks = book.asks if hasattr(book, "asks") else []
                if asks:
                    return float(asks[0].price)
            except Exception:
                pass

        return round(self._paper_price(token_id) + 0.005, 3)

    def get_best_bid(self, token_id: str) -> Optional[float]:
        """Get best bid — real cache > CLOB client > paper simulation."""
        real_id = self._resolve_token(token_id)
        cached = self._poly_cache.get(real_id)
        if cached and cached.get("bid") is not None:
            if time.time() - cached["ts"] < 30:
                return cached["bid"]

        if self.clob is not None:
            try:
                book = self.clob.get_order_book(token_id)
                bids = book.bids if hasattr(book, "bids") else []
                if bids:
                    return float(bids[0].price)
            except Exception:
                pass

        return round(self._paper_price(token_id) - 0.005, 3)
