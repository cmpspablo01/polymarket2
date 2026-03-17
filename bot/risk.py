"""
risk.py — RiskEngine

Enforces:
 - Max daily loss
 - Max open positions
 - Max position size
 - Kill switch
"""
from __future__ import annotations

from loguru import logger

from .config import BotConfig
from .models import Signal


class RiskEngine:
    """Pre-trade risk checks."""

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.daily_pnl: float = 0.0
        self.open_position_count: int = 0
        self._killed = False

    def check(self, signal: Signal) -> tuple[bool, str]:
        """
        Returns (allowed, reason).
        allowed=True means the signal can proceed to execution.
        """
        if self.cfg.risk.kill_switch or self._killed:
            return False, "kill switch active"

        if self.daily_pnl <= -self.cfg.risk.max_daily_loss_usd:
            return False, f"daily loss limit reached ({self.daily_pnl:.2f})"

        if self.open_position_count >= self.cfg.risk.max_open_positions:
            return False, f"max open positions ({self.cfg.risk.max_open_positions})"

        if signal.size_usd > self.cfg.risk.max_position_size_usd:
            return False, f"trade size {signal.size_usd} > max {self.cfg.risk.max_position_size_usd}"

        return True, "ok"

    def record_pnl(self, pnl: float):
        self.daily_pnl += pnl

    def position_opened(self):
        self.open_position_count += 1

    def position_closed(self):
        self.open_position_count = max(0, self.open_position_count - 1)

    def kill(self):
        self._killed = True
        logger.critical("KILL SWITCH ACTIVATED")

    def reset_daily(self):
        """Call at start of each trading day."""
        self.daily_pnl = 0.0
        logger.info("Risk daily PnL reset")
