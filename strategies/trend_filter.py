import pandas as pd
from .base import BaseStrategy


class TrendFilter(BaseStrategy):
    """
    Filtro de tendência baseado na MA50.
    BUY  → preço acima da MA50 (uptrend) — autoriza entradas
    SELL → preço abaixo da MA50 (downtrend) — bloqueia entradas
    HOLD → sem dados suficientes
    """

    def __init__(self, period: int = 50):
        super().__init__("Trend")
        self.period = period

    def analyze(self, df: pd.DataFrame) -> str:
        if len(df) < self.period:
            return "HOLD"
        ma = df["close"].rolling(self.period).mean().iloc[-1]
        price = df["close"].iloc[-1]
        if price > ma:
            return "BUY"    # uptrend → permite entrada
        return "SELL"       # downtrend → bloqueia entrada
