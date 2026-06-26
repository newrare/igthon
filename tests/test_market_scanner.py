"""Tests for the MarketScanner service."""

from unittest.mock import AsyncMock, MagicMock
from urllib.parse import unquote

import httpx
import pytest

from src.core.api.client import IGAPIError
from src.markets.market_scanner import MarketInfo, MarketScanner


def _market_info(epic: str, instrument_type: str, spread_ratio: float) -> MarketInfo:
    """Build a minimal MarketInfo for subset-selection tests."""
    return MarketInfo(
        epic=epic,
        name=epic,
        bid=1.0,
        offer=1.0,
        spread_ratio=spread_ratio,
        dealing_enabled=True,
        status="TRADEABLE",
        instrument_type=instrument_type,
    )


def _ig_error(epics_str: str, status_code: int, ig_error_code: str = "") -> IGAPIError:
    """Build an IGAPIError mimicking a failed batch /markets call."""
    request = httpx.Request("GET", f"https://demo-api.ig.com/markets?epics={epics_str}")
    response = httpx.Response(status_code, request=request)
    return IGAPIError(
        "boom", request=request, response=response, ig_error_code=ig_error_code
    )


def _make_settings(
    max_spread: float = 0.002,
    search_terms: list[str] | None = None,
    max_funds: float = 0.0,
    allowed_types: list[str] | None = None,
) -> MagicMock:
    s = MagicMock()
    s.strategy_max_spread_ratio = max_spread
    s.scanner_search_terms = search_terms if search_terms is not None else []
    # 0 disables the funds filter — most tests don't supply margin data.
    s.max_funds_per_position = max_funds
    # Empty list disables the asset-class filter — most tests omit instrument
    # types, so the default keeps them all.
    s.scanner_allowed_instrument_types = (
        allowed_types if allowed_types is not None else []
    )
    return s


def _make_market_detail(
    epic: str,
    bid: float,
    offer: float,
    status: str = "TRADEABLE",
    force_open: bool = True,
    stops_limits: bool = True,
    margin_factor: float | None = None,
    contract_size: float = 1.0,
    min_deal: float = 1.0,
    instrument_type: str | None = None,
) -> dict:
    instrument: dict = {
        "epic": epic,
        "name": f"Market {epic}",
        "forceOpenAllowed": force_open,
        "stopsLimitsAllowed": stops_limits,
        "contractSize": contract_size,
        "currencies": [{"code": "EUR", "exchangeRate": 1.0, "isDefault": True}],
    }
    if instrument_type is not None:
        instrument["type"] = instrument_type
    if margin_factor is not None:
        instrument["marginFactor"] = margin_factor
        instrument["marginFactorUnit"] = "PERCENTAGE"
    return {
        "instrument": instrument,
        "snapshot": {"bid": bid, "offer": offer, "marketStatus": status},
        "dealingRules": {"minDealSize": {"value": min_deal}},
    }


def _make_client(
    search_results: dict[str, list[dict]] | None = None,
    watchlists: list[dict] | None = None,
    watchlist_markets: dict[str, list[dict]] | None = None,
) -> MagicMock:
    """Build a mock IGClient that dispatches get() calls by endpoint."""
    search_results = search_results or {}
    watchlists = watchlists or []
    watchlist_markets = watchlist_markets or {}

    async def mock_get(endpoint: str, *, version: int = 1) -> dict:
        if endpoint == "/watchlists":
            return {"watchlists": watchlists}
        if endpoint.startswith("/watchlists/"):
            wl_id = endpoint.split("/")[-1]
            return {"markets": watchlist_markets.get(wl_id, [])}
        if endpoint.startswith("/markets?searchTerm="):
            term = unquote(endpoint.split("=", 1)[1])
            return {"markets": search_results.get(term, [])}
        if endpoint.startswith("/markets?epics="):
            return {"marketDetails": []}
        return {}

    client = MagicMock()
    client.get = AsyncMock(side_effect=mock_get)
    return client


