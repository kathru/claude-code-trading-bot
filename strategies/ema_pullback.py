import pandas as pd
from .base import BaseStrategy


class EMAPullback(BaseStrategy):
    """
    EMA Pullback Trend — 1H candles.
    Compra pullback ao EMA21 dentro de tendência confirmada.

    BUY  → EMA9 > EMA21 > EMA50 (uptrend)
           + low tocou EMA21 (pullback real)
           + close >= EMA21 (rejeição/recompra)
           + vela verde
           + EMA50 com inclinação positiva (tendência macro subindo)
           + Volume baixo no pullback + alto na retomada (realização saudável)
    SELL → EMA9 cruzou abaixo EMA21 por 2 velas consecutivas (anti-whipsaw)

    Melhorias v2:
      - Slope EMA50: filtra pullbacks em tendências "flat" ou revertendo
      - Volume filter: pullback com volume alto = distribuição (sair)
                       retomada com volume alto = acumulação (entrar)
    """

    def __init__(self, fast: int = 9, mid: int = 21, slow: int = 50,
                 touch_tolerance_pct: float = 0.3,
                 slope_bars: int = 5,
                 vol_pullback_mult: float = 0.8,
                 vol_breakout_mult: float = 1.2):
        super().__init__("EMA Pullback")
        self.fast             = fast
        self.mid              = mid
        self.slow             = slow
        self.tol              = touch_tolerance_pct / 100.0
        self.slope_bars       = slope_bars         # barras para medir inclinação da EMA50
        self.vol_pullback_mult = vol_pullback_mult  # pullback deve ter volume < média × mult
        self.vol_breakout_mult = vol_breakout_mult  # retomada deve ter volume > média × mult

    def analyze(self, df: pd.DataFrame) -> str:
        min_bars = self.slow + self.slope_bars + 10
        if len(df) < min_bars:
            return "HOLD"

        df = df.copy()
        df["ema_f"]  = df["close"].ewm(span=self.fast, adjust=False).mean()
        df["ema_m"]  = df["close"].ewm(span=self.mid,  adjust=False).mean()
        df["ema_s"]  = df["close"].ewm(span=self.slow, adjust=False).mean()
        df["vol_ma"] = df["volume"].rolling(20).mean()
        df = df.dropna().reset_index(drop=True)
        if len(df) < self.slope_bars + 3:
            return "HOLD"

        prev  = df.iloc[-2]
        curr  = df.iloc[-1]

        # ── Condições de tendência ────────────────────────────────────────────
        in_uptrend   = curr["ema_f"] > curr["ema_m"] > curr["ema_s"]

        # Inclinação da EMA50: deve estar subindo (slope positivo nas últimas N barras)
        # Se a EMA50 estiver "flat" ou caindo, o pullback não tem força por trás
        ema50_now    = float(curr["ema_s"])
        ema50_prev   = float(df["ema_s"].iloc[-(self.slope_bars + 1)])
        ema50_slope  = (ema50_now - ema50_prev) / ema50_prev  # variação % em N barras
        slope_ok     = ema50_slope > 0.0005  # EMA50 subindo pelo menos 0.05% em slope_bars barras

        # ── Condições de pullback ─────────────────────────────────────────────
        touched_ema21 = curr["low"] <= curr["ema_m"] * (1 + self.tol)
        reclaimed     = curr["close"] >= curr["ema_m"]
        bull_candle   = curr["close"] > curr["open"]

        # ── Filtro de volume ─────────────────────────────────────────────────
        vol_ma = float(curr["vol_ma"]) if float(curr["vol_ma"]) > 0 else 1.0

        # Pullback saudável: volume BAIXO na vela de pullback (realização, não pânico)
        pullback_vol_ok = float(prev["volume"]) < vol_ma * self.vol_pullback_mult

        # Retomada forte: volume ALTO na vela verde de recompra
        breakout_vol_ok = float(curr["volume"]) > vol_ma * self.vol_breakout_mult

        # ── Sinal BUY ────────────────────────────────────────────────────────
        if (in_uptrend and slope_ok and touched_ema21
                and reclaimed and bull_candle
                and pullback_vol_ok and breakout_vol_ok):
            return "BUY"

        # ── SELL: EMA9 abaixo da EMA21 por 2 velas consecutivas (anti-whipsaw)
        if len(df) >= 3:
            prev2 = df.iloc[-3]
            two_candle_cross = (
                prev2["ema_f"] >= prev2["ema_m"]
                and prev["ema_f"]  < prev["ema_m"]
                and curr["ema_f"]  < curr["ema_m"]
            )
            if two_candle_cross:
                return "SELL"

        return "HOLD"
