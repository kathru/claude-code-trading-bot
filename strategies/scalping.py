import pandas as pd
from .base import BaseStrategy


class ScalpingStrategy(BaseStrategy):
    """Scalping baseado em momentum de curto prazo e Bollinger Bands."""

    def __init__(self, bb_period: int = 20, bb_std: float = 2.0, momentum_period: int = 5):
        super().__init__("Scalping")
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.momentum_period = momentum_period

    def analyze(self, df: pd.DataFrame) -> str:
        if len(df) < self.bb_period + self.momentum_period:
            return "HOLD"

        df = df.copy()
        df["ma"] = df["close"].rolling(self.bb_period).mean()
        df["std"] = df["close"].rolling(self.bb_period).std()
        df["upper"] = df["ma"] + self.bb_std * df["std"]
        df["lower"] = df["ma"] - self.bb_std * df["std"]
        df["momentum"] = df["close"].pct_change(self.momentum_period)

        curr = df.iloc[-1]

        # Preço tocou a banda inferior com momentum positivo -> BUY
        if curr["close"] <= curr["lower"] and curr["momentum"] > 0:
            return "BUY"

        # Preço tocou a banda superior com momentum negativo -> SELL
        if curr["close"] >= curr["upper"] and curr["momentum"] < 0:
            return "SELL"

        return "HOLD"