# ------------------------------------------------------------------
# Search-based discovery
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_discovers_epics_for_configured_terms() -> None:
    """Should return epics from all matching search results.

    Product variants of the same underlying market (TODAY vs CFD on EUR/USD)
    collapse to a single representative epic, preferring CFD.
    """
    client = _make_client(
        search_results={
            "EUR/USD": [
                {"epic": "CS.D.EURUSD.TODAY.IP"},
                {"epic": "CS.D.EURUSD.CFD.IP"},
            ],
            "Gold": [{"epic": "CS.D.GOLD.TODAY.IP"}],
        }
    )
    settings = _make_settings(search_terms=["EUR/USD", "Gold"])
    scanner = MarketScanner(client=client, settings=settings)

    epics = await scanner.get_tradeable_epics()

    # EUR/USD's two product variants collapse to one (CFD preferred).
    assert "CS.D.EURUSD.CFD.IP" in epics
    assert "CS.D.EURUSD.TODAY.IP" not in epics
    assert "CS.D.GOLD.TODAY.IP" in epics


@pytest.mark.asyncio
async def test_search_error_on_one_term_does_not_crash() -> None:
    """A failing search term should log a warning and not abort other terms."""

    async def mock_get(endpoint: str, *, version: int = 1) -> dict:
        if endpoint == "/markets?searchTerm=bad_term":
            raise RuntimeError("API error")
        if endpoint == "/markets?searchTerm=Gold":
            return {"markets": [{"epic": "CS.D.GOLD.TODAY.IP"}]}
        if endpoint == "/watchlists":
            return {"watchlists": []}
        return {}

    client = MagicMock()
    client.get = AsyncMock(side_effect=mock_get)
    settings = _make_settings(search_terms=["bad_term", "Gold"])
    scanner = MarketScanner(client=client, settings=settings)

    epics = await scanner.get_tradeable_epics()

    assert "CS.D.GOLD.TODAY.IP" in epics


@pytest.mark.asyncio
async def test_no_search_terms_returns_watchlists_only() -> None:
    """With empty search terms, only watchlist epics are returned."""
    client = _make_client(
        watchlists=[{"id": "w1"}],
        watchlist_markets={"w1": [{"epic": "WL.ONLY.EPIC"}]},
    )
    settings = _make_settings(search_terms=[])
    scanner = MarketScanner(client=client, settings=settings)

    epics = await scanner.get_tradeable_epics()

    assert "WL.ONLY.EPIC" in epics


# ------------------------------------------------------------------
# Watchlist discovery
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchlist_epics_are_included() -> None:
    """Epics from user watchlists are included alongside search results."""
    client = _make_client(
        search_results={"EUR/USD": [{"epic": "SEARCH.EPIC"}]},
        watchlists=[{"id": "w1"}],
        watchlist_markets={"w1": [{"epic": "WL.EPIC"}]},
    )
    settings = _make_settings(search_terms=["EUR/USD"])
    scanner = MarketScanner(client=client, settings=settings)

    epics = await scanner.get_tradeable_epics()

    assert "SEARCH.EPIC" in epics
    assert "WL.EPIC" in epics


@pytest.mark.asyncio
async def test_deduplication_across_search_and_watchlists() -> None:
    """Same epic from search and watchlist should appear only once."""
    client = _make_client(
        search_results={"Gold": [{"epic": "CS.D.GOLD.TODAY.IP"}]},
        watchlists=[{"id": "w1"}],
        watchlist_markets={"w1": [{"epic": "CS.D.GOLD.TODAY.IP"}]},
    )
    settings = _make_settings(search_terms=["Gold"])
    scanner = MarketScanner(client=client, settings=settings)

    epics = await scanner.get_tradeable_epics()

    assert epics.count("CS.D.GOLD.TODAY.IP") == 1


@pytest.mark.asyncio
async def test_fallback_when_all_sources_empty() -> None:
    """Returns empty list if both search and watchlists yield nothing."""
    client = _make_client()
    scanner = MarketScanner(client=client, settings=_make_settings())

    epics = await scanner.get_tradeable_epics()

    assert epics == []


