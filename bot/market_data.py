"""
market_data.py — MarketDataEngine

Responsibilities:
 1. Fetch BTC price from Binance + Coinbase via ccxt (public, no keys).
 2. Discover active BTC up/down markets on Polymarket via CLOB client.
 3. Fetch Polymarket orderbook / midpoint for active markets.
 4. Maintain a rolling window of BTC price ticks for momentum detection.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Optional

import ccxt.async_support as ccxt
from loguru import logger

from py_clob_client.client import ClobClient

from .config import BotConfig
from .models import MarketInfo, PriceTick


class MarketDataEngine:
    """Provides real-time BTC prices and Polymarket market data."""

    # Rolling window size (number of ticks kept)
    MAX_TICKS = 200

    def __init__(self, cfg: BotConfig, clob_client: Optional[ClobClient] = None):
        self.cfg = cfg
        self.clob = clob_client
        self.ticks: deque[PriceTick] = deque(maxlen=self.MAX_TICKS)
        self._binance: Optional[ccxt.binance] = None
        self._coinbase: Optional[ccxt.coinbase] = None
        self._active_markets: dict[str, MarketInfo] = {}

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self):
        self._binance = ccxt.binance({"enableRateLimit": True})
        self._coinbase = ccxt.coinbase({"enableRateLimit": True})
        logger.info("MarketDataEngine started")

    async def stop(self):
        for ex in (self._binance, self._coinbase):
            if ex:
                await ex.close()
        logger.info("MarketDataEngine stopped")

    # ── BTC price fetching ────────────────────────────────────────────

    async def fetch_btc_prices(self) -> list[PriceTick]:
        """Fetch latest BTC price from Binance and Coinbase concurrently."""
        ticks: list[PriceTick] = []

        async def _fetch(exchange, source: str, symbol: str):
            try:
                ticker = await exchange.fetch_ticker(symbol)
                t = PriceTick(
                    source=source,
                    symbol=symbol,
                    price=float(ticker["last"]),
                    timestamp=time.time(),
                )
                ticks.append(t)
            except Exception as e:
                logger.warning(f"Failed to fetch {source}: {e}")

        await asyncio.gather(
            _fetch(self._binance, "binance", "BTC/USDT"),
            _fetch(self._coinbase, "coinbase", "BTC/USD"),
        )

        for t in ticks:
            self.ticks.append(t)

        return ticks

    def latest_btc_price(self) -> Optional[float]:
        """Return the most recent BTC price across all sources."""
        if not self.ticks:
            return None
        return self.ticks[-1].price

    def recent_ticks(self, n: int = 20) -> list[PriceTick]:
        """Return the last n ticks."""
        return list(self.ticks)[-n:]

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

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get midpoint price for a token on Polymarket."""
        if self.clob is None:
            return None
        try:
            mid = self.clob.get_midpoint(token_id)
            return float(mid) if mid else None
        except Exception as e:
            logger.warning(f"Failed to get midpoint for {token_id}: {e}")
            return None

    def get_best_ask(self, token_id: str) -> Optional[float]:
        """Get best ask (lowest sell price) for a token."""
        if self.clob is None:
            return None
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
            return None
        try:
            book = self.clob.get_order_book(token_id)
            bids = book.bids if hasattr(book, "bids") else []
            if bids:
                return float(bids[0].price)
        except Exception as e:
            logger.warning(f"Failed to get order book for {token_id}: {e}")
        return None
