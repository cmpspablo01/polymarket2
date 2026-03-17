"""
test_ws.py — Test WebSocket BTC feed (Binance + Coinbase)
Run: python test_ws.py
"""
import asyncio
from bot.config import BotConfig
from bot.market_data import MarketDataEngine


async def main():
    cfg = BotConfig()
    engine = MarketDataEngine(cfg)
    await engine.start()
    print("Waiting for WebSocket ticks...\n")

    for i in range(15):
        got = await engine.wait_for_tick(timeout=3.0)
        if got and engine.ticks:
            t = engine.ticks[-1]
            print(f"  Tick {i+1:02d}: {t.source:8s}  BTC = ${t.price:,.2f}")
        else:
            print(f"  Tick {i+1:02d}: TIMEOUT — WebSocket not connected yet?")

    await engine.stop()
    print(f"\nTotal ticks in buffer: {len(engine.ticks)}")
    if engine.ticks:
        prices = [t.price for t in engine.ticks]
        print(f"Price range: ${min(prices):,.2f} — ${max(prices):,.2f}")


if __name__ == "__main__":
    asyncio.run(main())
