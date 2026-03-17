"""Quick test to verify BTC price fetching works."""
import asyncio

from bot.config import BotConfig
from bot.market_data import MarketDataEngine


async def test():
    cfg = BotConfig()
    engine = MarketDataEngine(cfg)
    await engine.start()
    for i in range(6):
        got = await engine.wait_for_tick(timeout=3.0)
        if got and engine.ticks:
            t = engine.ticks[-1]
            print(f"  [{t.source}] BTC = ${t.price:,.2f}")
        else:
            print("  [timeout] no BTC tick received")
    print(f"Total ticks: {len(engine.ticks)}")
    latest = engine.latest_btc_price()
    if latest:
        print(f"Latest: ${latest:,.2f}")
    await engine.stop()


asyncio.run(test())
