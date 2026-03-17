"""
signal_engine.py — Momentum-based signal generation.

Detects X consecutive BTC ticks in the same direction and generates
a Signal with direction, score, and price constraints.
"""
from __future__ import annotations

import time

from loguru import logger

from .config import BotConfig
from .market_data import MarketDataEngine
from .models import MarketInfo, PriceTick, Signal, Side


class SignalEngine:
    """
    Core strategy logic — momentum pattern detection.

    Flow:
     1. Receive recent BTC ticks.
     2. Compute direction of each consecutive pair.
     3. If N consecutive moves in same direction AND cumulative change > threshold
        → emit Signal.
    """

    def __init__(self, cfg: BotConfig, data_engine: MarketDataEngine):
        self.cfg = cfg
        self.data = data_engine

    def evaluate(self, market: MarketInfo) -> Signal | None:
        """
        Check the latest ticks for a momentum pattern.
        Returns a Signal if conditions are met, else None.
        """
        required = self.cfg.strategy.momentum_ticks + 1  # need N+1 ticks for N moves
        ticks = self.data.recent_ticks(required)

        if len(ticks) < required:
            return None

        # Compute consecutive directions
        directions: list[int] = []  # +1 up, -1 down
        for i in range(1, len(ticks)):
            diff = ticks[i].price - ticks[i - 1].price
            if diff > 0:
                directions.append(1)
            elif diff < 0:
                directions.append(-1)
            else:
                directions.append(0)

        # Check for N consecutive same-direction moves
        n = self.cfg.strategy.momentum_ticks
        tail = directions[-n:]

        if not tail:
            return None

        # All must be same non-zero direction
        if tail[0] == 0:
            return None
        if not all(d == tail[0] for d in tail):
            return None

        direction = tail[0]  # +1 = up, -1 = down

        # Compute % change over the window
        start_price = ticks[-(n + 1)].price
        end_price = ticks[-1].price
        if start_price == 0:
            return None
        change_pct = abs(end_price - start_price) / start_price * 100

        if change_pct < self.cfg.strategy.momentum_min_change_pct:
            return None

        # Check market timing — don't trade too close to end
        now = time.time()
        seconds_left = market.end_time - now
        if seconds_left < self.cfg.strategy.market_end_buffer_seconds:
            return None

        # Determine side
        side = Side.BUY_YES if direction == 1 else Side.BUY_NO

        # Get current Polymarket price for the token we'd buy
        token_id = market.yes_token_id if side == Side.BUY_YES else market.no_token_id
        current_price = self.data.get_midpoint(token_id)
        if current_price is None:
            return None

        # Price guard — don't buy if above max
        if current_price > self.cfg.strategy.max_buy_price:
            logger.debug(
                f"Signal rejected: price {current_price:.3f} > max {self.cfg.strategy.max_buy_price}"
            )
            return None

        signal = Signal(
            market_id=market.condition_id,
            side=side,
            theoretical_entry_price=current_price,
            size_usd=self.cfg.strategy.trade_size_usd,
            max_slippage_bps=self.cfg.strategy.max_slippage_bps,
            momentum_score=change_pct,
            pattern_length=n,
            price_change_pct=change_pct,
        )

        logger.info(
            f"Signal {signal.signal_id} | {side.value} | "
            f"momentum={change_pct:.4f}% over {n} ticks | "
            f"price={current_price:.3f} | market={market.question[:50]}"
        )

        return signal
