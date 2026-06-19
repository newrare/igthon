"""Backtest & simulation domain — offline evaluation of entry/close pairings.

Replays entry strategies + close profiles over synthetic curves (the simulator)
or archived real candles (the backtester), with no DB and no IG API. Reads the
candle archive and the curve generator. Because open and close are decoupled, a
close profile can be measured independently of any entry here.
"""