@pytest.mark.asyncio
async def test_search_drops_off_class_results_by_instrument_type() -> None:
    """SHARES/RATES returned by broad terms are dropped at search time.

    The asset-class allow-list is applied using each search result's
    ``instrumentType``; a result with no type is kept (verified later on the
    fetched details).
    """
    client = _make_client(
        search_results={
            "Gold": [
                {"epic": "MT.D.GC.CFD.IP", "instrumentType": "COMMODITIES"},
                {"epic": "SD.D.BARRICK.CASH.IP", "instrumentType": "SHARES"},
                {"epic": "EUR.RATE.IP", "instrumentType": "RATES"},
                {"epic": "UNKNOWN.EPIC"},  # no type → kept
            ]
        }
    )
    settings = _make_settings(
        search_terms=["Gold"],
        allowed_types=["CURRENCIES", "INDICES", "COMMODITIES"],
    )
    scanner = MarketScanner(client=client, settings=settings)

    epics = await scanner.get_tradeable_epics()

    assert "MT.D.GC.CFD.IP" in epics
    assert "UNKNOWN.EPIC" in epics
    assert "SD.D.BARRICK.CASH.IP" not in epics  # SHARES dropped at source
    assert "EUR.RATE.IP" not in epics  # RATES dropped at source


# ------------------------------------------------------------------
# Trade-time filtering (get_tradeable_markets)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tradeable_markets_includes_wide_spread() -> None:
    """get_tradeable_markets should NOT filter on spread — wide spreads stay.

    The spread is checked later at analysis time, so a momentarily wide spread
    must not drop an otherwise tradeable epic from the list.
    """
    client = MagicMock()
    client.get = AsyncMock(
        return_value={
            "marketDetails": [
                _make_market_detail("WIDE.EPIC", bid=100.0, offer=105.0),
                _make_market_detail("TIGHT.EPIC", bid=18000.0, offer=18001.0),
            ]
        }
    )
    scanner = MarketScanner(client=client, settings=_make_settings(max_spread=0.002))

    result = await scanner.get_tradeable_markets(["WIDE.EPIC", "TIGHT.EPIC"])
    epics = [m.epic for m in result]

    assert "WIDE.EPIC" in epics
    assert "TIGHT.EPIC" in epics


@pytest.mark.asyncio
async def test_get_tradeable_markets_excludes_non_tradeable() -> None:
    """get_tradeable_markets should exclude non-TRADEABLE markets."""
    client = MagicMock()
    client.get = AsyncMock(
        return_value={
            "marketDetails": [
                _make_market_detail(
                    "CLOSED.EPIC", bid=100.0, offer=100.1, status="CLOSED"
                ),
                _make_market_detail(
                    "OPEN.EPIC", bid=100.0, offer=100.1, status="TRADEABLE"
                ),
            ]
        }
    )
    scanner = MarketScanner(client=client, settings=_make_settings(max_spread=0.01))

    result = await scanner.get_tradeable_markets(["CLOSED.EPIC", "OPEN.EPIC"])
    epics = [m.epic for m in result]

    assert "CLOSED.EPIC" not in epics
    assert "OPEN.EPIC" in epics


@pytest.mark.asyncio
async def test_get_tradeable_markets_excludes_zero_price() -> None:
    """get_tradeable_markets should exclude markets where bid or offer is 0."""
    client = MagicMock()
    client.get = AsyncMock(
        return_value={
            "marketDetails": [
                _make_market_detail("NO.PRICE", bid=0.0, offer=0.0),
                _make_market_detail("HAS.PRICE", bid=100.0, offer=100.1),
            ]
        }
    )
    scanner = MarketScanner(client=client, settings=_make_settings(max_spread=0.01))

    result = await scanner.get_tradeable_markets(["NO.PRICE", "HAS.PRICE"])
    epics = [m.epic for m in result]

    assert "NO.PRICE" not in epics
    assert "HAS.PRICE" in epics


