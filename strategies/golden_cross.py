import pandas as pd
from .base import BaseStrategy


class GoldenCross(BaseStrategy):
    """
    Golden Cross / Death Cross — candles diários (ONE_DAY).
    BUY  → MA50 cruza acima da MA200 (Golden Cross).
    SELL → MA50 cruza abaixo da MA200 (Death Cross).
    HOLD → Sem cruzamento recente.

    Também detecta consolidação (BTC passa 70% do tempo consolidando):
    Em consolidação, sinaliza acumulação (BUY fraco) para aproveitar
    o rompimento posterior.
    """

    def __init__(self, short: int = 50, long: int = 200, consol_pct: float = 2.0):
        super().__init__("Golden Cross")
        self.short      = short
        self.long       = long
        self.consol_pct = consol_pct / 100   # amplitude % para considerar consolidação

    def analyze(self, df: pd.DataFrame) -> str:
        if len(df) < self.long + 2:
            return "HOLD"

        df = df.copy()
        df["ma_short"] = df["close"].rolling(self.short).mean()
        df["ma_long"]  = df["close"].rolling(self.long).mean()
        df = df.dropna().reset_index(drop=True)

        prev = df.iloc[-2]
        curr = df.iloc[-1]

        # Golden Cross: MA50 cruza acima da MA200
        if prev["ma_short"] <= prev["ma_long"] and curr["ma_short"] > curr["ma_long"]:
            return "BUY"

        # Death Cross: MA50 cruza abaixo da MA200
        if prev["ma_short"] >= prev["ma_long"] and curr["ma_short"] < curr["ma_long"]:
            return "SELL"

        # Consolidação: preço dentro de ±consol_pct da MA50 → acumulação leve
        price_vs_ma = abs(curr["close"] - curr["ma_short"]) / curr["ma_short"]
        ma_aligned  = curr["ma_short"] > curr["ma_long"]   # tendência de alta

        if price_vs_ma <= self.consol_pct and ma_aligned:
            return "BUY"   # acumula na consolidação em uptrend

        return "HOLD"
