import pandas as pd
from .base import BaseStrategy


class GoldenCross(BaseStrategy):
    """
    Golden Cross / Death Cross — candles diários (ONE_DAY).
    BUY  → MA50 cruza acima da MA200 (Golden Cross) — sinal forte de alta.
    SELL → MA50 cruza abaixo da MA200 (Death Cross) — sinal forte de baixa.
    HOLD → Sem cruzamento; mantém posição existente.

    Nota: só sinaliza BUY/SELL no momento do cruzamento.
    Raro mas muito confiável — crypto tende a fazer grandes movimentos após Golden Cross.
    """

    def __init__(self, short: int = 50, long: int = 200):
        super().__init__("Golden Cross")
        self.short = short
        self.long  = long

    def analyze(self, df: pd.DataFrame) -> str:
        if len(df) < self.long + 2:
            return "HOLD"

        df = df.copy()
        df["ma_short"] = df["close"].rolling(self.short).mean()
        df["ma_long"]  = df["close"].rolling(self.long).mean()
        df = df.dropna().reset_index(drop=True)

        if len(df) < 2:
            return "HOLD"

        prev = df.iloc[-2]
        curr = df.iloc[-1]

        # Golden Cross: MA50 cruza acima da MA200
        if prev["ma_short"] <= prev["ma_long"] and curr["ma_short"] > curr["ma_long"]:
            return "BUY"

        # Death Cross: MA50 cruza abaixo da MA200
        if prev["ma_short"] >= prev["ma_long"] and curr["ma_short"] < curr["ma_long"]:
            return "SELL"

        # MA50 > MA200 (uptrend) mas sem cruzamento recente — sinal fraco de continuação
        if curr["ma_short"] > curr["ma_long"]:
            # Só retorna BUY se MA50 subindo (momentum positivo)
            ma_momentum = curr["ma_short"] - prev["ma_short"]
            if ma_momentum > 0:
                return "BUY"

        return "HOLD"
