"""
main.py — Entry point

Usage:
  python main.py              # Run with .env config
  python main.py --paper      # Force paper trading mode
"""
import asyncio
import sys

from loguru import logger

from bot.config import BotConfig
from bot.bot import create_bot


def setup_logging():
    """Configure loguru for structured, readable output."""
    logger.remove()
    logger.add(
        sys.stderr,
        format=(
            "<green>{time:HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<level>{message}</level>"
        ),
        level="INFO",
    )
    logger.add(
        "logs/bot_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="30 days",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {message}",
        level="DEBUG",
    )


async def main():
    setup_logging()

    cfg = BotConfig()

    # CLI override
    if "--paper" in sys.argv:
        cfg.paper_trading = True

    logger.info(f"Paper trading: {cfg.paper_trading}")

    bot = create_bot(cfg)
    await bot.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
