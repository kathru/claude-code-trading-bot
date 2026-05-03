import pandas as pd
import numpy as np
from .base import BaseStrategy


class RSIDivergence(BaseStrategy):
    """
    RSI Divergence — 4H/6H candles.
    BUY  → Divergência de alta: preço faz fundo menor, RSI faz fundo maior.
    SELL → Divergência de baixa: preço faz topo maior, RSI faz topo menor.
    """

    def __init__(self, rsi_period: int = 14, lookback: int = 30, swing_size: int = 5):
        super().__init__("RSI Divergence")
        self.rsi_period = rsi_period
        self.lookback = lookback
        self.swing_size = swing_size   # janela para identificar swings

    def _calc_rsi(self, series: pd.Series) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0).rolling(self.rsi_period).mean()
        loss = (-delta.clip(upper=0)).rolling(self.rsi_period).mean()
        rs = gain / loss.replace(0, float("inf"))
        return 100 - (100 / (1 + rs))

    def _find_lows(self, series: pd.Series, n: int) -> list:
        """Retorna índices dos fundos locais."""
        lows = []
        for i in range(n, len(series) - n):
            if series.iloc[i] == series.iloc[i-n:i+n+1].min():
                lows.append(i)
        return lows[-3:]   # últimos 3 fundos

    def _find_highs(self, series: pd.Series, n: int) -> list:
        highs = []
        for i in range(n, len(series) - n):
            if series.iloc[i] == series.iloc[i-n:i+n+1].max():
                highs.append(i)
        return highs[-3:]

    def analyze(self, df: pd.DataFrame) -> str:
        min_bars = self.rsi_period + self.lookback + self.swing_size * 2
        if len(df) < min_bars:
            return "HOLD"

        df = df.tail(self.lookback + self.swing_size * 4).copy()
        df["rsi"] = self._calc_rsi(df["close"])
        df = df.dropna(subset=["rsi"]).reset_index(drop=True)

        price = df["close"]
        rsi   = df["rsi"]

        # ── Divergência de alta (bullish) ─────────────────────────────
        lows = self._find_lows(price, self.swing_size)
        if len(lows) >= 2:
            i1, i2 = lows[-2], lows[-1]
            price_lower_low = price.iloc[i2] < price.iloc[i1]
            rsi_higher_low  = rsi.iloc[i2]   > rsi.iloc[i1]
            if price_lower_low and rsi_higher_low:
                return "BUY"

        # ── Divergência de baixa (bearish) ────────────────────────────
        highs = self._find_highs(price, self.swing_size)
        if len(highs) >= 2:
            i1, i2 = highs[-2], highs[-1]
            price_higher_high = price.iloc[i2] > price.iloc[i1]
            rsi_lower_high    = rsi.iloc[i2]   < rsi.iloc[i1]
            if price_higher_high and rsi_lower_high:
                return "SELL"

        return "HOLD"
