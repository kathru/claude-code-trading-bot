import pandas as pd
from .base import BaseStrategy


class RSIDivergenceDetector(BaseStrategy):
    """
    Detector de Divergência RSI — usado como FILTRO em outras estratégias.

    Divergência Altista:
    ├─ Preço faz mínima mais baixa
    └─ RSI faz mínima mais alta → Sintetiza reversal altista

    Divergência Baixista:
    ├─ Preço faz máxima mais alta
    └─ RSI faz máxima mais baixa → Sintetiza reversal baixista

    Edge: Detecta reversões ANTES de confirmação técnica (Donchian, EMA, etc)
    Uso: Confirmar sinais de entrada em outras estratégias
    """

    def __init__(self, period: int = 14, lookback_periods: int = 5):
        super().__init__("RSI Divergence")
        self.period = period
        self.lookback = lookback_periods

    def _rsi(self, series: pd.Series) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0).rolling(self.period).mean()
        loss = (-delta.clip(upper=0)).rolling(self.period).mean()
        rs = gain / loss.replace(0, float("inf"))
        return 100 - (100 / (1 + rs))

    def analyze(self, df: pd.DataFrame) -> str:
        """
        Retorna:
        - "BULLISH_DIV": Divergência altista detectada (confirma BUY)
        - "BEARISH_DIV": Divergência baixista detectada (confirma SELL)
        - "HOLD": Sem divergência
        """
        if len(df) < self.period + self.lookback + 5:
            return "HOLD"

        df = df.copy()
        df["rsi"] = self._rsi(df["close"])
        df = df.dropna().reset_index(drop=True)

        if len(df) < self.lookback + 2:
            return "HOLD"

        # Pegar últimos N períodos para comparação
        recent = df.tail(self.lookback + 1)
        recent_idx = df.index[-self.lookback - 1:].tolist()

        # ── Divergência Altista: preço baixo, RSI alto
        price_min_idx = recent["low"].idxmin()
        rsi_values = recent["rsi"].values
        rsi_min_idx = rsi_values.argmin()

        if price_min_idx > rsi_min_idx:
            # Preço caiu depois (mínima mais recente é mais baixa)
            # mas RSI não caiu tanto (mínima mais recente é mais alta)
            price_current = df["low"].iloc[-1]
            price_prev = df["low"].iloc[price_min_idx]
            rsi_current = df["rsi"].iloc[-1]
            rsi_prev = df["rsi"].iloc[rsi_min_idx]

            if price_current < price_prev and rsi_current > rsi_prev:
                return "BULLISH_DIV"

        # ── Divergência Baixista: preço alto, RSI baixo
        price_max_idx = recent["high"].idxmax()
        rsi_max_idx = rsi_values.argmax()

        if price_max_idx > rsi_max_idx:
            # Preço subiu depois (máxima mais recente é mais alta)
            # mas RSI não subiu tanto (máxima mais recente é mais baixa)
            price_current = df["high"].iloc[-1]
            price_prev = df["high"].iloc[price_max_idx]
            rsi_current = df["rsi"].iloc[-1]
            rsi_prev = df["rsi"].iloc[rsi_max_idx]

            if price_current > price_prev and rsi_current < rsi_prev:
                return "BEARISH_DIV"

        return "HOLD"
