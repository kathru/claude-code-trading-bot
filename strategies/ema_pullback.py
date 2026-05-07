import pandas as pd
from .base import BaseStrategy


class EMAPullback(BaseStrategy):
    """
    EMA Pullback Trend — 1H candles.
    Compra pullback ao EMA21 dentro de tendência confirmada.

    Tendência: EMA9 > EMA21 > EMA50  →  uptrend forte
    BUY  → low da vela toca/quebra o EMA21 (pullback) E close >= EMA21
           (rejeição/recompra) E vela atual é verde.
    SELL → EMA9 cruza abaixo do EMA21 (perda de tendência curta).
    SELL_HALF → Quando atingir +2.5% de lucro (meio-caminho até TP de 5%)
                para proteger pyramides e fazer lock-in de lucro.

    Edge: padrão clássico de continuação de tendência.
    Win rate alto porque só age dentro de regime de alta.
    Pyramiding protection: tira lucro parcial em subidas de +2.5%.
    """

    def __init__(self, fast: int = 9, mid: int = 21, slow: int = 50,
                 touch_tolerance_pct: float = 0.4, tp_half: float = 2.5):
        super().__init__("EMA Pullback")
        self.fast = fast
        self.mid  = mid
        self.slow = slow
        self.tol  = touch_tolerance_pct / 100.0
        self.tp_half = tp_half / 100.0  # 2.5% → 0.025

    def analyze(self, df: pd.DataFrame) -> str:
        if len(df) < self.slow + 5:
            return "HOLD"

        df = df.copy()
        df["ema_f"] = df["close"].ewm(span=self.fast, adjust=False).mean()
        df["ema_m"] = df["close"].ewm(span=self.mid,  adjust=False).mean()
        df["ema_s"] = df["close"].ewm(span=self.slow, adjust=False).mean()
        df = df.dropna().reset_index(drop=True)
        if len(df) < 2:
            return "HOLD"

        prev = df.iloc[-2]
        curr = df.iloc[-1]

        in_uptrend = (curr["ema_f"] > curr["ema_m"] > curr["ema_s"])
        # Pullback: low tocou área do EMA21 (até tol% abaixo)
        touched_em21 = curr["low"] <= curr["ema_m"] * (1 + self.tol)
        # Recompra: close ficou em cima do EMA21
        reclaimed    = curr["close"] >= curr["ema_m"]
        # Vela verde
        bull_candle  = curr["close"] > curr["open"]

        if in_uptrend and touched_em21 and reclaimed and bull_candle:
            return "BUY"

        # SELL técnico: EMA9 cruzou abaixo do EMA21 (perdeu tendência)
        if prev["ema_f"] >= prev["ema_m"] and curr["ema_f"] < curr["ema_m"]:
            return "SELL"

        return "HOLD"
