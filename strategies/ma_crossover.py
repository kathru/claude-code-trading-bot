import pandas as pd
from .base import BaseStrategy


class MACrossoverStrategy(BaseStrategy):
    def __init__(self, short_window: int = 9, long_window: int = 21):
        super().__init__("MA Crossover")
        self.short_window = short_window
        self.long_window = long_window

    def analyze(self, df: pd.DataFrame) -> str:
        if len(df) < self.long_window + 1:
            return "HOLD"

        df = df.copy()
        df["ma_short"] = df["close"].rolling(self.short_window).mean()
        df["ma_long"] = df["close"].rolling(self.long_window).mean()

        prev = df.iloc[-2]
        curr = df.iloc[-1]

        # Golden cross: short crosses above long -> BUY
        if prev["ma_short"] <= prev["ma_long"] and curr["ma_short"] > curr["ma_long"]:
            return "BUY"

        # Death cross: short crosses below long -> SELL
        if prev["ma_short"] >= prev["ma_long"] and curr["ma_short"] < curr["ma_long"]:
            return "SELL"

        return "HOLD"
