"""Tests for the entry signal book (``OPEN_MODE=notify``).

A signal is a proposed open the bot did not take. The book must notify once per
setup (not once per analysis tick), forget a proposal the market has outrun, and
stop counting a signal the user has answered.
"""

from datetime import UTC, datetime, timedelta

from src.core.signals import EntrySignal, SignalBook


class TestRecord:
    """Raising a signal."""

    def test_record_returns_the_signal_and_makes_it_pending(self):
        book = SignalBook()
        signal = book.record(
            "IX.D.DAX.IFMM.IP",
            "buy",
            strategy="open_momentum",
            score=1.5,
            price=18000.0,
            name="Germany 40",
        )
        assert isinstance(signal, EntrySignal)
        assert signal.direction == "BUY"  # normalised
        assert [s.id for s in book.pending()] == [signal.id]

    def test_same_epic_within_cooldown_raises_nothing(self):
        # A strategy re-firing every 30 s tick must notify once, not forty times.
        book = SignalBook()
        first = book.record("E", "BUY")
        again = book.record("E", "BUY")
        assert first is not None
        assert again is None
        assert len(book.pending()) == 1

    def test_another_epic_is_not_held_by_the_cooldown(self):
        book = SignalBook()
        book.record("A", "BUY")
        assert book.record("B", "BUY") is not None
        assert len(book.pending()) == 2

    def test_cooldown_expires(self):
        book = SignalBook(cooldown=timedelta(seconds=0))
        assert book.record("E", "BUY") is not None
        assert book.record("E", "BUY") is not None

    def test_ids_are_unique_and_increasing(self):
        book = SignalBook(cooldown=timedelta(seconds=0))
        ids = [book.record("E", "BUY").id for _ in range(3)]
        assert ids == sorted(set(ids))

    def test_newest_first(self):
        book = SignalBook()
        book.record("A", "BUY")
        book.record("B", "SELL")
        assert [s.epic for s in book.pending()] == ["B", "A"]

    def test_size_is_capped(self):
        book = SignalBook(cooldown=timedelta(seconds=0), max_signals=3)
        for _ in range(6):
            book.record("E", "BUY")
        assert len(book.pending()) == 3


class TestExpiry:
    """A proposal is worth nothing once its market has moved on."""

    def test_signals_older_than_the_ttl_are_dropped(self):
        book = SignalBook(ttl=timedelta(minutes=10))
        signal = book.record("E", "BUY")
        signal.created_at = datetime.now(UTC) - timedelta(minutes=11)
        assert book.pending() == []

    def test_an_expired_epic_can_signal_again(self):
        book = SignalBook(ttl=timedelta(minutes=10), cooldown=timedelta(minutes=10))
        signal = book.record("E", "BUY")
        signal.created_at = datetime.now(UTC) - timedelta(minutes=11)
        assert book.record("E", "BUY") is not None


class TestDismiss:
    """Answering a proposal."""

    def test_dismiss_removes_it_from_pending(self):
        book = SignalBook()
        signal = book.record("E", "BUY")
        assert book.dismiss(signal.id) is True
        assert book.pending() == []

    def test_dismiss_twice_reports_false(self):
        book = SignalBook()
        signal = book.record("E", "BUY")
        book.dismiss(signal.id)
        assert book.dismiss(signal.id) is False

    def test_dismiss_unknown_id_reports_false(self):
        assert SignalBook().dismiss(999) is False

    def test_dismissed_epic_stays_in_cooldown(self):
        # Dismissing must not re-arm the notification on the next tick.
        book = SignalBook()
        signal = book.record("E", "BUY")
        book.dismiss(signal.id)
        assert book.record("E", "BUY") is None

    def test_dismiss_epic_clears_every_pending_signal_on_it(self):
        book = SignalBook(cooldown=timedelta(seconds=0))
        book.record("E", "BUY")
        book.record("E", "SELL")
        book.record("OTHER", "BUY")
        assert book.dismiss_epic("E") == 2
        assert [s.epic for s in book.pending()] == ["OTHER"]

    def test_clear_empties_the_book(self):
        book = SignalBook(cooldown=timedelta(seconds=0))
        book.record("A", "BUY")
        book.record("B", "BUY")
        book.clear()
        assert book.pending() == []


class TestSerialisation:
    """The dashboard JSON shape."""

    def test_as_dicts_carries_what_the_card_needs(self):
        book = SignalBook()
        book.record(
            "E", "SELL", strategy="open_fade", score=2.345, price=101.5, name="Epic E"
        )
        (payload,) = book.as_dicts()
        assert payload["epic"] == "E"
        assert payload["direction"] == "SELL"
        assert payload["strategy"] == "open_fade"
        assert payload["score"] == 2.35  # rounded for display
        assert payload["price"] == 101.5
        assert payload["name"] == "Epic E"
        assert payload["age_seconds"] >= 0
        assert "id" in payload and "created_at" in payload

    def test_name_falls_back_to_the_epic(self):
        book = SignalBook()
        book.record("E", "BUY")
        assert book.as_dicts()[0]["name"] == "E"

    def test_len_counts_only_pending(self):
        book = SignalBook(cooldown=timedelta(seconds=0))
        first = book.record("E", "BUY")
        book.record("E", "BUY")
        book.dismiss(first.id)
        assert len(book) == 1
