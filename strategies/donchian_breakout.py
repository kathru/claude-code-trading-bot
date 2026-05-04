import pandas as pd
from .base import BaseStrategy


class DonchianBreakout(BaseStrategy):
    """
    Donchian Channel Breakout (estilo Turtle Traders) — 30min.

    BUY  → Close > maior high das últimas N barras (ex.: 20)
           + RSI(14) > 55 (momentum positivo)
           + volume > 1.2× média (confirmação)
    SELL → Close < menor low das últimas N barras (saída técnica;
           o trading loop usa SL/TP/trailing — este sinal é sinalização visual)

    Edge: catch breakouts EARLY. Crypto é fortemente trending,
    e Donchian historicamente captura todos os movimentos grandes.
    """

    def __init__(self, period: int = 20, rsi_period: int = 14,
                 rsi_min: float = 55.0, vol_mult: float = 1.2):
        super().__init__("Donchian Breakout")
        self.period     = period
        self.rsi_period = rsi_period
        self.rsi_min    = rsi_min
        self.vol_mult   = vol_mult

    def _rsi(self, series: pd.Series) -> pd.Series:
        delta = series.diff()
        gain  = delta.clip(lower=0).rolling(self.rsi_period).mean()
        loss  = (-delta.clip(upper=0)).rolling(self.rsi_period).mean()
        rs    = gain / loss.replace(0, float("inf"))
        return 100 - (100 / (1 + rs))

    def analyze(self, df: pd.DataFrame) -> str:
        if len(df) < self.period + self.rsi_period + 5:
            return "HOLD"

        df = df.copy()
        # Donchian: maior high / menor low das últimas N barras (excl. atual)
        df["dc_upper"] = df["high"].rolling(self.period).max().shift(1)
        df["dc_lower"] = df["low"].rolling(self.period).min().shift(1)
        df["rsi"]      = self._rsi(df["close"])
        df["vol_ma"]   = df["volume"].rolling(self.period).mean()
        df = df.dropna().reset_index(drop=True)
        if len(df) < 2:
            return "HOLD"

        curr = df.iloc[-1]
        # ── BUY: rompimento de máxima + momentum + volume
        if (curr["close"] > curr["dc_upper"]
            and curr["rsi"] >= self.rsi_min
            and curr["volume"] >= curr["vol_ma"] * self.vol_mult):
            return "BUY"

        # ── SELL técnico: rompimento de mínima
        if curr["close"] < curr["dc_lower"]:
            return "SELL"

        return "HOLD"
