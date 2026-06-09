"""Tests for utility helpers — focus on the euro P&L calculation."""

import pytest

from src.utils.tools import (
    conversion_rate,
    euro_per_point,
    parse_ig_pnl,
)


class TestParseIgPnl:
    """IG ``profitAndLoss`` strings are prefixed with a currency symbol."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("E-2.73", -2.73),
            ("E21.82", 21.82),
            ("E+15.00", 15.0),
            ("-2.73", -2.73),
            ("E-1,234.50", -1234.50),  # thousands separator
            ("$1.50", 1.5),
            ("£-3.20", -3.2),
            (-2.73, -2.73),
            (5, 5.0),
        ],
    )
    def test_parses_signed_amount(self, raw, expected):
        assert parse_ig_pnl(raw) == pytest.approx(expected)

    @pytest.mark.parametrize("raw", [None, "", "  ", "E", "abc", "-"])
    def test_returns_none_when_unparseable(self, raw):
        assert parse_ig_pnl(raw) is None


class TestConversionRate:
    """The quote->EUR rate comes from instrument.currencies[].exchangeRate."""

    def test_matches_currency_code(self):
        instrument = {
            "currencies": [
                {"code": "JPY", "exchangeRate": 0.005455, "isDefault": True},
                {"code": "USD", "exchangeRate": 0.92},
            ]
        }
        assert conversion_rate(instrument, "USD") == pytest.approx(0.92)

    def test_falls_back_to_default_then_first(self):
        instrument = {
            "currencies": [
                {"code": "JPY", "exchangeRate": 0.005455, "isDefault": True},
            ]
        }
        assert conversion_rate(instrument, "ZZZ") == pytest.approx(0.005455)

    def test_no_currencies_returns_one(self):
        assert conversion_rate({}, "EUR") == 1.0

    def test_base_exchange_rate_fallback(self):
        instrument = {"currencies": [{"code": "EUR", "baseExchangeRate": 1.0}]}
        assert conversion_rate(instrument, "EUR") == 1.0


class TestEuroPerPoint:
    """euro_per_point reproduces the broker's realized P&L exactly.

    Each case is a real trade from the IG statement:
    P&L = (close - open) * euro_per_point.
    """

    @pytest.mark.parametrize(
        "contract_size,currency,rate,size,open_p,close_p,expected_pnl",
        [
            # USD/JPY: 100k contract, JPY quote -> EUR at 0.005454958709088
            (100000, "JPY", 0.005454958709088, 1, 160.239, 160.234, -2.73),
            # GBP/JPY
            (100000, "JPY", 0.005455141741728, 1, 213.831, 213.791, -21.82),
            # EUR/JPY
            (100000, "JPY", 0.005453857819872, 1, 184.856, 184.816, -21.82),
            # GBP/EUR: quote already in EUR, no conversion
            (100000, "EUR", 1.0, 1, 1.15729, 1.15649, -80.00),
            # France 40 cash: €10 per point
            (10, "EUR", 1.0, 1, 8179.3, 8175.3, -40.00),
            # GBP/EUR small moves
            (100000, "EUR", 1.0, 1, 1.15755, 1.15732, -23.00),
            (100000, "EUR", 1.0, 1, 1.15753, 1.15738, -15.00),
        ],
    )
    def test_reproduces_broker_pnl(
        self, contract_size, currency, rate, size, open_p, close_p, expected_pnl
    ):
        market_data = {
            "instrument": {
                "contractSize": str(contract_size),
                "currencies": [{"code": currency, "exchangeRate": rate}],
            }
        }
        epp = euro_per_point(market_data, size, currency)
        pnl = (close_p - open_p) * epp
        assert pnl == pytest.approx(expected_pnl, abs=0.01)

    def test_zero_when_contract_size_unknown(self):
        """Returns 0.0 so callers fall back to the legacy estimate."""
        assert euro_per_point({"instrument": {}}, 1, "EUR") == 0.0

    def test_lot_size_fallback(self):
        market_data = {
            "instrument": {
                "lotSize": "10",
                "currencies": [{"code": "EUR", "exchangeRate": 1.0}],
            }
        }
        assert euro_per_point(market_data, 1, "EUR") == pytest.approx(10.0)

    def test_scales_with_size(self):
        market_data = {
            "instrument": {
                "contractSize": "100000",
                "currencies": [{"code": "EUR", "exchangeRate": 1.0}],
            }
        }
        assert euro_per_point(market_data, 3, "EUR") == pytest.approx(300000.0)