# ------------------------------------------------------------------
# Market-level dedup + funds filter (select_tradable)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tradable_collapses_product_variants_keeping_tightest_spread() -> None:
    """IFMM/IMF variants of the same index collapse to one (tightest spread)."""
    client = MagicMock()
    client.get = AsyncMock(
        return_value={
            "marketDetails": [
                # Same underlying DAX market, different product codes.
                _make_market_detail("IX.D.DAX.IFMM.IP", bid=18000.0, offer=18010.0),
                _make_market_detail("IX.D.DAX.IMF.IP", bid=18000.0, offer=18002.0),
                _make_market_detail("CS.D.EURGBP.CFD.IP", bid=0.85, offer=0.8501),
            ]
        }
    )
    scanner = MarketScanner(client=client, settings=_make_settings(max_spread=0.01))

    result = await scanner.get_tradeable_markets(
        ["IX.D.DAX.IFMM.IP", "IX.D.DAX.IMF.IP", "CS.D.EURGBP.CFD.IP"]
    )
    epics = [m.epic for m in result]

    # Only one DAX variant survives — the tighter-spread IMF one.
    assert "IX.D.DAX.IMF.IP" in epics
    assert "IX.D.DAX.IFMM.IP" not in epics
    assert "CS.D.EURGBP.CFD.IP" in epics


@pytest.mark.asyncio
async def test_tradable_drops_unaffordable_epics_keeps_unknown() -> None:
    """Epics above the funds cap are dropped; unknown-funds epics are kept."""
    client = MagicMock()
    client.get = AsyncMock(
        return_value={
            "marketDetails": [
                # margin = 1 * 1 * 20000 * 50% = 10000€ — over the 500€ cap.
                _make_market_detail(
                    "EXPENSIVE.EPIC",
                    bid=20000.0,
                    offer=20000.0,
                    margin_factor=50.0,
                ),
                # margin = 1 * 1 * 100 * 5% = 5€ — under the cap.
                _make_market_detail(
                    "CHEAP.EPIC", bid=100.0, offer=100.0, margin_factor=5.0
                ),
                # No margin data → funds unknown → kept.
                _make_market_detail("UNKNOWN.EPIC", bid=100.0, offer=100.1),
            ]
        }
    )
    scanner = MarketScanner(
        client=client, settings=_make_settings(max_spread=0.01, max_funds=500.0)
    )

    result = await scanner.get_tradeable_markets(
        ["EXPENSIVE.EPIC", "CHEAP.EPIC", "UNKNOWN.EPIC"]
    )
    epics = [m.epic for m in result]

    assert "EXPENSIVE.EPIC" not in epics
    assert "CHEAP.EPIC" in epics
    assert "UNKNOWN.EPIC" in epics


@pytest.mark.asyncio
async def test_market_info_carries_funds_needed() -> None:
    """funds_needed is computed onto MarketInfo from the /markets payload."""
    client = MagicMock()
    client.get = AsyncMock(
        return_value={
            "marketDetails": [
                _make_market_detail(
                    "FX.EPIC", bid=100.0, offer=200.0, margin_factor=10.0
                ),
            ]
        }
    )
    scanner = MarketScanner(client=client, settings=_make_settings())

    infos = await scanner.get_all_market_infos(["FX.EPIC"])

    # funds = euro_per_point(1) * offer(200) * 10% = 1 * 200 * 0.10 = 20€
    assert infos[0].funds_needed == pytest.approx(20.0)


# ------------------------------------------------------------------
# Asset-class filter (instrument type)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_off_class_instruments_are_dropped() -> None:
    """SHARES (e.g. gold miners with .CASH epics) are filtered out.

    Allowed asset classes are kept; an unknown/blank type is kept too (we can't
    prove it's out of scope).
    """
    client = MagicMock()
    client.get = AsyncMock(
        return_value={
            "marketDetails": [
                _make_market_detail(
                    "CC.D.LCO.CFD.IP", 100.0, 100.1, instrument_type="COMMODITIES"
                ),
                _make_market_detail(
                    "IX.D.DAX.IFMM.IP", 100.0, 100.1, instrument_type="INDICES"
                ),
                _make_market_detail(
                    "SD.D.BARRICK.CASH.IP", 100.0, 100.1, instrument_type="SHARES"
                ),
                _make_market_detail("MYSTERY.EPIC", 100.0, 100.1),  # no type → kept
            ]
        }
    )
    settings = _make_settings(allowed_types=["CURRENCIES", "INDICES", "COMMODITIES"])
    scanner = MarketScanner(client=client, settings=settings)

    infos = await scanner.get_all_market_infos(
        ["CC.D.LCO.CFD.IP", "IX.D.DAX.IFMM.IP", "SD.D.BARRICK.CASH.IP", "MYSTERY.EPIC"]
    )
    epics = [i.epic for i in infos]

    assert "SD.D.BARRICK.CASH.IP" not in epics
    assert "CC.D.LCO.CFD.IP" in epics
    assert "IX.D.DAX.IFMM.IP" in epics
    assert "MYSTERY.EPIC" in epics  # unknown type is kept


