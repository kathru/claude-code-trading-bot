import pandas as pd
from .base import BaseStrategy


class RSIStrategy(BaseStrategy):
    def __init__(self, period: int = 14, oversold: float = 30, overbought: float = 70):
        super().__init__("RSI")
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    def _calc_rsi(self, series: pd.Series) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0).rolling(self.period).mean()
        loss = (-delta.clip(upper=0)).rolling(self.period).mean()
        rs = gain / loss.replace(0, float("inf"))
        return 100 - (100 / (1 + rs))

    def analyze(self, df: pd.DataFrame) -> str:
        if len(df) < self.period + 2:
            return "HOLD"

        df = df.copy()
        df["rsi"] = self._calc_rsi(df["close"])

        prev_rsi = df["rsi"].iloc[-2]
        curr_rsi = df["rsi"].iloc[-1]

        # Saindo de sobrevenda -> BUY
        if prev_rsi < self.oversold and curr_rsi >= self.oversold:
            return "BUY"

        # Saindo de sobrecompra -> SELL
        if prev_rsi > self.overbought and curr_rsi <= self.overbought:
            return "SELL"

        return "HOLD"
