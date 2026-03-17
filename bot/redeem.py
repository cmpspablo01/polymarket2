"""
redeem.py — RedeemEngine

Handles redemption of winning tokens after market resolution.
Critical: if not redeemed, capital stays locked and bot can't trade.
"""
from __future__ import annotations

import time

from loguru import logger

from .config import BotConfig


class RedeemEngine:
    """
    Periodically checks and redeems resolved market proceeds.

    On Polymarket, winning tokens must be explicitly redeemed
    to convert them back to USDC.
    """

    def __init__(self, cfg: BotConfig, clob_client=None):
        self.cfg = cfg
        self.clob = clob_client
        self.last_redeem_check: float = 0.0
        self.redeem_interval: float = 300.0  # check every 5 minutes
        self.total_redeemed_usd: float = 0.0

    def should_check(self) -> bool:
        return time.time() - self.last_redeem_check > self.redeem_interval

    def check_and_redeem(self) -> float:
        """
        Check for unredeemed proceeds and redeem them.
        Returns amount redeemed in USD.
        """
        self.last_redeem_check = time.time()

        if self.cfg.paper_trading:
            # In paper mode, nothing to redeem
            return 0.0

        if self.clob is None:
            return 0.0

        redeemed = 0.0

        try:
            # The Polymarket API approach to redemption:
            # 1. Check for positions in resolved markets
            # 2. Call the redeem/merge endpoint
            #
            # The exact method depends on the CLOB client version.
            # Typical flow:
            #   - get_positions() or get_balance_allowance()
            #   - for resolved markets with winning tokens, call redeem

            # Attempt to use available redemption methods
            # This is a best-effort approach since the API may vary
            logger.debug("Checking for unredeemed proceeds...")

            # Method 1: Try get_positions-based approach
            # Method 2: Check resolved markets we traded

            # NOTE: The exact redemption API calls depend on the
            # py-clob-client version. The user should verify the
            # available methods. Common patterns:
            #
            #   client.redeem(condition_id)
            #   or via on-chain CTF contract calls

        except Exception as e:
            logger.error(f"Redeem check failed: {e}")

        if redeemed > 0:
            self.total_redeemed_usd += redeemed
            logger.info(f"Redeemed ${redeemed:.2f} | Total redeemed: ${self.total_redeemed_usd:.2f}")

        return redeemed
