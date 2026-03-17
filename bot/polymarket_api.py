"""
polymarket_api.py — Lightweight REST client for Polymarket public APIs.

Provides read-only access to:
 - Market discovery (Gamma API)
 - Orderbook / midpoint prices (CLOB API)

No authentication or py-clob-client needed.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from loguru import logger

from .models import MarketInfo

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# ET (US/Eastern) handling — simplified UTC-4 / UTC-5
_ET_OFFSET = timezone(timedelta(hours=-4))  # EDT (March–November)


class PolymarketAPI:
    """Async HTTP client for Polymarket's public endpoints."""

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def start(self):
        self._client = httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            headers={"Accept": "application/json"},
        )

    async def stop(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── market discovery ──────────────────────────────────────────────

    async def discover_btc_markets(self) -> list[MarketInfo]:
        """
        Fetch active BTC-related markets using two strategies:
        1. Slug-based lookup for CURRENT and upcoming 5/15-min windows (today)
        2. /markets endpoint for future markets (tomorrow+)
        """
        if not self._client:
            return []

        found: list[MarketInfo] = []
        seen: set[str] = set()
        now = time.time()

        # Strategy 1: slug-based lookup for current/upcoming time windows
        slug_markets = await self._discover_by_slug(now)
        for mi in slug_markets:
            if mi.condition_id not in seen:
                found.append(mi)
                seen.add(mi.condition_id)

        # Strategy 2: /markets endpoint for future markets (sorted newest first)
        try:
            resp = await self._client.get(
                f"{GAMMA_API}/markets",
                params={
                    "closed": "false",
                    "active": "true",
                    "limit": "200",
                    "order": "startDate",
                    "ascending": "false",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            markets = data if isinstance(data, list) else data.get("data", [])
            for m in markets:
                mi = self._parse_market(m, now)
                if mi and mi.condition_id not in seen:
                    found.append(mi)
                    seen.add(mi.condition_id)
        except Exception as exc:
            logger.warning(f"Gamma API error: {exc}")

        return found

    async def _discover_by_slug(self, now: float) -> list[MarketInfo]:
        """
        Look up current and upcoming 15-min BTC Up or Down markets
        by computed event slug.
        Slug pattern: btc-updown-15m-{start_epoch}
        """
        results: list[MarketInfo] = []
        now_dt = datetime.fromtimestamp(now, tz=_ET_OFFSET)

        # Compute 15-min window boundaries (align to 0/15/30/45)
        minute = now_dt.minute
        base_15 = now_dt.replace(minute=(minute // 15) * 15, second=0, microsecond=0)

        # Generate slugs: current window + next 3 upcoming windows
        slugs: list[str] = []

        for i in range(4):
            ts = int((base_15 + timedelta(minutes=i * 15)).timestamp())
            slugs.append(f"btc-updown-15m-{ts}")

        # De-duplicate slugs
        slugs = list(dict.fromkeys(slugs))

        # Look up each slug via /events endpoint
        for slug in slugs:
            try:
                resp = await self._client.get(
                    f"{GAMMA_API}/events",
                    params={"slug": slug},
                )
                if resp.status_code != 200:
                    continue

                data = resp.json()
                events = (
                    data
                    if isinstance(data, list)
                    else [data] if isinstance(data, dict) and data.get("title") else []
                )
                for ev in events:
                    if ev.get("closed"):
                        continue
                    for m in ev.get("markets", []):
                        mi = self._parse_market(m, now)
                        if mi:
                            results.append(mi)
            except Exception:
                continue

        if results:
            logger.info(
                f"Slug-based discovery: found {len(results)} 15-min BTC Up/Down markets"
            )

        return results

    # ── Time range parser for question titles ─────────────────────────
    # Example: "Bitcoin Up or Down - March 17, 11:30PM-11:45PM ET"
    # Also:    "Bitcoin Up or Down - March 17, 1PM-1:15PM ET"

    _TIME_RE = re.compile(
        r"(\w+ \d{1,2}),?\s*"                          # "March 17"
        r"(\d{1,2}(?::\d{2})?(?:AM|PM))\s*-\s*"        # "11:30PM-" or "1PM-"
        r"(\d{1,2}(?::\d{2})?(?:AM|PM))\s*ET",          # "11:45PM ET" or "1:15PM ET"
        re.IGNORECASE,
    )

    @staticmethod
    def _normalize_time(t: str) -> str:
        """Ensure time string has :MM part, e.g. '1PM' -> '1:00PM'."""
        t = t.upper()
        if ":" not in t:
            t = t.replace("AM", ":00AM").replace("PM", ":00PM")
        return t

    def _parse_time_range(self, question: str) -> tuple[float, float]:
        """
        Extract start/end timestamps from question title.
        Returns (start_ts, end_ts) or (0, 0) if unparseable.
        """
        match = self._TIME_RE.search(question)
        if not match:
            return 0.0, 0.0

        date_part = match.group(1)     # "March 17"
        start_time = self._normalize_time(match.group(2))  # "11:30PM"
        end_time = self._normalize_time(match.group(3))     # "11:45PM"

        now = datetime.now(_ET_OFFSET)
        year = now.year

        try:
            start_dt = datetime.strptime(
                f"{date_part} {year} {start_time}", "%B %d %Y %I:%M%p"
            ).replace(tzinfo=_ET_OFFSET)

            end_dt = datetime.strptime(
                f"{date_part} {year} {end_time}", "%B %d %Y %I:%M%p"
            ).replace(tzinfo=_ET_OFFSET)

            # Handle midnight crossing (e.g., 11:45PM - 12:00AM)
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)

            return start_dt.timestamp(), end_dt.timestamp()
        except Exception:
            return 0.0, 0.0

    def _parse_market(self, m: dict, now: float) -> Optional[MarketInfo]:
        """Parse a single market dict from the Gamma API into MarketInfo.
        Only accepts 15-minute Bitcoin Up or Down markets."""
        question = (m.get("question") or "")
        q_lower = question.lower()
        if "bitcoin" not in q_lower and "btc" not in q_lower:
            return None
        # Only target "up or down" momentum markets
        if "up or down" not in q_lower:
            return None

        # Token IDs and outcomes can be JSON strings
        token_ids = m.get("clobTokenIds") or m.get("tokens") or []
        outcomes = m.get("outcomes") or []

        if isinstance(token_ids, str):
            try:
                token_ids = json.loads(token_ids)
            except Exception:
                return None

        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                return None

        if len(token_ids) < 2 or len(outcomes) < 2:
            return None

        # Parse timing — prefer extracting from question title
        start_ts, end_ts = self._parse_time_range(question)

        if end_ts == 0:
            # Fallback to endDateIso (less precise, date-only for short-term)
            end_str = (
                m.get("endDateIso") or m.get("endDate")
                or m.get("end_date_iso") or ""
            )
            if not end_str:
                return None
            try:
                end_ts = datetime.fromisoformat(
                    end_str.replace("Z", "+00:00")
                ).timestamp()
            except Exception:
                return None

        if end_ts < now:
            return None

        # Only accept 15-minute duration markets
        duration = end_ts - start_ts
        if start_ts > 0 and not (800 < duration < 1000):
            # 15 min = 900 seconds, allow small margin
            return None

        # Map outcomes to token IDs: Up/Yes → yes_token, Down/No → no_token
        yes_token = ""
        no_token = ""
        for i, out in enumerate(outcomes):
            out_lower = out.lower() if isinstance(out, str) else ""
            if out_lower in ("yes", "up") and i < len(token_ids):
                yes_token = str(token_ids[i])
            elif out_lower in ("no", "down") and i < len(token_ids):
                no_token = str(token_ids[i])

        if not yes_token or not no_token:
            return None

        return MarketInfo(
            condition_id=m.get("conditionId") or m.get("condition_id") or "",
            question=question,
            yes_token_id=yes_token,
            no_token_id=no_token,
            end_time=end_ts,
            start_time=start_ts,
            active=True,
            neg_risk=bool(m.get("neg_risk") or m.get("negRisk")),
            tick_size=str(
                m.get("minimum_tick_size")
                or m.get("minimumTickSize")
                or m.get("orderPriceMinTickSize")
                or "0.01"
            ),
        )

    # ── orderbook / prices ────────────────────────────────────────────

    async def get_orderbook(self, token_id: str) -> dict:
        """Fetch orderbook: {bids: [{price, size}, ...], asks: [...]}."""
        if not self._client:
            return {"bids": [], "asks": []}
        try:
            resp = await self._client.get(
                f"{CLOB_API}/book",
                params={"token_id": token_id},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.debug(f"Book fetch error ({token_id[:12]}): {exc}")
            return {"bids": [], "asks": []}

    async def get_midpoint(self, token_id: str) -> Optional[float]:
        """Fetch midpoint from CLOB API."""
        if not self._client:
            return None
        try:
            resp = await self._client.get(
                f"{CLOB_API}/midpoint",
                params={"token_id": token_id},
            )
            resp.raise_for_status()
            data = resp.json()
            mid = data.get("mid")
            return float(mid) if mid else None
        except Exception as exc:
            logger.debug(f"Midpoint fetch error ({token_id[:12]}): {exc}")
            return None
