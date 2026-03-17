"""
config.py — Central configuration loaded from environment / .env
"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _bool(val: str) -> bool:
    return val.strip().lower() in ("true", "1", "yes")


@dataclass
class PolyConfig:
    host: str = os.getenv("POLY_HOST", "https://clob.polymarket.com")
    chain_id: int = int(os.getenv("POLY_CHAIN_ID", "137"))
    private_key: str = os.getenv("POLY_PRIVATE_KEY", "")
    funder_address: str = os.getenv("POLY_FUNDER_ADDRESS", "")
    signature_type: int = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))


@dataclass
class StrategyConfig:
    momentum_ticks: int = int(os.getenv("MOMENTUM_TICKS", "4"))
    # Live: 0.03 is good for 1-minute bars (~$22 move on BTC).
    # Paper: 0.002 fires on 1-second sampled ticks for testing.
    momentum_min_change_pct: float = float(os.getenv("MOMENTUM_MIN_CHANGE_PCT", "0.002"))
    # Interval between sampled "signal ticks" (seconds).
    # 1 for paper/testing, 60 for production (1-minute bars like Kiro).
    tick_sample_seconds: int = int(os.getenv("TICK_SAMPLE_SECONDS", "1"))
    max_slippage_bps: int = int(os.getenv("MAX_SLIPPAGE_BPS", "300"))
    trade_size_usd: float = float(os.getenv("TRADE_SIZE_USD", "5.0"))
    max_buy_price: float = float(os.getenv("MAX_BUY_PRICE", "0.95"))
    min_sell_price: float = float(os.getenv("MIN_SELL_PRICE", "0.05"))
    market_end_buffer_seconds: int = int(os.getenv("MARKET_END_BUFFER_SECONDS", "60"))
    force_exit_seconds: int = int(os.getenv("FORCE_EXIT_SECONDS", "30"))


@dataclass
class RiskConfig:
    max_daily_loss_usd: float = float(os.getenv("MAX_DAILY_LOSS_USD", "50.0"))
    max_open_positions: int = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
    max_position_size_usd: float = float(os.getenv("MAX_POSITION_SIZE_USD", "50.0"))
    kill_switch: bool = _bool(os.getenv("KILL_SWITCH", "false"))


@dataclass
class ExecutionConfig:
    fok_max_retries: int = int(os.getenv("FOK_MAX_RETRIES", "3"))
    fok_retry_delay_ms: int = int(os.getenv("FOK_RETRY_DELAY_MS", "200"))


@dataclass
class BotConfig:
    poly: PolyConfig = field(default_factory=PolyConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    paper_trading: bool = _bool(os.getenv("PAPER_TRADING", "true"))