@pytest.mark.asyncio
async def test_empty_allow_list_disables_asset_class_filter() -> None:
    """An empty allow-list keeps every asset class, including SHARES."""
    client = MagicMock()
    client.get = AsyncMock(
        return_value={
            "marketDetails": [
                _make_market_detail(
                    "SD.D.BARRICK.CASH.IP", 100.0, 100.1, instrument_type="SHARES"
                ),
            ]
        }
    )
    scanner = MarketScanner(client=client, settings=_make_settings(allowed_types=[]))

    infos = await scanner.get_all_market_infos(["SD.D.BARRICK.CASH.IP"])

    assert [i.epic for i in infos] == ["SD.D.BARRICK.CASH.IP"]


# ------------------------------------------------------------------
# Batch fetching — poison-epic isolation
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_500_bisects_to_drop_only_the_bad_epic() -> None:
    """A single unresolvable epic must not take down the rest of the batch."""

    async def mock_get(
        endpoint: str, *, version: int = 1, suppress_error_logging: bool = False
    ) -> dict:
        epics_str = endpoint.split("epics=", 1)[1]
        batch = epics_str.split(",")
        if "BAD.EPIC" in batch:
            raise _ig_error(epics_str, 500, "Transformation failure")
        return {"marketDetails": [_make_market_detail(e, 100.0, 100.1) for e in batch]}

    client = MagicMock()
    client.get = AsyncMock(side_effect=mock_get)
    scanner = MarketScanner(client=client, settings=_make_settings(max_spread=0.01))

    result = await scanner.get_tradeable_markets(["G1", "BAD.EPIC", "G2", "G3"])
    epics = [m.epic for m in result]

    assert epics == ["G1", "G2", "G3"]


@pytest.mark.asyncio
async def test_batch_non_500_error_does_not_bisect() -> None:
    """A rate-limit (or other non-500) failure drops the batch without splitting."""
    call_count = 0

    async def mock_get(
        endpoint: str, *, version: int = 1, suppress_error_logging: bool = False
    ) -> dict:
        nonlocal call_count
        call_count += 1
        epics_str = endpoint.split("epics=", 1)[1]
        raise _ig_error(epics_str, 429, "error.request.too.frequent")

    client = MagicMock()
    client.get = AsyncMock(side_effect=mock_get)
    scanner = MarketScanner(client=client, settings=_make_settings(max_spread=0.01))

    result = await scanner.get_tradeable_markets(["A", "B", "C", "D"])

    assert result == []
    assert call_count == 1  # no bisection on non-500 errors


def test_diversified_subset_balances_classes_over_tightest_spread() -> None:
    """The cap is filled round-robin per class, not by global spread.

    FX pairs have far tighter spreads here, so a pure spread sort would take all
    6 forex names and ignore indices/commodities. The diversified pick must keep
    the best of every class instead.
    """
    markets = (
        [_market_info(f"FX{i}", "CURRENCIES", 0.0001 * (i + 1)) for i in range(6)]
        + [_market_info(f"IDX{i}", "INDICES", 0.01 * (i + 1)) for i in range(4)]
        + [_market_info(f"COM{i}", "COMMODITIES", 0.05 * (i + 1)) for i in range(4)]
    )

    chosen = MarketScanner.select_diversified_subset(markets, cap=6)
    classes = {m.instrument_type for m in chosen}

    assert len(chosen) == 6
    assert classes == {"CURRENCIES", "INDICES", "COMMODITIES"}
    # Round-robin takes 2 of each (tightest first within the class).
    fx = [m.epic for m in chosen if m.instrument_type == "CURRENCIES"]
    idx = [m.epic for m in chosen if m.instrument_type == "INDICES"]
    assert fx == ["FX0", "FX1"]
    assert idx == ["IDX0", "IDX1"]


