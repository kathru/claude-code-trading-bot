import pandas as pd
from .base import BaseStrategy


class MACDMomentum(BaseStrategy):
    """
    MACD Momentum Surge — 1H candles.

    BUY  → Histograma cruza de negativo para positivo
           E close > EMA50 (acima da média de longo prazo)
           E momentum (close > close 3 barras atrás)
    SELL → Histograma cruza de positivo para negativo

    Edge: identifica reversões de momentum CEDO,
    muito antes do Golden Cross. Excelente para crypto.
    """

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9,
                 ema_filter: int = 20):
        super().__init__("MACD Momentum")
        self.fast       = fast
        self.slow       = slow
        self.signal     = signal
        self.ema_filter = ema_filter

    def analyze(self, df: pd.DataFrame) -> str:
        if len(df) < self.slow + self.signal + self.ema_filter + 5:
            return "HOLD"

        df = df.copy()
        ema_fast = df["close"].ewm(span=self.fast,   adjust=False).mean()
        ema_slow = df["close"].ewm(span=self.slow,   adjust=False).mean()
        macd     = ema_fast - ema_slow
        sig_line = macd.ewm(span=self.signal, adjust=False).mean()
        df["hist"]    = macd - sig_line
        df["macd"]    = macd
        df["ema_flt"] = df["close"].ewm(span=self.ema_filter, adjust=False).mean()
        df = df.dropna().reset_index(drop=True)
        if len(df) < 4:
            return "HOLD"

        prev = df.iloc[-2]
        curr = df.iloc[-1]

        # ── BUY: cruzamento de histograma + filtro de tendência + momentum
        hist_cross_up = prev["hist"] <= 0 and curr["hist"] > 0
        above_filter  = curr["close"] > curr["ema_flt"]
        momentum_pos  = curr["close"] > df["close"].iloc[-4]
        if hist_cross_up and above_filter and momentum_pos:
            return "BUY"

        # ── SELL: cruzamento de histograma para baixo
        hist_cross_dn = prev["hist"] >= 0 and curr["hist"] < 0
        if hist_cross_dn:
            return "SELL"

        return "HOLD"
