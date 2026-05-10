import pandas as pd
from .base import BaseStrategy


class MACDMomentum(BaseStrategy):
    """
    MACD Momentum Surge — 1H candles.

    BUY  → Histograma cruza de negativo para positivo (impulso inicial)
           + Linha MACD acima de zero (tendência de médio prazo já virou)
           + close > EMA20 (preço acima filtro de curto prazo)
           + momentum positivo (close > close 3 barras atrás)
           + RSI < 70 (não sobrecomprado — evita topo de momentum)
    SELL → Histograma cruza de positivo para negativo

    Melhorias v2:
      - MACD line > 0: garante que a tendência de médio prazo já virou para alta,
        não apenas um salto temporário do histograma
      - RSI < 70: evita comprar no "topo do momentum" quando RSI já está esticado
    """

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9,
                 ema_filter: int = 20,
                 rsi_period: int = 14,
                 rsi_max: float = 70.0):
        super().__init__("MACD Momentum")
        self.fast       = fast
        self.slow       = slow
        self.signal     = signal
        self.ema_filter = ema_filter
        self.rsi_period = rsi_period
        self.rsi_max    = rsi_max

    def _rsi(self, series: pd.Series) -> pd.Series:
        delta = series.diff()
        gain  = delta.clip(lower=0).rolling(self.rsi_period).mean()
        loss  = (-delta.clip(upper=0)).rolling(self.rsi_period).mean()
        rs    = gain / loss.replace(0, float("inf"))
        return 100 - (100 / (1 + rs))

    def analyze(self, df: pd.DataFrame) -> str:
        min_bars = self.slow + self.signal + max(self.ema_filter, self.rsi_period) + 5
        if len(df) < min_bars:
            return "HOLD"

        df = df.copy()
        ema_fast     = df["close"].ewm(span=self.fast,   adjust=False).mean()
        ema_slow     = df["close"].ewm(span=self.slow,   adjust=False).mean()
        macd_line    = ema_fast - ema_slow
        sig_line     = macd_line.ewm(span=self.signal, adjust=False).mean()
        df["hist"]   = macd_line - sig_line
        df["macd"]   = macd_line           # linha MACD (não apenas o histograma)
        df["ema_flt"] = df["close"].ewm(span=self.ema_filter, adjust=False).mean()
        df["rsi"]    = self._rsi(df["close"])
        df = df.dropna().reset_index(drop=True)
        if len(df) < 4:
            return "HOLD"

        prev = df.iloc[-2]
        curr = df.iloc[-1]

        # ── BUY ──────────────────────────────────────────────────────────────

        # 1. Histograma cruzou de negativo para positivo (impulso se iniciando)
        hist_cross_up = prev["hist"] <= 0 and curr["hist"] > 0

        # 2. Linha MACD acima de zero (tendência de médio prazo já virou para alta)
        #    Diferencial principal v2: sem isso compramos no "bounce" de baixa
        macd_above_zero = curr["macd"] > 0

        # 3. Preço acima do filtro de curto prazo (EMA20)
        above_filter = curr["close"] > curr["ema_flt"]

        # 4. Momentum: preço maior que 3 barras atrás
        momentum_pos = curr["close"] > df["close"].iloc[-4]

        # 5. RSI não sobrecomprado (< 70): evita comprar no topo do momentum
        rsi_ok = curr["rsi"] < self.rsi_max

        if hist_cross_up and macd_above_zero and above_filter and momentum_pos and rsi_ok:
            return "BUY"

        # ── SELL: histograma cruzou de positivo para negativo ────────────────
        hist_cross_dn = prev["hist"] >= 0 and curr["hist"] < 0
        if hist_cross_dn:
            return "SELL"

        return "HOLD"
