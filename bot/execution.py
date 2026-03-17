"""
execution.py — ExecutionEngine

Handles order placement on Polymarket with:
 - FOK orders with bounded retries
 - Price guards (max slippage)
 - Paper trading mode
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from loguru import logger

from .config import BotConfig
from .market_data import MarketDataEngine
from .models import (
    MarketInfo,
    OrderAttempt,
    OrderStatus,
    OrderType,
    Signal,
    Side,
)


class ExecutionEngine:
    """Places and manages orders on Polymarket."""

    def __init__(self, cfg: BotConfig, data_engine: MarketDataEngine, clob_client=None):
        self.cfg = cfg
        self.data = data_engine
        self.clob = clob_client

    async def execute_signal(
        self, signal: Signal, market: MarketInfo
    ) -> OrderAttempt:
        """
        Try to fill a signal via FOK order with retries.
        Returns the final OrderAttempt (filled or failed).
        """
        token_id = (
            market.yes_token_id
            if signal.side == Side.BUY_YES
            else market.no_token_id
        )

        best_attempt = OrderAttempt(
            signal_id=signal.signal_id,
            order_type=OrderType.FOK,
            status=OrderStatus.REJECTED,
            reason="no attempts made",
        )

        for attempt_idx in range(self.cfg.execution.fok_max_retries):
            # Re-fetch price before each attempt
            current_price = self.data.get_best_ask(token_id)
            if current_price is None:
                current_price = self.data.get_midpoint(token_id)
            if current_price is None:
                best_attempt.reason = "could not fetch price"
                logger.warning(f"Attempt {attempt_idx}: no price for {token_id}")
                break

            # Slippage check
            max_price = signal.theoretical_entry_price * (
                1 + signal.max_slippage_bps / 10000
            )
            if current_price > max_price:
                best_attempt.reason = (
                    f"price {current_price:.4f} > max {max_price:.4f}"
                )
                logger.info(
                    f"Attempt {attempt_idx}: slippage exceeded "
                    f"({current_price:.4f} > {max_price:.4f})"
                )
                break

            # Hard price guard
            if current_price > self.cfg.strategy.max_buy_price:
                best_attempt.reason = f"price {current_price:.4f} > hard max {self.cfg.strategy.max_buy_price}"
                break

            # Calculate shares from USD amount
            shares = signal.size_usd / current_price if current_price > 0 else 0
            if shares <= 0:
                best_attempt.reason = "zero shares"
                break

            attempt = OrderAttempt(
                signal_id=signal.signal_id,
                attempt_index=attempt_idx,
                order_type=OrderType.FOK,
                limit_price=current_price,
                sent_ts=time.time(),
            )

            if self.cfg.paper_trading:
                # Simulate fill
                attempt.status = OrderStatus.FILLED
                attempt.filled_qty = shares
                attempt.avg_fill_price = current_price
                attempt.ack_ts = time.time()
                attempt.reason = "paper_fill"
                logger.info(
                    f"[PAPER] FOK filled @ ${current_price:.3f} x {shares:.1f} "
                    f"(${signal.size_usd:.2f})"
                )
                return attempt

            # Real order
            try:
                attempt = await self._place_fok_order(
                    attempt, token_id, current_price, signal.size_usd, market
                )
                if attempt.status == OrderStatus.FILLED:
                    return attempt

                best_attempt = attempt
            except Exception as e:
                attempt.status = OrderStatus.REJECTED
                attempt.reason = str(e)
                attempt.ack_ts = time.time()
                best_attempt = attempt
                logger.error(f"Attempt {attempt_idx} error: {e}")

            # Wait before retry
            if attempt_idx < self.cfg.execution.fok_max_retries - 1:
                await asyncio.sleep(self.cfg.execution.fok_retry_delay_ms / 1000)

        return best_attempt

    async def _place_fok_order(
        self,
        attempt: OrderAttempt,
        token_id: str,
        price: float,
        amount_usd: float,
        market: MarketInfo,
    ) -> OrderAttempt:
        """Place a real FOK market order on Polymarket."""
        from py_clob_client.clob_types import MarketOrderArgs
        from py_clob_client.clob_types import OrderType as PolyOrderType
        from py_clob_client.order_builder.constants import BUY

        mo = MarketOrderArgs(
            token_id=token_id,
            amount=amount_usd,
            side=BUY,
            order_type=PolyOrderType.FOK,
        )
        signed = self.clob.create_market_order(mo)
        resp = self.clob.post_order(signed, PolyOrderType.FOK)

        attempt.ack_ts = time.time()

        if resp and resp.get("success"):
            attempt.status = OrderStatus.FILLED
            attempt.filled_qty = amount_usd / price
            attempt.avg_fill_price = price
            attempt.order_id = resp.get("orderID", "")
            attempt.reason = "filled"
            logger.info(
                f"FOK filled @ ${price:.3f} x {attempt.filled_qty:.1f} "
                f"(${amount_usd:.2f})"
            )
        else:
            attempt.status = OrderStatus.REJECTED
            attempt.reason = str(resp)
            logger.info(f"FOK rejected: {resp}")

        return attempt

    async def sell_position(
        self, token_id: str, shares: float, min_price: float, market: MarketInfo
    ) -> OrderAttempt:
        """Sell/exit a position. Used for locking profits or force-exit."""
        attempt = OrderAttempt(
            order_type=OrderType.FOK,
            sent_ts=time.time(),
        )

        current_bid = self.data.get_best_bid(token_id)
        if current_bid is None:
            attempt.status = OrderStatus.REJECTED
            attempt.reason = "no bid"
            return attempt

        if current_bid < min_price:
            attempt.status = OrderStatus.REJECTED
            attempt.reason = f"bid {current_bid:.4f} < min {min_price:.4f}"
            return attempt

        if self.cfg.paper_trading:
            attempt.status = OrderStatus.FILLED
            attempt.filled_qty = shares
            attempt.avg_fill_price = current_bid
            attempt.ack_ts = time.time()
            attempt.reason = "paper_sell"
            logger.info(f"[PAPER] SELL filled @ ${current_bid:.3f} x {shares:.1f}")
            return attempt

        # Real sell
        try:
            from py_clob_client.clob_types import MarketOrderArgs
            from py_clob_client.clob_types import OrderType as PolyOrderType
            from py_clob_client.order_builder.constants import SELL

            mo = MarketOrderArgs(
                token_id=token_id,
                amount=shares * current_bid,
                side=SELL,
                order_type=PolyOrderType.FOK,
            )
            signed = self.clob.create_market_order(mo)
            resp = self.clob.post_order(signed, PolyOrderType.FOK)

            attempt.ack_ts = time.time()
            if resp and resp.get("success"):
                attempt.status = OrderStatus.FILLED
                attempt.filled_qty = shares
                attempt.avg_fill_price = current_bid
                attempt.order_id = resp.get("orderID", "")
            else:
                attempt.status = OrderStatus.REJECTED
                attempt.reason = str(resp)
        except Exception as e:
            attempt.status = OrderStatus.REJECTED
            attempt.reason = str(e)
            attempt.ack_ts = time.time()

        return attempt
