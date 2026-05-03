import pandas as pd
from .base import BaseStrategy


class SupportResistance(BaseStrategy):
    """
    Support-Resistance Flip — 1H candles.
    Detecta quando o preço rompe uma resistência e a retesta como suporte (confirmação).
    BUY  → Rompimento + retest confirmado (resistência virou suporte).
    SELL → Preço abaixo de suporte + retest como resistência.
    """

    def __init__(self, lookback: int = 50, tolerance_pct: float = 1.0, min_touches: int = 2):
        super().__init__("S/R Flip")
        self.lookback    = lookback
        self.tolerance   = tolerance_pct / 100   # 1% de tolerância para detectar mais níveis
        self.min_touches = min_touches

    def _find_levels(self, df: pd.DataFrame) -> list:
        highs  = df["high"].values
        lows   = df["low"].values
        levels = []
        for i in range(2, len(df) - 2):
            # Resistência: máximo local
            if highs[i] >= max(highs[i-2:i]) and highs[i] >= max(highs[i+1:i+3]):
                level = highs[i]
                touches = sum(
                    abs(highs[j] - level) / level < self.tolerance
                    or abs(lows[j] - level) / level < self.tolerance
                    for j in range(len(df)) if j != i
                )
                if touches >= self.min_touches:
                    levels.append(level)
        return sorted(set(round(l, 2) for l in levels))

    def analyze(self, df: pd.DataFrame) -> str:
        if len(df) < self.lookback + 5:
            return "HOLD"

        window = df.tail(self.lookback).reset_index(drop=True)
        levels = self._find_levels(window)
        if not levels:
            return "HOLD"

        current = df["close"].iloc[-1]
        prev    = df["close"].iloc[-4]   # 4 velas atrás para confirmar movimento

        for level in levels:
            band_hi = level * (1 + self.tolerance)
            band_lo = level * (1 - self.tolerance)

            # Preço estava abaixo → rompeu resistência → retestou como suporte → BUY
            if prev < band_lo and band_lo <= current <= band_hi:
                return "BUY"

            # Preço estava acima → quebrou suporte → retestou como resistência → SELL
            if prev > band_hi and band_lo <= current <= band_hi:
                return "SELL"

        return "HOLD"
