import pandas as pd
from .base import BaseStrategy


class StochBounce(BaseStrategy):
    """
    Stochastic Oversold Bounce — 30min candles.
    Caça exaustões de venda em mercados com volatilidade estável.

    BUY  → Stoch %K < 20 (sobrevenda extrema) E %K cruza acima de %D
           E vela verde (confirmação de força)
           E close > EMA50 no 30min (tendência macro positiva)
           E Bollinger Bandwidth estável (não em modo pânico/crash)
    SELL → Stoch %K > 75 E %K cruza abaixo de %D (sobrecompra)

    Melhorias v2:
      - Bollinger Bandwidth: filtra períodos de volatilidade extrema (crashes)
        O stoch perde a precisão quando as bandas estão explodindo
      - EMA200 no timeframe maior (6H) passada via app.py como filtro externo
      - Oversold mais restrito (20 vs 25): só entra em sobrevendas extremas
      - Overbought mais conservador (75 vs 80): sai mais cedo
    """

    def __init__(self, k_period: int = 14, d_period: int = 3,
                 oversold: float = 20.0, overbought: float = 75.0,
                 ma_filter: int = 50,
                 bb_period: int = 20, bb_mult: float = 2.0,
                 bb_bandwidth_max: float = 0.15):
        super().__init__("Stoch Bounce")
        self.k_period         = k_period
        self.d_period         = d_period
        self.oversold         = oversold
        self.overbought       = overbought
        self.ma_filter        = ma_filter
        self.bb_period        = bb_period
        self.bb_mult          = bb_mult
        self.bb_bandwidth_max = bb_bandwidth_max  # máx bandwidth tolerada (15%)

    def _bollinger_bandwidth(self, close: pd.Series) -> float:
        """
        Bollinger Bandwidth = (Upper - Lower) / Middle.
        Valor alto (> bb_bandwidth_max) = volatilidade em expansão (pânico/euforia).
        Valor baixo = mercado em canal — mean reversion tem alta precisão.
        """
        mid   = close.rolling(self.bb_period).mean()
        std   = close.rolling(self.bb_period).std()
        upper = mid + self.bb_mult * std
        lower = mid - self.bb_mult * std
        bw    = (upper - lower) / mid.replace(0, 1e-9)
        return float(bw.iloc[-1])

    def analyze(self, df: pd.DataFrame) -> str:
        min_candles = max(self.k_period + self.d_period + 2,
                          self.bb_period + 5,
                          self.ma_filter + 5)
        if len(df) < min_candles:
            return "HOLD"

        df = df.copy()
        low_min  = df["low"].rolling(self.k_period).min()
        high_max = df["high"].rolling(self.k_period).max()
        df["k"]     = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, 1e-9)
        df["d"]     = df["k"].rolling(self.d_period).mean()
        df["ema50"] = df["close"].ewm(span=self.ma_filter, adjust=False).mean()
        df = df.dropna().reset_index(drop=True)
        if len(df) < 2:
            return "HOLD"

        prev = df.iloc[-2]
        curr = df.iloc[-1]

        bull_candle = curr["close"] > curr["open"]
        above_ema50 = curr["close"] > curr["ema50"]  # tendência macro positiva no 30min

        # ── Filtro Bollinger Bandwidth: não operar em modo de pânico ─────────
        # Quando as bandas estão explodindo, o mercado está em queda livre ou euforia.
        # O estocástico lê "sobrevenda" mas o preço continua caindo → bull trap.
        bw = self._bollinger_bandwidth(df["close"])
        volatility_stable = bw < self.bb_bandwidth_max

        # ── BUY: sobrevenda extrema + cruzamento + confirmações ──────────────
        oversold_cross_up = (
            prev["k"] < self.oversold
            and prev["k"] <= prev["d"]
            and curr["k"] > curr["d"]
        )

        if oversold_cross_up and bull_candle and above_ema50 and volatility_stable:
            return "BUY"

        # ── SELL: sobrecompra + cruzamento %K abaixo %D ──────────────────────
        overbought_cross_dn = (
            prev["k"] > self.overbought
            and prev["k"] >= prev["d"]
            and curr["k"] < curr["d"]
        )
        if overbought_cross_dn:
            return "SELL"

        return "HOLD"
