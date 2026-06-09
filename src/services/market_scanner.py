"""Market scanner — discovers tradeable epics from the IG API.

Instead of a hardcoded epic list, this service dynamically fetches markets
by running sequential searches for configured terms (forex pairs, index names,
commodity names), then combining the results with any user watchlists.

All discovery calls are intentionally sequential with a configurable delay
between each one (``inter_call_delay``) to avoid exhausting the IG per-minute
quota during bulk scans.  The coroutines still use async/await throughout so
the event loop is never blocked during sleeps or HTTP calls.

Typical usage:
    scanner = MarketScanner(client, settings)
    epics = await scanner.get_tradeable_epics()

IG API endpoints used:
- GET /markets?searchTerm=X  (v1) — term-based market search
- GET /watchlists             (v1) — user watchlist enumeration
- GET /watchlists/{id}        (v1) — watchlist contents
- GET /markets?epics=X,Y,...  (v2) — batch market details (max 50 per call)
"""

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import quote

from src.api.client import IGAPIError, IGClient
from src.config import Settings

logger = logging.getLogger(__name__)

# IG's batch /markets endpoint fails the WHOLE batch with HTTP 500
# "Transformation failure" if a single epic is unresolvable (expired future,
# option chain, unsupported instrument). Batch size only changes how many good
# epics a single bad one takes down — _fetch_batch bisects on 500 to isolate it.
_BATCH_SIZE = 25

# Product-type segments that appear in the 4th position of IG epic codes
# (e.g. CS.D.EURGBP.**CFD**.IP vs CS.D.EURGBP.**MINI**.IP).
# Order defines preference when multiple variants of the same market are found.
_PRODUCT_PREFERENCE: list[str] = ["CFD", "MINI", "DAILY", "DFB", "WEEKLY"]
_PRODUCT_SEGMENTS: frozenset[str] = frozenset(_PRODUCT_PREFERENCE)


@dataclass
class MarketInfo:
    """Minimal tradeable market descriptor."""

    epic: str
    name: str
    bid: float
    offer: float
    spread_ratio: float
    dealing_enabled: bool
    status: str


