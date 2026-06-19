"""Feed domain — live price feed, in-memory buffer and candle persistence.

Subscribes to the chosen epics' Lightstreamer feed, maintains the rolling
in-memory candle buffer used by the strategies, and persists candles to the DB.
Purely functional and independent of open/close logic.
"""
