"""
metrics.py — MetricsEngine

Produces the @cc_-style performance report:
  REAL P&L (Executed Orders)
  THEORETICAL (All Signals)
"""
from __future__ import annotations

import time

from loguru import logger

from .models import TradeOutcome, TradeResult


class MetricsEngine:
    """Aggregates and displays trading metrics, real vs theoretical."""

    def __init__(self):
        self.all_signals: list[TradeOutcome] = []
        self.start_time: float = time.time()

    def record(self, outcome: TradeOutcome):
        self.all_signals.append(outcome)

    def compute(self) -> dict:
        """Compute all metrics."""
        executed = [o for o in self.all_signals if o.executed]
        theoretical = self.all_signals  # all signals count

        real_wins = sum(1 for o in executed if o.result_real == TradeResult.WIN)
        real_losses = sum(1 for o in executed if o.result_real == TradeResult.LOSS)
        real_pnl = sum(o.real_pnl for o in executed)

        # For theoretical, we count everything (executed uses real result,
        # non-executed uses theoretical result)
        theo_wins = 0
        theo_losses = 0
        theo_pnl = 0.0
        pending = 0

        for o in theoretical:
            if o.executed:
                if o.result_real == TradeResult.WIN:
                    theo_wins += 1
                elif o.result_real == TradeResult.LOSS:
                    theo_losses += 1
                theo_pnl += o.real_pnl
            else:
                if o.result_theoretical == TradeResult.WIN:
                    theo_wins += 1
                    theo_pnl += o.theoretical_pnl
                elif o.result_theoretical == TradeResult.LOSS:
                    theo_losses += 1
                    theo_pnl += o.theoretical_pnl
                elif o.result_theoretical == TradeResult.PENDING:
                    pending += 1

        total_signals = len(theoretical)
        executed_count = len(executed)
        failed_orders = total_signals - executed_count - pending

        real_wr = (real_wins / executed_count * 100) if executed_count > 0 else 0.0
        theo_total_resolved = theo_wins + theo_losses
        theo_wr = (theo_wins / theo_total_resolved * 100) if theo_total_resolved > 0 else 0.0
        avg_per_trade = (theo_pnl / theo_total_resolved) if theo_total_resolved > 0 else 0.0

        return {
            "executed_trades": executed_count,
            "real_wins": real_wins,
            "real_losses": real_losses,
            "real_win_rate": real_wr,
            "real_pnl": real_pnl,
            "total_signals": total_signals,
            "pending": pending,
            "failed_orders": failed_orders,
            "theo_wins": theo_wins,
            "theo_losses": theo_losses,
            "theo_win_rate": theo_wr,
            "theo_pnl": theo_pnl,
            "avg_per_trade": avg_per_trade,
            "uptime_hours": (time.time() - self.start_time) / 3600,
        }

    def print_report(self):
        """Print the @cc_-style performance report."""
        m = self.compute()

        pending_str = f" ({m['pending']} pending)" if m["pending"] > 0 else ""

        report = f"""
  ══════════════════════════════
  REAL P&L (Executed Orders)
  ──────────────────────────────
  Executed Trades: {m['executed_trades']}
  Wins / Losses:   {m['real_wins']} / {m['real_losses']}
  Win Rate:        {m['real_win_rate']:.1f}%
  Real P&L:        ${m['real_pnl']:+.2f}

  THEORETICAL (All Signals)
  ──────────────────────────────
  Total Signals:   {m['total_signals']}{pending_str}
  Failed Orders:   {m['failed_orders']}
  Wins / Losses:   {m['theo_wins']} / {m['theo_losses']}
  Win Rate:        {m['theo_win_rate']:.1f}%
  Theoretical P&L: ${m['theo_pnl']:+.2f}
  Avg per Trade:   ${m['avg_per_trade']:+.2f}

  Uptime:          {m['uptime_hours']:.1f}h
  ══════════════════════════════
"""
        logger.info(report)
        return m