@dataclass
class MarketScanner:
    """Discovers and filters tradeable markets from the IG API.

    Args:
        client: Authenticated IG client.
        settings: Application settings (spread threshold, search terms).
        max_spread_ratio: Override the spread filter (default: from settings).
        inter_call_delay: Seconds to sleep between consecutive discovery API
            calls.  Keeps burst rate well below the IG per-minute quota.
            Set to 0 to disable (useful in unit tests with mocked HTTP).
    """

    client: IGClient
    settings: Settings
    max_spread_ratio: float | None = None
    inter_call_delay: float = 1.0

    def __post_init__(self) -> None:
        if self.max_spread_ratio is None:
            self.max_spread_ratio = self.settings.strategy_max_spread_ratio
        # Epics that caused HTTP 500 "Transformation failure" when fetched alone.
        # Cached across calls to avoid re-probing known-bad instruments each scan.
        self._poison_epics: set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_tradeable_epics(self) -> list[str]:
        """Return a deduplicated list of epic codes, one per underlying market.

        Combines term-based search results with user watchlists sequentially,
        deduplicates exact epic strings, then collapses product-type variants
        (CFD/MINI/DAILY/DFB) that map to the same underlying market into a
        single representative epic (preferring CFD > MINI > DAILY > DFB).
        """
        search_epics = await self._epics_from_search()
        wl_epics = await self._epics_from_watchlists()
        candidates = search_epics + wl_epics

        if not candidates:
            logger.warning("MarketScanner: no candidate epics found.")
            return []

        seen: set[str] = set()
        unique = [e for e in candidates if not (e in seen or seen.add(e))]  # type: ignore[func-returns-value]

        market_unique = self._deduplicate_by_market(unique)
        logger.info(
            "MarketScanner: %d unique markets discovered "
            "(%d from search, %d from watchlists, %d product-type duplicates removed)",
            len(market_unique),
            len(search_epics),
            len(wl_epics),
            len(unique) - len(market_unique),
        )
        return market_unique

    async def get_tradeable_markets(self, epics: list[str]) -> list[MarketInfo]:
        """Fetch market details and keep only currently open/TRADEABLE markets.

        No spread filtering is applied here — the spread is checked later at
        analysis time (``compute_signal``) against the live price buffer, so a
        momentarily wide spread no longer drops an epic from tracking for a whole
        hour.
        """
        markets = await self._fetch_market_details(epics)
        return self._filter_open_tradeable(markets)

    # ------------------------------------------------------------------
    # Market-level deduplication
    # ------------------------------------------------------------------

    @staticmethod
    def _market_base_key(epic: str) -> str:
        """Strip product-type segments to get the underlying market key.

        CS.D.EURGBP.CFD.IP  →  CS.D.EURGBP.IP
        CS.D.EURGBP.MINI.IP →  CS.D.EURGBP.IP
        """
        return ".".join(p for p in epic.split(".") if p not in _PRODUCT_SEGMENTS)

    def _deduplicate_by_market(self, epics: list[str]) -> list[str]:
        """Return one epic per underlying market, preserving discovery order.

        Groups epics that share the same market base key (i.e. differ only in
        their product-type segment such as CFD vs MINI) and selects the preferred
        variant according to ``_PRODUCT_PREFERENCE``.  Epics whose key does not
        contain any known product-type segment are kept as-is.
        """
        groups: dict[str, list[str]] = {}
        for epic in epics:
            key = self._market_base_key(epic)
            groups.setdefault(key, []).append(epic)

        result: list[str] = []
        for key, group in groups.items():
            if len(group) == 1:
                result.append(group[0])
                continue
            chosen = group[0]
            for preferred in _PRODUCT_PREFERENCE:
                candidate = next((e for e in group if preferred in e.split(".")), None)
                if candidate is not None:
                    chosen = candidate
                    break
            logger.debug(
                "MarketScanner: market %s — kept %s, dropped: %s",
                key,
                chosen,
                ", ".join(e for e in group if e != chosen),
            )
            result.append(chosen)

        return result

    # ------------------------------------------------------------------
    # Search-based discovery
    # ------------------------------------------------------------------

    async def _epics_from_search(self) -> list[str]:
        """Search all configured terms sequentially, sleeping between calls.

        Each term hits GET /markets?searchTerm=X (v1). The ``inter_call_delay``
        sleep between terms keeps the burst rate well below the IG per-minute
        quota.  asyncio.sleep() yields to the event loop so other tasks are not
        blocked during the wait.
        """
        terms = self.settings.scanner_search_terms
        if not terms:
            return []

        epics: list[str] = []
        ok = 0
        for i, term in enumerate(terms):
            if i > 0 and self.inter_call_delay > 0:
                await asyncio.sleep(self.inter_call_delay)
            try:
                result = await self._search_term(term)
                epics.extend(result)
                ok += 1
            except Exception as exc:
                logger.warning("MarketScanner: search error for '%s': %s", term, exc)

        logger.info(
            "MarketScanner: search discovery — %d epics across %d/%d terms",
            len(epics),
            ok,
            len(terms),
        )
        return epics

    async def _search_term(self, term: str) -> list[str]:
        """Search markets for a single term and return epic codes.

        IG search results include option-chain epics (OPTCALL/OPTPUT segments)
        which cannot be fetched via the batch /markets?epics= endpoint — IG
        returns HTTP 500 "Transformation failure" for them. Filter them out here
        before they reach the batch pipeline.
        """
        data = await self.client.get(
            f"/markets?searchTerm={quote(term, safe='')}", version=1
        )
        epics = []
        for m in data.get("markets", []):
            epic = m.get("epic", "")
            if not epic:
                continue
            if self._is_option_epic(epic):
                logger.debug("MarketScanner: skipping option-chain epic %s", epic)
                continue
            epics.append(epic)
        return epics

    @staticmethod
    def _is_option_epic(epic: str) -> bool:
        """Return True if the epic is an option chain instrument.

        Option chain epics contain an 'OPT'-prefixed segment (e.g. OPTCALL,
        OPTPUT) and cannot be fetched via the batch /markets?epics= endpoint.
        """
        return any(seg.startswith("OPT") for seg in epic.split("."))

    # ------------------------------------------------------------------
    # Watchlist discovery (complementary source)
    # ------------------------------------------------------------------

    async def _epics_from_watchlists(self) -> list[str]:
        """Collect all epics from every user watchlist sequentially."""
        epics: list[str] = []
        try:
            watchlists_data = await self.client.get("/watchlists", version=1)
            watchlists = watchlists_data.get("watchlists", [])
            logger.info("MarketScanner: found %d watchlists", len(watchlists))

            valid = [wl for wl in watchlists if wl.get("id")]
            for i, wl in enumerate(valid):
                if i > 0 and self.inter_call_delay > 0:
                    await asyncio.sleep(self.inter_call_delay)
                try:
                    result = await self._fetch_watchlist_epics(wl["id"])
                    epics.extend(result)
                except Exception as exc:
                    logger.warning("MarketScanner: watchlist fetch error: %s", exc)

        except Exception as exc:
            logger.error("MarketScanner: failed to fetch watchlists: %s", exc)

        return epics

    async def _fetch_watchlist_epics(self, watchlist_id: str) -> list[str]:
        """Fetch epic codes from a single watchlist, excluding option chains."""
        data = await self.client.get(f"/watchlists/{watchlist_id}", version=1)
        epics = []
        for m in data.get("markets", []):
            epic = m.get("epic", "")
            if not epic:
                continue
            if self._is_option_epic(epic):
                logger.debug("MarketScanner: skipping option-chain epic %s", epic)
                continue
            epics.append(epic)
        return epics

    # ------------------------------------------------------------------
    # Market details + filtering
    # ------------------------------------------------------------------

    async def _fetch_market_details(self, epics: list[str]) -> list[dict]:
        """Batch-fetch market details sequentially in chunks of 25."""
        # Skip epics already known to cause HTTP 500 "Transformation failure".
        skipped = [e for e in epics if e in self._poison_epics]
        if skipped:
            logger.debug(
                "MarketScanner: skipping %d known-poison epic(s): %s",
                len(skipped),
                ", ".join(skipped),
            )
        filtered = [e for e in epics if e not in self._poison_epics]

        all_details: list[dict] = []
        batches = [
            filtered[i : i + _BATCH_SIZE] for i in range(0, len(filtered), _BATCH_SIZE)
        ]
        for batch in batches:
            all_details.extend(await self._fetch_batch(batch))
        return all_details

    async def _fetch_batch(self, epics: list[str]) -> list[dict]:
        """Fetch market details for a batch of epics, isolating poison epics.

        IG fails the entire batch with HTTP 500 "Transformation failure" when a
        single epic is unresolvable. On that error we bisect the batch and retry
        each half, recursing down to single-epic granularity so only the genuine
        offenders are dropped and every valid epic is still returned.
        """
        if not epics:
            return []
        epics_str = ",".join(epics)
        try:
            data = await self.client.get(
                f"/markets?epics={epics_str}",
                version=1,
                suppress_error_logging=True,
            )
            return data.get("marketDetails", [])
        except IGAPIError as exc:
            if not self._is_poison_batch_error(exc):
                logger.warning(
                    "MarketScanner: batch of %d epics failed — %s", len(epics), exc
                )
                return []
            if len(epics) == 1:
                self._poison_epics.add(epics[0])
                logger.warning(
                    "MarketScanner: dropping unresolvable epic %s — %s (cached, won't retry)",
                    epics[0],
                    exc,
                )
                return []
            mid = len(epics) // 2
            logger.debug(
                "MarketScanner: bisecting failed batch of %d epics to isolate bad epic",
                len(epics),
            )
            left = await self._fetch_batch(epics[:mid])
            right = await self._fetch_batch(epics[mid:])
            return left + right
        except Exception as exc:
            logger.warning(
                "MarketScanner: batch of %d epics failed — %s", len(epics), exc
            )
            return []

    @staticmethod
    def _is_poison_batch_error(exc: IGAPIError) -> bool:
        """True if bisecting the batch can isolate the failure.

        IG returns HTTP 500 for a "Transformation failure" caused by one bad
        epic. Other errors (auth, rate-limit) won't be fixed by splitting, so we
        don't amplify the request count for them.
        """
        return exc.response is not None and exc.response.status_code == 500

    def _filter_dealing_enabled(self, market_details: list[dict]) -> list[MarketInfo]:
        """Keep only markets where dealing is enabled.

        Neither spread NOR marketStatus are checked here — this is for
        startup discovery. Markets may show as CLOSED pre-market but become
        TRADEABLE once the session opens.
        """
        result: list[MarketInfo] = []
        for detail in market_details:
            info = self._parse_market(detail)
            if info is None:
                continue
            if not info.dealing_enabled:
                logger.debug("MarketScanner: skip %s — dealing disabled", info.epic)
                continue
            logger.debug(
                "MarketScanner: accept %s (status=%s, spread=%.5f)",
                info.epic,
                info.status,
                info.spread_ratio,
            )
            result.append(info)
        return result

    def _filter_open_tradeable(self, market_details: list[dict]) -> list[MarketInfo]:
        """Keep only markets that are currently open and tradeable.

        Mirrors the PHP trade-time check (apiGetMarketAndPostOpenClose.php) minus
        the spread test:
        - marketStatus == "TRADEABLE"
        - bid and offer both present (> 0)

        The spread is intentionally NOT checked here — it is evaluated later at
        analysis time against the live price buffer. Logs a breakdown so an empty
        result is diagnosable.
        """
        result: list[MarketInfo] = []
        unparseable = 0
        no_price = 0
        status_counts: dict[str, int] = {}
        for detail in market_details:
            info = self._parse_market(detail)
            if info is None:
                unparseable += 1
                continue
            if info.status != "TRADEABLE":
                status_counts[info.status] = status_counts.get(info.status, 0) + 1
                continue
            if info.bid <= 0 or info.offer <= 0:
                no_price += 1
                continue
            result.append(info)

        if len(result) < len(market_details):
            non_tradeable = ", ".join(
                f"{status}={count}" for status, count in sorted(status_counts.items())
            )
            logger.info(
                "MarketScanner: filter — %d/%d open/tradeable "
                "(rejected: %d non-tradeable [%s], %d no-price, %d unparseable)",
                len(result),
                len(market_details),
                sum(status_counts.values()),
                non_tradeable or "none",
                no_price,
                unparseable,
            )
        return result

    @staticmethod
    def _parse_market(detail: dict) -> MarketInfo | None:
        """Extract key fields from a /markets response item."""
        try:
            instrument = detail.get("instrument", {})
            snapshot = detail.get("snapshot", {})

            epic = instrument.get("epic", "")
            name = instrument.get("name", "")
            bid = float(snapshot.get("bid") or 0)
            offer = float(snapshot.get("offer") or 0)
            status = snapshot.get("marketStatus", "CLOSED")

            mid = (bid + offer) / 2 if (bid + offer) > 0 else 1
            spread_ratio = (offer - bid) / mid if mid > 0 else 999.0

            # IG does not expose a plain `dealingEnabled` boolean in the
            # batch /markets response. PHP used forceOpenAllowed +
            # stopsLimitsAllowed as the proxy — match that here.
            dealing_enabled = bool(
                instrument.get("forceOpenAllowed")
                and instrument.get("stopsLimitsAllowed")
            )

            return MarketInfo(
                epic=epic,
                name=name,
                bid=bid,
                offer=offer,
                spread_ratio=spread_ratio,
                dealing_enabled=dealing_enabled,
                status=status,
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("MarketScanner: failed to parse market detail: %s", exc)
            return None
