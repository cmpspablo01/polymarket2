"""
bot.py — Main orchestrator / bot loop

This is the heart of the system. It:
 1. Discovers active BTC up/down markets on Polymarket
 2. Polls BTC prices from Binance + Coinbase
 3. Runs momentum signal detection
 4. Executes orders (FOK with retries + guards)
 5. Manages positions (open / close / force-exit)
 6. Tracks real vs theoretical P&L
 7. Handles redemption of proceeds
 8. Prints periodic performance reports
"""
from __future__ import annotations

import asyncio
import signal
import sys
import time
from typing import Optional

from loguru import logger

from .config import BotConfig
from .execution import ExecutionEngine
from .market_data import MarketDataEngine
from .metrics import MetricsEngine
from .models import (
    MarketInfo,
    OrderStatus,
    Signal,
    Side,
    TradeResult,
)
from .portfolio import PortfolioEngine
from .redeem import RedeemEngine
from .risk import RiskEngine
from .signal_engine import SignalEngine


class TradingBot:
    """Main trading bot orchestrator."""

    # How often to poll BTC prices (seconds)
    TICK_INTERVAL = 1.0
    # How often to discover new markets (seconds)
    MARKET_SCAN_INTERVAL = 60.0
    # How often to print the performance report (seconds)
    REPORT_INTERVAL = 300.0

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.clob_client = None
        self._running = False

        # Build CLOB client if not paper-only
        if cfg.poly.private_key and not cfg.paper_trading:
            self._init_clob_client()

        # Engines
        self.data = MarketDataEngine(cfg, self.clob_client)
        self.signals = SignalEngine(cfg, self.data)
        self.execution = ExecutionEngine(cfg, self.data, self.clob_client)
        self.risk = RiskEngine(cfg)
        self.portfolio = PortfolioEngine(cfg, self.data)
        self.redeem = RedeemEngine(cfg, self.clob_client)
        self.metrics = MetricsEngine()

        # State
        self._pending_signals: dict[str, tuple[Signal, MarketInfo]] = {}
        self._last_market_scan: float = 0.0
        self._last_report: float = 0.0

    def _init_clob_client(self):
        """Initialize Polymarket CLOB client with authentication."""
        try:
            from py_clob_client.client import ClobClient

            self.clob_client = ClobClient(
                self.cfg.poly.host,
                key=self.cfg.poly.private_key,
                chain_id=self.cfg.poly.chain_id,
                signature_type=self.cfg.poly.signature_type,
                funder=self.cfg.poly.funder_address,
            )
            self.clob_client.set_api_creds(
                self.clob_client.create_or_derive_api_creds()
            )
            logger.info("CLOB client initialized and authenticated")
        except Exception as e:
            logger.error(f"Failed to init CLOB client: {e}")
            logger.warning("Falling back to paper trading mode")
            self.cfg.paper_trading = True
            self.clob_client = None

    async def start(self):
        """Start the bot."""
        logger.info("=" * 50)
        logger.info("  POLYMARKET BTC MOMENTUM BOT")
        logger.info(f"  Mode: {'PAPER' if self.cfg.paper_trading else 'LIVE'}")
        logger.info(f"  Trade size: ${self.cfg.strategy.trade_size_usd}")
        logger.info(f"  Momentum ticks: {self.cfg.strategy.momentum_ticks}")
        logger.info(f"  Max slippage: {self.cfg.strategy.max_slippage_bps} bps")
        logger.info("=" * 50)

        await self.data.start()
        self._running = True

        # Handle graceful shutdown
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                asyncio.get_event_loop().add_signal_handler(
                    sig, lambda: asyncio.create_task(self.stop())
                )
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass

        try:
            await self._main_loop()
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        finally:
            await self.stop()

    async def stop(self):
        """Graceful shutdown."""
        self._running = False
        logger.info("Shutting down...")
        self.metrics.print_report()
        await self.data.stop()
        logger.info("Bot stopped")

    async def _main_loop(self):
        """Core event loop."""
        logger.info("Entering main loop...")

        while self._running:
            try:
                loop_start = time.time()

                # 1. Discover markets periodically
                if time.time() - self._last_market_scan > self.MARKET_SCAN_INTERVAL:
                    await self._scan_markets()

                # 2. Fetch BTC prices
                ticks = await self.data.fetch_btc_prices()
                if not ticks:
                    logger.debug("No BTC ticks received, waiting...")
                    await asyncio.sleep(self.TICK_INTERVAL)
                    continue

                btc_price = self.data.latest_btc_price()
                if btc_price:
                    logger.debug(f"BTC: ${btc_price:,.2f}")

                # 3. Check active markets
                markets = self.data.get_active_markets()
                if not markets:
                    logger.debug("No active markets, scanning...")
                    self._last_market_scan = 0  # force rescan
                    await asyncio.sleep(self.TICK_INTERVAL)
                    continue

                # 4. For each active market, evaluate signals
                for market in markets:
                    await self._process_market(market)

                # 5. Manage open positions (check for exits)
                await self._manage_positions(markets)

                # 6. Redeem proceeds
                if self.redeem.should_check():
                    self.redeem.check_and_redeem()

                # 7. Print report periodically
                if time.time() - self._last_report > self.REPORT_INTERVAL:
                    self.metrics.print_report()
                    self._last_report = time.time()

                # Rate limit
                elapsed = time.time() - loop_start
                sleep_time = max(0, self.TICK_INTERVAL - elapsed)
                await asyncio.sleep(sleep_time)

            except Exception as e:
                logger.error(f"Main loop error: {e}")
                await asyncio.sleep(1.0)

    async def _scan_markets(self):
        """Discover active BTC up/down markets."""
        self._last_market_scan = time.time()

        if self.cfg.paper_trading and self.clob_client is None:
            # In pure paper mode without CLOB client, create a synthetic market
            self._create_synthetic_market()
            return

        self.data.discover_btc_markets()

    def _create_synthetic_market(self):
        """Create a fake market for paper trading without API access."""
        now = time.time()
        # Create a 15-minute market starting now
        market = MarketInfo(
            condition_id="paper_btc_15min",
            question="[PAPER] Will BTC go up in next 15 minutes?",
            yes_token_id="paper_yes_token",
            no_token_id="paper_no_token",
            end_time=now + 900,  # 15 minutes from now
            start_time=now,
            active=True,
        )
        self.data._active_markets[market.condition_id] = market
        logger.info(f"Created synthetic paper market (expires in 15 min)")

    async def _process_market(self, market: MarketInfo):
        """Evaluate and potentially trade on a single market."""
        # Don't trade if too close to end
        seconds_left = market.end_time - time.time()
        if seconds_left < self.cfg.strategy.market_end_buffer_seconds:
            return

        # Check for momentum signal
        signal = self.signals.evaluate(market)
        if signal is None:
            return

        # Risk check
        allowed, reason = self.risk.check(signal)
        if not allowed:
            logger.info(f"Signal {signal.signal_id} blocked by risk: {reason}")
            self._pending_signals[signal.signal_id] = (signal, market)
            return

        # Execute
        attempt = await self.execution.execute_signal(signal, market)

        if attempt.status == OrderStatus.FILLED:
            # Open position
            pos = self.portfolio.open_position(signal, attempt, market)
            if pos:
                self.risk.position_opened()
            self._pending_signals[signal.signal_id] = (signal, market)
        else:
            # Failed order — track for theoretical P&L
            logger.info(
                f"Signal {signal.signal_id} failed: {attempt.reason}"
            )
            self._pending_signals[signal.signal_id] = (signal, market)

    async def _manage_positions(self, markets: list[MarketInfo]):
        """
        Check open positions for:
         1. Profit lock (sell before expiry)
         2. Force exit (close to market end)
         3. Market resolution (token → $1 or $0)
        """
        now = time.time()
        markets_by_id = {m.condition_id: m for m in markets}

        for signal_id, pos in list(self.portfolio.positions.items()):
            market = markets_by_id.get(pos.market_id)
            if market is None:
                continue

            seconds_left = market.end_time - now

            # Force exit if close to end
            if seconds_left < self.cfg.strategy.force_exit_seconds:
                logger.info(
                    f"Force exit: {seconds_left:.0f}s left for {signal_id}"
                )
                current_price = self.data.get_best_bid(pos.token_id)
                if current_price is None:
                    # If paper trading with synthetic tokens
                    if self.cfg.paper_trading:
                        btc_price = self.data.latest_btc_price()
                        current_price = 0.55  # paper estimate
                    else:
                        continue

                outcome = self.portfolio.close_position(signal_id, current_price)
                if outcome:
                    self.risk.position_closed()
                    self.risk.record_pnl(outcome.real_pnl)
                    self.metrics.record(outcome)
                continue

            # Check for profit lock opportunity
            current_price = self.data.get_best_bid(pos.token_id)
            if current_price is None and self.cfg.paper_trading:
                # Paper: simulate price movement based on BTC momentum
                current_price = self._estimate_paper_price(pos)

            if current_price is not None:
                unrealized_pnl = (current_price - pos.entry_price) * pos.size
                # Lock profit if price moved significantly in our favor
                if unrealized_pnl > pos.cost_usd * 0.10:  # 10%+ profit
                    logger.info(
                        f"Locking profit: ${unrealized_pnl:+.2f} on {signal_id}"
                    )
                    outcome = self.portfolio.close_position(
                        signal_id, current_price
                    )
                    if outcome:
                        self.risk.position_closed()
                        self.risk.record_pnl(outcome.real_pnl)
                        self.metrics.record(outcome)

        # Check resolved markets — record theoretical for unexecuted signals
        self._resolve_expired_signals(now)

    def _estimate_paper_price(self, pos) -> float:
        """
        In paper mode, estimate current price of a position
        based on BTC price movement since entry.
        """
        ticks = self.data.recent_ticks(2)
        if len(ticks) < 2:
            return pos.entry_price

        recent_change = (ticks[-1].price - ticks[-2].price) / ticks[-2].price

        if pos.side == Side.BUY_YES:
            # YES token goes up when BTC goes up
            return min(0.99, max(0.01, pos.entry_price + recent_change * 2))
        else:
            # NO token goes up when BTC goes down
            return min(0.99, max(0.01, pos.entry_price - recent_change * 2))

    def _resolve_expired_signals(self, now: float):
        """For expired markets, resolve pending signals (theoretical P&L)."""
        expired_signals = []
        for sig_id, (signal, market) in self._pending_signals.items():
            if market.end_time > now:
                continue
            expired_signals.append(sig_id)

            # Determine if the market resolved YES or NO
            # based on BTC price direction over the market period
            ticks = self.data.recent_ticks(50)
            if len(ticks) >= 2:
                start_price = ticks[0].price
                end_price = ticks[-1].price
                btc_went_up = end_price > start_price
            else:
                btc_went_up = True  # default assumption

            # Did the signal's side win?
            if signal.side == Side.BUY_YES:
                won = btc_went_up
            else:
                won = not btc_went_up

            # If position was executed, close it
            pos = self.portfolio.get_position(sig_id)
            if pos:
                outcome = self.portfolio.close_at_resolution(sig_id, won)
                if outcome:
                    self.risk.position_closed()
                    self.risk.record_pnl(outcome.real_pnl)
                    self.metrics.record(outcome)
            else:
                # Record theoretical only
                outcome = self.portfolio.record_theoretical(signal, market, won)
                self.metrics.record(outcome)

        for sig_id in expired_signals:
            del self._pending_signals[sig_id]


def create_bot(cfg: Optional[BotConfig] = None) -> TradingBot:
    """Factory to create a configured bot instance."""
    if cfg is None:
        cfg = BotConfig()
    return TradingBot(cfg)
