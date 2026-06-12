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
from src.utils.tools import funds_needed_for_one_buy, stop_loss_eur_for_one_buy

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
    # IG instrument class (e.g. CURRENCIES, INDICES, COMMODITIES, SHARES).
    # Empty when the /markets payload omits it. Used by the asset-class filter
    # to keep shares (gold miners, etc. with .CASH epics) out of the universe.
    instrument_type: str = ""
    # Estimated margin (EUR) to open one minimum-size BUY, or None when the
    # /markets payload lacks the margin/contract/price data to compute it.
    funds_needed: float | None = None
    # Estimated EUR loss if that minimum-size BUY is stopped out at IG's minimum
    # stop distance, or None when the contract/price/stop-rule data is missing.
    stop_loss_eur: float | None = None


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

        Combines term-based search results with user watchlists, keeping only
        the configured asset classes (``scanner_allowed_instrument_types``) using
        the ``instrumentType`` field present on every search/watchlist result —
        so off-class instruments (chiefly SHARES with ``.CASH`` epics surfaced by
        broad commodity/index terms) are dropped at the source, before any market
        detail is fetched.

        Exact-epic duplicates are removed, then product-type variants
        (CFD/MINI/DAILY/DFB) that map to the same underlying market collapse to a
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

    async def get_all_market_infos(self, epics: list[str]) -> list[MarketInfo]:
        """Fetch and parse market details for every epic.

        Returns one ``MarketInfo`` per successfully-parsed market — including
        closed/non-tradeable ones — so callers can enrich the full epic list
        (name, funds needed) before narrowing it down to the tradable subset.

        The only filtering applied here is by asset class: instruments whose
        ``type`` is known and not in ``settings.scanner_allowed_instrument_types``
        (typically SHARES surfaced by broad commodity/index search terms, with
        ``.CASH`` epics) are dropped so they never reach the Epic List or the
        tradable set. Markets with a blank/unknown type are kept.
        """
        details = await self._fetch_market_details(epics)
        infos: list[MarketInfo] = []
        for detail in details:
            info = self._parse_market(detail)
            if info is not None:
                infos.append(info)
        return self._filter_instrument_type(infos)

    def _filter_instrument_type(self, infos: list[MarketInfo]) -> list[MarketInfo]:
        """Drop markets whose asset class is not in the configured allow-list.

        Keeps only the instrument types listed in
        ``settings.scanner_allowed_instrument_types`` (e.g. CURRENCIES, INDICES,
        COMMODITIES). A market with a blank/unknown type is kept — we can't prove
        it's out of scope, mirroring ``_filter_affordable``'s treatment of
        unknown funds. Logs a per-type breakdown so an unexpected drop is
        diagnosable.
        """
        if not self.settings.scanner_allowed_instrument_types:
            return infos
        result: list[MarketInfo] = []
        dropped_counts: dict[str, int] = {}
        for info in infos:
            if not self._type_allowed(info.instrument_type):
                dropped_counts[info.instrument_type.upper()] = (
                    dropped_counts.get(info.instrument_type.upper(), 0) + 1
                )
                continue
            result.append(info)
        if dropped_counts:
            breakdown = ", ".join(
                f"{kind}={count}" for kind, count in sorted(dropped_counts.items())
            )
            logger.info(
                "MarketScanner: dropped %d off-class epic(s) [%s] — keeping %s",
                sum(dropped_counts.values()),
                breakdown,
                ", ".join(self.settings.scanner_allowed_instrument_types),
            )
        return result

    async def get_tradeable_markets(self, epics: list[str]) -> list[MarketInfo]:
        """Fetch market details and keep only the currently tradable subset.

        Convenience wrapper around ``get_all_market_infos`` + ``select_tradable``
        for callers that don't need the full (unfiltered) market list.
        """
        infos = await self.get_all_market_infos(epics)
        return self.select_tradable(infos)

    def select_tradable(self, infos: list[MarketInfo]) -> list[MarketInfo]:
        """Narrow parsed markets down to the tradable subscription set.

        Three stages, in order:
        1. Keep only currently open/TRADEABLE markets with a live price
           (``_filter_open_tradeable``). No spread filtering — the spread is
           checked later at analysis time against the live price buffer.
        2. Drop markets too expensive to ever open (funds needed for one BUY
           above ``max_funds_per_position``). Markets with unknown funds are
           kept, since we can't prove they're unaffordable.
        3. Deduplicate by underlying market, keeping the tightest-spread variant
           (e.g. IX.D.DAX.IFMM.IP vs IX.D.DAX.IMF.IP → one DAX subscription).
        """
        open_tradeable = self._filter_open_tradeable(infos)
        affordable = self._filter_affordable(open_tradeable)
        return self._dedupe_markets(affordable)

    @staticmethod
    def select_diversified_subset(
        markets: list[MarketInfo], cap: int
    ) -> list[MarketInfo]:
        """Pick at most ``cap`` markets balanced across asset classes.

        IG caps Lightstreamer at a fixed number of subscriptions, so when more
        markets are tradable than fit, a subset must be chosen. Taking the
        globally tightest spreads tends to fill every slot with one asset class
        (FX pairs are far tighter than commodities), starving indices and
        commodities. Instead this groups markets by ``instrument_type`` and picks
        round-robin — the tightest-spread market of each class first, then the
        second of each, and so on — so the result keeps the best market of every
        class while still preferring tight spreads within each.

        Classes that run out simply drop out of the rotation, their unused slots
        going to the classes that still have markets. Markets with a blank type
        are grouped under one bucket so they still participate. Returns the input
        unchanged (as a list) when it already fits under ``cap``.
        """
        if cap <= 0 or len(markets) <= cap:
            return list(markets)

        buckets: dict[str, list[MarketInfo]] = {}
        for m in markets:
            key = m.instrument_type.upper() or "OTHER"
            buckets.setdefault(key, []).append(m)
        for bucket in buckets.values():
            bucket.sort(key=lambda m: m.spread_ratio)

        # Stable class order keeps the selection deterministic across refreshes.
        order = sorted(buckets)
        cursors = {key: 0 for key in order}
        chosen: list[MarketInfo] = []
        while len(chosen) < cap:
            progressed = False
            for key in order:
                if len(chosen) >= cap:
                    break
                cursor = cursors[key]
                if cursor < len(buckets[key]):
                    chosen.append(buckets[key][cursor])
                    cursors[key] += 1
                    progressed = True
            if not progressed:
                break
        return chosen

    def get_non_tradable_reasons(
        self,
        all_infos: list[MarketInfo],
        tradable: list[MarketInfo],
    ) -> dict[str, str]:
        """Return {epic: reason} for every epic in all_infos excluded from tradable.

        Reasons (in priority order):
        - The IG marketStatus value (e.g. "CLOSED", "OFFLINE") when not TRADEABLE
        - "no_price" when bid or offer is missing
        - "too_expensive" when funds_needed exceeds the configured cap
        - "duplicate" when deduped as a product-type variant of a kept epic
        """
        tradable_set = {m.epic for m in tradable}
        cap = self.settings.max_funds_per_position

        reasons: dict[str, str] = {}
        for info in all_infos:
            if info.epic in tradable_set:
                continue
            if info.status != "TRADEABLE":
                reasons[info.epic] = info.status or "CLOSED"
            elif info.bid <= 0 or info.offer <= 0:
                reasons[info.epic] = "no_price"
            elif (
                cap > 0
                and info.funds_needed is not None
                and info.funds_needed > cap
            ):
                reasons[info.epic] = "too_expensive"
            else:
                reasons[info.epic] = "duplicate"
        return reasons

    # ------------------------------------------------------------------
    # Market-level deduplication
    # ------------------------------------------------------------------

    @staticmethod
    def _market_base_key(epic: str) -> str:
        """Strip the product-type segment to get the underlying market key.

        CS.D.EURGBP.CFD.IP  →  CS.D.EURGBP.IP
        CS.D.EURGBP.MINI.IP →  CS.D.EURGBP.IP
        IX.D.DAX.IFMM.IP    →  IX.D.DAX.IP
        IX.D.DAX.IMF.IP     →  IX.D.DAX.IP

        IG epics follow ``A.B.NAME.PRODUCT.SUFFIX`` (5 segments) where the 4th
        segment is the product type. For that canonical shape we drop the 4th
        segment generically so variants the fixed ``_PRODUCT_SEGMENTS`` list
        doesn't know about (IFMM/IMF/… for indices) still collapse together.
        Non-canonical epics fall back to stripping only known product segments.
        """
        parts = epic.split(".")
        if len(parts) == 5:
            return ".".join(parts[:3] + parts[4:])
        return ".".join(p for p in parts if p not in _PRODUCT_SEGMENTS)

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

    def _type_allowed(self, instrument_type: str | None) -> bool:
        """Return True if an instrument's asset class passes the allow-list.

        An empty allow-list or a missing/blank type is treated as allowed — we
        can't prove a typeless result is out of scope, and the authoritative
        filter re-runs later on the fetched market details.
        """
        allowed = {t.upper() for t in self.settings.scanner_allowed_instrument_types}
        if not allowed or not instrument_type:
            return True
        return instrument_type.upper() in allowed

    # ------------------------------------------------------------------
    # Search-based discovery
    # ------------------------------------------------------------------

    async def _epics_from_search(self) -> list[str]:
        """Submit all search-term calls concurrently and collect results.

        Each term hits GET /markets?searchTerm=X (v1). All calls are enqueued
        at once so the APIQueue/APIGuard fills up and drains visibly in the UI;
        rate-limit serialisation is handled by the guard, not by manual sleeps.
        """
        terms = self.settings.scanner_search_terms
        if not terms:
            return []

        results = await asyncio.gather(
            *[self._search_term(term) for term in terms],
            return_exceptions=True,
        )
        epics: list[str] = []
        ok = 0
        for term, result in zip(terms, results):
            if isinstance(result, BaseException):
                logger.warning("MarketScanner: search error for '%s': %s", term, result)
            else:
                epics.extend(result)
                ok += 1

        logger.info(
            "MarketScanner: search discovery — %d epics across %d/%d terms",
            len(epics),
            ok,
            len(terms),
        )
        return epics

    async def _search_term(self, term: str) -> list[str]:
        """Search markets for a single term and return keepable epic codes.

        Broad terms (e.g. "USD", "Oil") return many off-class instruments —
        SHARES, RATES, etc. — alongside the wanted markets. Each result carries
        an ``instrumentType``, so the asset-class allow-list is applied here to
        drop them at the source. Option-chain epics (OPTCALL/OPTPUT segments) are
        also skipped: they cannot be fetched via the batch /markets?epics=
        endpoint (IG returns HTTP 500 "Transformation failure") and would poison
        the batch pipeline.
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
            if not self._type_allowed(m.get("instrumentType")):
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
        """Collect all epics from every user watchlist concurrently.

        First fetches the watchlist index (one call), then enqueues all
        individual watchlist fetches at once so the queue fills visibly.
        """
        try:
            watchlists_data = await self.client.get("/watchlists", version=1)
        except Exception as exc:
            logger.error("MarketScanner: failed to fetch watchlists: %s", exc)
            return []

        watchlists = watchlists_data.get("watchlists", [])
        logger.info("MarketScanner: found %d watchlists", len(watchlists))

        valid = [wl for wl in watchlists if wl.get("id")]
        if not valid:
            return []

        results = await asyncio.gather(
            *[self._fetch_watchlist_epics(wl["id"]) for wl in valid],
            return_exceptions=True,
        )
        epics: list[str] = []
        for wl, result in zip(valid, results):
            if isinstance(result, BaseException):
                logger.warning("MarketScanner: watchlist fetch error: %s", result)
            else:
                epics.extend(result)
        return epics

    async def _fetch_watchlist_epics(self, watchlist_id: str) -> list[str]:
        """Fetch epic codes from a single watchlist.

        Excludes option chains and off-class instruments (via the asset-class
        allow-list on each result's ``instrumentType``), consistent with search
        discovery.
        """
        data = await self.client.get(f"/watchlists/{watchlist_id}", version=1)
        epics = []
        for m in data.get("markets", []):
            epic = m.get("epic", "")
            if not epic:
                continue
            if self._is_option_epic(epic):
                logger.debug("MarketScanner: skipping option-chain epic %s", epic)
                continue
            if not self._type_allowed(m.get("instrumentType")):
                continue
            epics.append(epic)
        return epics

    # ------------------------------------------------------------------
    # Market details + filtering
    # ------------------------------------------------------------------

    async def _fetch_market_details(self, epics: list[str]) -> list[dict]:
        """Batch-fetch market details for all epics concurrently in chunks of 25.

        All batches are enqueued at once — the APIGuard serialises actual HTTP
        calls so the per-minute quota is respected while the queue counter fills
        and drains visibly in the UI.
        """
        skipped = [e for e in epics if e in self._poison_epics]
        if skipped:
            logger.debug(
                "MarketScanner: skipping %d known-poison epic(s): %s",
                len(skipped),
                ", ".join(skipped),
            )
        filtered = [e for e in epics if e not in self._poison_epics]

        batches = [
            filtered[i : i + _BATCH_SIZE] for i in range(0, len(filtered), _BATCH_SIZE)
        ]
        results = await asyncio.gather(
            *[self._fetch_batch(batch) for batch in batches],
        )
        return [detail for batch_result in results for detail in batch_result]

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
                    "MarketScanner: dropping unresolvable epic %s — %s "
                    "(cached, won't retry)",
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

    def _filter_open_tradeable(self, infos: list[MarketInfo]) -> list[MarketInfo]:
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
        no_price = 0
        status_counts: dict[str, int] = {}
        for info in infos:
            if info.status != "TRADEABLE":
                status_counts[info.status] = status_counts.get(info.status, 0) + 1
                continue
            if info.bid <= 0 or info.offer <= 0:
                no_price += 1
                continue
            result.append(info)

        if len(result) < len(infos):
            non_tradeable = ", ".join(
                f"{status}={count}" for status, count in sorted(status_counts.items())
            )
            logger.info(
                "MarketScanner: filter — %d/%d open/tradeable "
                "(rejected: %d non-tradeable [%s], %d no-price)",
                len(result),
                len(infos),
                sum(status_counts.values()),
                non_tradeable or "none",
                no_price,
            )
        return result

    def _filter_affordable(self, infos: list[MarketInfo]) -> list[MarketInfo]:
        """Drop markets too expensive to ever open one minimum-size BUY.

        Removes markets whose ``funds_needed`` exceeds
        ``settings.max_funds_per_position``. Markets with an unknown
        ``funds_needed`` (None) are kept — we can't prove they're unaffordable,
        and dropping them silently would hide tradable markets.
        """
        cap = self.settings.max_funds_per_position
        if cap <= 0:
            return infos
        result = [
            info
            for info in infos
            if info.funds_needed is None or info.funds_needed <= cap
        ]
        dropped = len(infos) - len(result)
        if dropped:
            logger.info(
                "MarketScanner: dropped %d epic(s) needing > %.0f€ to open one BUY",
                dropped,
                cap,
            )
        return result

    def _dedupe_markets(self, infos: list[MarketInfo]) -> list[MarketInfo]:
        """Collapse product variants of the same underlying to one market.

        Groups by ``_market_base_key`` (which ignores the product-type segment)
        and keeps the tightest-spread variant per group — so the same price curve
        is never analyzed twice (e.g. IX.D.DAX.IFMM.IP and IX.D.DAX.IMF.IP yield a
        single DAX subscription).
        """
        groups: dict[str, MarketInfo] = {}
        dropped = 0
        for info in infos:
            key = self._market_base_key(info.epic)
            best = groups.get(key)
            if best is None:
                groups[key] = info
            else:
                dropped += 1
                if info.spread_ratio < best.spread_ratio:
                    groups[key] = info
        if dropped:
            logger.info(
                "MarketScanner: collapsed %d product-variant duplicate(s) "
                "(kept tightest spread per market)",
                dropped,
            )
        return list(groups.values())

    @staticmethod
    def _parse_market(detail: dict) -> MarketInfo | None:
        """Extract key fields from a /markets response item."""
        try:
            instrument = detail.get("instrument", {})
            snapshot = detail.get("snapshot", {})

            epic = instrument.get("epic", "")
            # The instrument name lives under ``instrument.name`` in the rich
            # /markets payload, but some endpoint versions surface it as
            # ``instrumentName`` (search-style) instead — fall back so the Epic
            # List "Name" column is never blank when a name is available.
            name = (
                instrument.get("name")
                or instrument.get("marketName")
                or detail.get("instrumentName")
                or ""
            )
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
                instrument_type=str(instrument.get("type") or ""),
                funds_needed=funds_needed_for_one_buy(detail),
                stop_loss_eur=stop_loss_eur_for_one_buy(detail),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("MarketScanner: failed to parse market detail: %s", exc)
            return None
