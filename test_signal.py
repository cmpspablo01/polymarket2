"""
test_signal.py — Diagnose signal engine in paper mode
Run: python test_signal.py
"""
import asyncio
import time

from bot.config import BotConfig
from bot.market_data import MarketDataEngine
from bot.signal_engine import SignalEngine
from bot.models import MarketInfo


async def main():
    cfg = BotConfig()
    engine = MarketDataEngine(cfg)
    await engine.start()

    market = MarketInfo(
        condition_id="test",
        question="[PAPER] Will BTC go up in next 15 minutes?",
        yes_token_id="paper_yes_token",
        no_token_id="paper_no_token",
        end_time=time.time() + 900,
        active=True,
    )

    sig_engine = SignalEngine(cfg, engine)

    print(f"\nConfig: momentum_ticks={cfg.strategy.momentum_ticks}, min_change_pct={cfg.strategy.momentum_min_change_pct}%\n")
    print("Collecting ticks for 10s...\n")
    await asyncio.sleep(10)

    ticks = engine.recent_ticks(20)
    print(f"Ticks collected: {len(ticks)}")
    print("\nRecent tick deltas:")
    for i in range(1, min(10, len(ticks))):
        diff = ticks[i].price - ticks[i - 1].price
        pct = abs(diff) / ticks[i - 1].price * 100
        direction = "UP  " if diff > 0 else ("DOWN" if diff < 0 else "FLAT")
        print(f"  [{direction}] {ticks[i].source:8s}: ${ticks[i].price:,.2f}  delta={diff:+.4f}  ({pct:.5f}%)")

    print(f"\nPaper midpoint (YES): {engine.get_midpoint(market.yes_token_id)}")
    print(f"Paper midpoint (NO):  {engine.get_midpoint(market.no_token_id)}")
    print(f"Paper best_ask (YES): {engine.get_best_ask(market.yes_token_id)}")

    sig = sig_engine.evaluate(market)
    if sig:
        print(f"\n[SIGNAL] {sig.side.value} @ {sig.theoretical_entry_price:.3f}  momentum={sig.momentum_score:.4f}%", flush=True)
    else:
        print(f"\n[NO SIGNAL] BTC move over {cfg.strategy.momentum_ticks} ticks too small or direction inconsistent", flush=True)

        # Show what the cumulative move was
        if len(ticks) >= cfg.strategy.momentum_ticks + 1:
            n = cfg.strategy.momentum_ticks
            start = ticks[-(n + 1)].price
            end = ticks[-1].price
            change = abs(end - start) / start * 100
            print(f"   Cumulative change over last {n} ticks: {change:.5f}%  (need {cfg.strategy.momentum_min_change_pct}%)", flush=True)

    try:
        await engine.stop()
    except BaseException:
        pass


if __name__ == "__main__":
    asyncio.run(main())
