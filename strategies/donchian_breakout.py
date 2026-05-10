import pandas as pd
from .base import BaseStrategy


class DonchianBreakout(BaseStrategy):
    """
    Donchian Channel Breakout (estilo Turtle Traders) — 30min.

    BUY  → Close > maior high das últimas N barras (rompimento)
           + ADX > 25 (tendência forte confirmada — sem breakouts falsos)
           + OBV em nova máxima (volume institucional confirmando o movimento)
           + RSI(14) > 55 (momentum positivo)
    SELL → Close < menor low das últimas N barras (saída técnica)

    Melhorias v2:
      - ADX > 25: filtra mercados laterais onde Donchian gera bull traps
      - OBV nova máxima: exige que volume acompanhe o preço (breakout real)
    """

    def __init__(self, period: int = 20, rsi_period: int = 14,
                 rsi_min: float = 55.0, vol_mult: float = 1.5,
                 adx_period: int = 14, adx_min: float = 25.0,
                 obv_lookback: int = 10):
        super().__init__("Donchian Breakout")
        self.period      = period
        self.rsi_period  = rsi_period
        self.rsi_min     = rsi_min
        self.vol_mult    = vol_mult
        self.adx_period  = adx_period
        self.adx_min     = adx_min
        self.obv_lookback = obv_lookback

    # ── Indicadores internos ──────────────────────────────────────────────────

    def _rsi(self, series: pd.Series) -> pd.Series:
        delta = series.diff()
        gain  = delta.clip(lower=0).rolling(self.rsi_period).mean()
        loss  = (-delta.clip(upper=0)).rolling(self.rsi_period).mean()
        rs    = gain / loss.replace(0, float("inf"))
        return 100 - (100 / (1 + rs))

    def _adx(self, df: pd.DataFrame) -> float:
        """ADX (Average Directional Index) — mede força da tendência.
        ADX > 25 → tendência forte (breakouts têm maior probabilidade de sucesso).
        ADX < 20 → mercado lateral (ignorar breakouts — alta chance de bull trap).
        """
        p = self.adx_period
        high, low, close = df["high"], df["low"], df["close"]

        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low  - close.shift(1)).abs()
        ], axis=1).max(axis=1)

        dm_plus  = (high - high.shift(1)).clip(lower=0)
        dm_minus = (low.shift(1) - low).clip(lower=0)
        dm_plus  = dm_plus.where(dm_plus > dm_minus, 0.0)
        dm_minus = dm_minus.where(dm_minus > dm_plus, 0.0)

        atr_s    = tr.ewm(alpha=1 / p, adjust=False).mean()
        di_plus  = 100 * dm_plus.ewm(alpha=1 / p, adjust=False).mean() / atr_s.replace(0, 1e-9)
        di_minus = 100 * dm_minus.ewm(alpha=1 / p, adjust=False).mean() / atr_s.replace(0, 1e-9)
        dx       = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, 1e-9)
        adx      = dx.ewm(alpha=1 / p, adjust=False).mean()
        return float(adx.iloc[-1])

    def _obv_at_new_high(self, df: pd.DataFrame) -> bool:
        """OBV em nova máxima nas últimas N barras.
        Confirma que volume institucional está acompanhando o breakout de preço.
        Breakout seco (sem OBV subindo) = alta probabilidade de falso rompimento.
        """
        direction = df["close"].diff().apply(
            lambda x: 1 if x > 0 else (-1 if x < 0 else 0)
        )
        obv = (direction * df["volume"]).cumsum()
        obv_now  = float(obv.iloc[-1])
        obv_prev = float(obv.iloc[-(self.obv_lookback + 1)])
        return obv_now > obv_prev

    # ── Análise principal ─────────────────────────────────────────────────────

    def analyze(self, df: pd.DataFrame) -> str:
        min_bars = self.period + self.rsi_period + self.adx_period * 2 + 5
        if len(df) < min_bars:
            return "HOLD"

        df = df.copy()
        df["dc_upper"] = df["high"].rolling(self.period).max().shift(1)
        df["dc_lower"] = df["low"].rolling(self.period).min().shift(1)
        df["rsi"]      = self._rsi(df["close"])
        df["vol_ma"]   = df["volume"].rolling(self.period).mean()
        df = df.dropna().reset_index(drop=True)
        if len(df) < self.obv_lookback + 5:
            return "HOLD"

        curr = df.iloc[-1]

        # ── BUY: rompimento de máxima
        price_breakout = curr["close"] > curr["dc_upper"]
        rsi_ok         = curr["rsi"] >= self.rsi_min
        vol_ok         = curr["volume"] >= curr["vol_ma"] * self.vol_mult

        if price_breakout and rsi_ok and vol_ok:
            # Filtro ADX: só entra se há tendência forte (ADX > 25)
            adx_value = self._adx(df)
            if adx_value < self.adx_min:
                return "HOLD"  # Mercado lateral — breakout provavelmente falso

            # Filtro OBV: volume deve confirmar o rompimento
            if not self._obv_at_new_high(df):
                return "HOLD"  # Breakout "seco" — sem volume institucional

            return "BUY"

        # ── SELL técnico: rompimento de mínima
        if curr["close"] < curr["dc_lower"]:
            return "SELL"

        return "HOLD"
