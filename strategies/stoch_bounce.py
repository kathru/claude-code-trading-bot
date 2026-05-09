import pandas as pd
from .base import BaseStrategy


class StochBounce(BaseStrategy):
    """
    Stochastic Oversold Bounce — 30min candles.
    Mean reversion pura — opera em qualquer regime de mercado.

    BUY  → Stoch %K < oversold (sobrevenda) E %K cruza acima de %D (reversão)
           E vela atual é verde (confirmação de força)
    SELL → Stoch %K > overbought E %K cruza abaixo de %D (sobrecompra)

    Edge: pega quicadas de sobrevenda com SL como proteção.
    Sem filtro de tendência macro — mean reversion funciona em qualquer direção.
    """

    def __init__(self, k_period: int = 14, d_period: int = 3,
                 oversold: float = 25.0, overbought: float = 80.0,
                 ma_filter: int = 50):
        super().__init__("Stoch Bounce")
        self.k_period   = k_period
        self.d_period   = d_period
        self.oversold   = oversold
        self.overbought = overbought
        self.ma_filter  = ma_filter  # mantido por compatibilidade mas não usado como filtro

    def analyze(self, df: pd.DataFrame) -> str:
        min_candles = self.k_period + self.d_period + 2
        if len(df) < min_candles:
            return "HOLD"

        df = df.copy()
        low_min  = df["low"].rolling(self.k_period).min()
        high_max = df["high"].rolling(self.k_period).max()
        df["k"]  = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, 1e-9)
        df["d"]  = df["k"].rolling(self.d_period).mean()
        df["ema50"] = df["close"].ewm(span=self.ma_filter, adjust=False).mean()
        df = df.dropna().reset_index(drop=True)
        if len(df) < 2:
            return "HOLD"

        prev = df.iloc[-2]
        curr = df.iloc[-1]

        bull_candle = curr["close"] > curr["open"]
        above_ema50 = curr["close"] > curr["ema50"]  # filtro de tendência macro

        # ── BUY: sobrevenda + cruzamento %K acima %D + vela verde + tendência macro positiva
        oversold_cross_up = (
            prev["k"] < self.oversold
            and prev["k"] <= prev["d"]
            and curr["k"] > curr["d"]
        )
        if oversold_cross_up and bull_candle and above_ema50:
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
