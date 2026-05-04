import pandas as pd
from .base import BaseStrategy


class StochBounce(BaseStrategy):
    """
    Stochastic Oversold Bounce — 30min candles.
    Mean reversion APENAS em uptrend macro.

    Filtro macro: close > MA200 (não opera mean revert em downtrend)
    BUY  → Stoch %K < 25 (sobrevenda) E %K cruza acima de %D (reversão)
           E vela atual é verde (confirmação)
    SELL → Stoch %K > 80 E %K cruza abaixo de %D (sobrecompra)

    Edge: pega quicadas em pullbacks de uptrend. Win rate alto
    pois só compra dips em regime de alta confirmado.
    """

    def __init__(self, k_period: int = 14, d_period: int = 3,
                 oversold: float = 25.0, overbought: float = 80.0,
                 ma_filter: int = 200):
        super().__init__("Stoch Bounce")
        self.k_period   = k_period
        self.d_period   = d_period
        self.oversold   = oversold
        self.overbought = overbought
        self.ma_filter  = ma_filter

    def analyze(self, df: pd.DataFrame) -> str:
        if len(df) < self.ma_filter + 5:
            return "HOLD"

        df = df.copy()
        low_min  = df["low"].rolling(self.k_period).min()
        high_max = df["high"].rolling(self.k_period).max()
        df["k"]  = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, 1e-9)
        df["d"]  = df["k"].rolling(self.d_period).mean()
        df["ma"] = df["close"].rolling(self.ma_filter).mean()
        df = df.dropna().reset_index(drop=True)
        if len(df) < 2:
            return "HOLD"

        prev = df.iloc[-2]
        curr = df.iloc[-1]

        in_uptrend  = curr["close"] > curr["ma"]
        bull_candle = curr["close"] > curr["open"]

        # ── BUY: sobrevenda + cruzamento %K acima %D em uptrend macro
        oversold_cross_up = (
            prev["k"] < self.oversold
            and prev["k"] <= prev["d"]
            and curr["k"] > curr["d"]
        )
        if in_uptrend and oversold_cross_up and bull_candle:
            return "BUY"

        # ── SELL: sobrecompra + cruzamento %K abaixo %D
        overbought_cross_dn = (
            prev["k"] > self.overbought
            and prev["k"] >= prev["d"]
            and curr["k"] < curr["d"]
        )
        if overbought_cross_dn:
            return "SELL"

        return "HOLD"