def test_diversified_subset_reassigns_slots_when_a_class_runs_out() -> None:
    """A class with few markets yields its unused slots to the others."""
    markets = (
        [_market_info(f"FX{i}", "CURRENCIES", 0.0001 * (i + 1)) for i in range(8)]
        + [_market_info("IDX0", "INDICES", 0.01)]
        + [_market_info("COM0", "COMMODITIES", 0.05)]
    )

    chosen = MarketScanner.select_diversified_subset(markets, cap=6)
    epics = {m.epic for m in chosen}

    assert len(chosen) == 6
    # The single index and commodity are kept; the rest fills with tightest FX.
    assert {"IDX0", "COM0"}.issubset(epics)
    assert sum(1 for m in chosen if m.instrument_type == "CURRENCIES") == 4


def test_diversified_subset_returns_input_when_it_already_fits() -> None:
    """No selection happens when the market count is at or under the cap."""
    markets = [_market_info("FX0", "CURRENCIES", 0.0001)]
    assert MarketScanner.select_diversified_subset(markets, cap=40) == markets


class TestParseMarketCloseUtc:
    """Defensive parsing of IG instrument.openingHours -> UTC close time."""

    def test_resolves_close_to_utc_with_offset(self):
        from datetime import time

        from src.markets.market_scanner import parse_market_close_utc

        instrument = {
            "timeZoneOffset": 1,  # market is UTC+1
            "openingHours": {
                "marketTimes": [{"openTime": "08:00", "closeTime": "16:30"}]
            },
        }
        # 16:30 local (UTC+1) -> 15:30 UTC
        assert parse_market_close_utc(instrument) == time(15, 30)

    def test_picks_latest_close_across_sessions(self):
        from datetime import time

        from src.markets.market_scanner import parse_market_close_utc

        instrument = {
            "timeZoneOffset": 0,
            "openingHours": {
                "marketTimes": [
                    {"openTime": "08:00", "closeTime": "12:00"},
                    {"openTime": "13:00", "closeTime": "17:00"},
                ]
            },
        }
        assert parse_market_close_utc(instrument) == time(17, 0)

    def test_accepts_plain_list_form(self):
        from datetime import time

        from src.markets.market_scanner import parse_market_close_utc

        instrument = {
            "timeZoneOffset": 2,
            "openingHours": [{"openTime": "09:00", "closeTime": "22:00"}],
        }
        # 22:00 local (UTC+2) -> 20:00 UTC
        assert parse_market_close_utc(instrument) == time(20, 0)

    def test_none_without_offset(self):
        from src.markets.market_scanner import parse_market_close_utc

        instrument = {
            "openingHours": {
                "marketTimes": [{"openTime": "08:00", "closeTime": "16:30"}]
            }
        }
        # No timezone offset -> cannot resolve to UTC -> None (fall back to global)
        assert parse_market_close_utc(instrument) is None

    def test_none_for_24h_market(self):
        from src.markets.market_scanner import parse_market_close_utc

        instrument = {
            "timeZoneOffset": 0,
            "openingHours": {
                "marketTimes": [{"openTime": "00:00", "closeTime": "00:00"}]
            },
        }
        # open == close -> 24h market, no meaningful daily close -> None
        assert parse_market_close_utc(instrument) is None

    def test_none_when_absent_or_malformed(self):
        from src.markets.market_scanner import parse_market_close_utc

        assert parse_market_close_utc({}) is None
        assert parse_market_close_utc({"openingHours": None}) is None
        assert (
            parse_market_close_utc(
                {"timeZoneOffset": 1, "openingHours": {"marketTimes": []}}
            )
            is None
        )
        assert (
            parse_market_close_utc(
                {
                    "timeZoneOffset": 1,
                    "openingHours": {"marketTimes": [{"openTime": "x"}]},
                }
            )
            is None
        )
