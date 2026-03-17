"""
models.py — Data models for events, signals, orders, trades, positions.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ── Enums ──────────────────────────────────────────────────────────────

class Side(str, Enum):
    BUY_YES = "BUY_YES"
    BUY_NO = "BUY_NO"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class OrderType(str, Enum):
    FOK = "FOK"
    FAK = "FAK"
    GTC = "GTC"


class TradeResult(str, Enum):
    WIN = "WIN"
    LOSS = "LOSS"
    PENDING = "PENDING"


# ── Market info ────────────────────────────────────────────────────────

@dataclass
class MarketInfo:
    """A BTC up/down 15-min (or 5-min) market on Polymarket."""
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    end_time: float          # epoch seconds
    start_time: float = 0.0
    active: bool = True
    neg_risk: bool = False
    tick_size: str = "0.01"


# ── Price tick from exchange ───────────────────────────────────────────

@dataclass
class PriceTick:
    source: str          # "binance", "coinbase"
    symbol: str          # "BTC/USDT"
    price: float
    timestamp: float     # epoch seconds (monotonic-compatible)


# ── Signal ─────────────────────────────────────────────────────────────

@dataclass
class Signal:
    signal_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = field(default_factory=time.time)
    market_id: str = ""
    side: Side = Side.BUY_YES
    theoretical_entry_price: float = 0.0
    size_usd: float = 0.0
    ttl_ms: int = 5000
    max_slippage_bps: int = 300
    momentum_score: float = 0.0
    pattern_length: int = 0
    price_change_pct: float = 0.0


# ── Order attempt ─────────────────────────────────────────────────────

@dataclass
class OrderAttempt:
    signal_id: str = ""
    attempt_index: int = 0
    order_type: OrderType = OrderType.FOK
    limit_price: float = 0.0
    sent_ts: float = 0.0
    ack_ts: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    reason: str = ""
    order_id: str = ""


# ── Trade outcome ─────────────────────────────────────────────────────

@dataclass
class TradeOutcome:
    signal_id: str = ""
    executed: bool = False
    side: Side = Side.BUY_YES
    entry_price: float = 0.0
    exit_price: Optional[float] = None
    size_usd: float = 0.0
    real_pnl: float = 0.0
    theoretical_pnl: float = 0.0
    result_real: TradeResult = TradeResult.PENDING
    result_theoretical: TradeResult = TradeResult.PENDING
    open_ts: float = 0.0
    close_ts: Optional[float] = None
    market_id: str = ""


# ── Position (open) ───────────────────────────────────────────────────

@dataclass
class Position:
    market_id: str = ""
    side: Side = Side.BUY_YES
    token_id: str = ""
    entry_price: float = 0.0
    size: float = 0.0          # number of shares
    cost_usd: float = 0.0
    open_ts: float = field(default_factory=time.time)
    signal_id: str = ""
    end_time: float = 0.0      # market end timestamp
