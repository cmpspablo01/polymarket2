"""
portfolio.py — PortfolioEngine

Tracks open positions, calculates real-time P&L, handles position lifecycle.
"""
from __future__ import annotations

import time
from typing import Optional

from loguru import logger

from .config import BotConfig
from .market_data import MarketDataEngine
from .models import (
    MarketInfo,
    OrderAttempt,
    OrderStatus,
    Position,
    Signal,
    Side,
    TradeOutcome,
    TradeResult,
)


class PortfolioEngine:
    """Manages open positions and trade outcomes."""

    def __init__(self, cfg: BotConfig, data_engine: MarketDataEngine):
        self.cfg = cfg
        self.data = data_engine
        self.positions: dict[str, Position] = {}  # signal_id -> Position
        self.outcomes: list[TradeOutcome] = []
        self.balance_usd: float = 0.0

    def open_position(
        self, signal: Signal, attempt: OrderAttempt, market: MarketInfo
    ) -> Optional[Position]:
        """Record a new open position from a filled order."""
        if attempt.status != OrderStatus.FILLED:
            return None

        token_id = (
            market.yes_token_id
            if signal.side == Side.BUY_YES
            else market.no_token_id
        )

        pos = Position(
            market_id=signal.market_id,
            side=signal.side,
            token_id=token_id,
            entry_price=attempt.avg_fill_price,
            size=attempt.filled_qty,
            cost_usd=attempt.avg_fill_price * attempt.filled_qty,
            signal_id=signal.signal_id,
            end_time=market.end_time,
        )
        self.positions[signal.signal_id] = pos
        self.balance_usd -= pos.cost_usd
        logger.info(
            f"Position opened: {signal.side.value} {pos.size:.1f} shares "
            f"@ ${pos.entry_price:.3f} (cost ${pos.cost_usd:.2f})"
        )
        return pos

    def close_position(
        self, signal_id: str, exit_price: float, exit_qty: Optional[float] = None
    ) -> Optional[TradeOutcome]:
        """Close a position and compute P&L."""
        pos = self.positions.pop(signal_id, None)
        if pos is None:
            return None

        qty = exit_qty if exit_qty is not None else pos.size
        proceeds = exit_price * qty
        cost = pos.entry_price * qty
        pnl = proceeds - cost

        result = TradeResult.WIN if pnl > 0 else TradeResult.LOSS

        outcome = TradeOutcome(
            signal_id=signal_id,
            executed=True,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            size_usd=pos.cost_usd,
            real_pnl=pnl,
            result_real=result,
            open_ts=pos.open_ts,
            close_ts=time.time(),
            market_id=pos.market_id,
        )
        self.outcomes.append(outcome)
        self.balance_usd += proceeds

        logger.info(
            f"Position closed: {result.value} | PnL=${pnl:+.2f} | "
            f"entry=${pos.entry_price:.3f} exit=${exit_price:.3f}"
        )
        return outcome

    def close_at_resolution(self, signal_id: str, won: bool) -> Optional[TradeOutcome]:
        """
        Close position at market resolution.
        won=True  → token worth $1.00
        won=False → token worth $0.00
        """
        exit_price = 1.0 if won else 0.0
        return self.close_position(signal_id, exit_price)

    def record_theoretical(
        self, signal: Signal, market: MarketInfo, won: bool
    ) -> TradeOutcome:
        """
        Record a theoretical outcome for a signal that was NOT executed
        (failed order, risk blocked, etc.)
        """
        exit_price = 1.0 if won else 0.0
        pnl = (exit_price - signal.theoretical_entry_price) * (
            signal.size_usd / signal.theoretical_entry_price
            if signal.theoretical_entry_price > 0
            else 0
        )
        result = TradeResult.WIN if pnl > 0 else TradeResult.LOSS

        outcome = TradeOutcome(
            signal_id=signal.signal_id,
            executed=False,
            side=signal.side,
            entry_price=signal.theoretical_entry_price,
            exit_price=exit_price,
            size_usd=signal.size_usd,
            theoretical_pnl=pnl,
            result_theoretical=result,
            open_ts=signal.timestamp,
            close_ts=time.time(),
            market_id=signal.market_id,
        )
        self.outcomes.append(outcome)
        return outcome

    def get_position(self, signal_id: str) -> Optional[Position]:
        return self.positions.get(signal_id)

    def open_positions(self) -> list[Position]:
        return list(self.positions.values())

    def positions_for_market(self, market_id: str) -> list[Position]:
        return [p for p in self.positions.values() if p.market_id == market_id]
