"""Quick test to verify BTC price fetching works."""
import asyncio
from bot.config import BotConfig
from bot.market_data import MarketDataEngine


async def test():
    cfg = BotConfig()
    engine = MarketDataEngine(cfg)
    await engine.start()
    for i in range(6):
        ticks = await engine.fetch_btc_prices()
        for t in ticks:
            print(f"  [{t.source}] BTC = ${t.price:,.2f}")
        await asyncio.sleep(1)
    print(f"Total ticks: {len(engine.ticks)}")
    latest = engine.latest_btc_price()
    if latest:
        print(f"Latest: ${latest:,.2f}")
    await engine.stop()


asyncio.run(test())
