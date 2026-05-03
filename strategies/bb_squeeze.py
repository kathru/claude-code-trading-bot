import pandas as pd
from .base import BaseStrategy


class BBSqueeze(BaseStrategy):
    """
    Bollinger Band Squeeze — 1H candles.
    Aguarda período de baixa volatilidade (bandas estreitas) e compra no
    rompimento (breakout), idealmente com volume acima da média.

    BUY  → Squeeze detectado + rompimento para cima com volume.
    SELL → Squeeze + rompimento para baixo com volume.
    """

    def __init__(self, period: int = 20, std: float = 2.0,
                 squeeze_pct: float = 3.0, vol_mult: float = 1.5):
        super().__init__("BB Squeeze")
        self.period      = period
        self.std         = std
        self.squeeze_pct = squeeze_pct / 100   # largura % para considerar squeeze
        self.vol_mult    = vol_mult            # volume deve ser X× a média

    def analyze(self, df: pd.DataFrame) -> str:
        if len(df) < self.period + 5:
            return "HOLD"

        df = df.copy()
        df["ma"]    = df["close"].rolling(self.period).mean()
        df["upper"] = df["ma"] + self.std * df["close"].rolling(self.period).std()
        df["lower"] = df["ma"] - self.std * df["close"].rolling(self.period).std()
        df["width"] = (df["upper"] - df["lower"]) / df["ma"]
        df["vol_ma"] = df["volume"].rolling(self.period).mean()
        df = df.dropna().reset_index(drop=True)

        if len(df) < 3:
            return "HOLD"

        # Squeeze: largura mínima recente
        recent_min_width = df["width"].iloc[-10:].min()
        prev_width       = df["width"].iloc[-2]
        curr_width       = df["width"].iloc[-1]

        is_squeeze = recent_min_width <= self.squeeze_pct
        is_expanding = curr_width > prev_width * 1.05   # banda expandindo

        curr        = df.iloc[-1]
        high_volume = curr["volume"] >= curr["vol_ma"] * self.vol_mult

        if is_squeeze and is_expanding:
            if curr["close"] > curr["upper"] and high_volume:
                return "BUY"
            if curr["close"] < curr["lower"] and high_volume:
                return "SELL"

        return "HOLD"
