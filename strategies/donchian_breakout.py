import pandas as pd
from .base import BaseStrategy


class DonchianBreakout(BaseStrategy):
    """
    Donchian Channel Breakout (estilo Turtle Traders) — 1H principal, confirmação 6H.

    BUY  → Candle FECHADO acima da banda superior (close confirmado, não apenas wick)
           + RVOL >= 1.5 (volume relativo acima da média — breakout real)
           + ADX > 20 (tendência confirmada — sem lateralização)
           + OBV em nova máxima (volume institucional acompanhando)
           + RSI(14) > 45 (momentum positivo)
    SELL → Close < menor low das últimas N barras (saída técnica)

    CLOSE-CONFIRMATION RULE:
      O sinal só é gerado no candle ANTERIOR ao atual (último candle fechado).
      O candle em formação (still-open) é descartado antes da análise.
      Isso garante que apenas fechamentos reais acima da banda geram entrada —
      nunca wicks intrabar que não sustentaram o preço ao fechar.

    RVOL Crescente (2–3 candles):
      Em vez de threshold fixo (>= 1.5), exige que o RVOL esteja ACELERANDO
      nos últimos 3 candles fechados — delta de volume positivo barra a barra.
      RVOL[n-2] < RVOL[n-1] < RVOL[n] → acumulação progressiva → breakout real.
      RVOL estagnado ou decrescente → smart money saindo → ignorar sinal.
    """

    def __init__(self, period: int = 20, rsi_period: int = 14,
                 rsi_min: float = 45.0, vol_mult: float = 1.0,
                 adx_period: int = 14, adx_min: float = 20.0,
                 obv_lookback: int = 5,
                 rvol_period: int = 20, rvol_lookback: int = 3):
        super().__init__("Donchian Breakout")
        self.period        = period
        self.rsi_period    = rsi_period
        self.rsi_min       = rsi_min
        self.vol_mult      = vol_mult
        self.adx_period    = adx_period
        self.adx_min       = adx_min
        self.obv_lookback  = obv_lookback
        self.rvol_period   = rvol_period    # barras para calcular o volume médio de referência
        self.rvol_lookback = rvol_lookback  # candles consecutivos com RVOL crescente exigidos

    # ── Indicadores internos ──────────────────────────────────────────────────

    def _rsi(self, series: pd.Series) -> pd.Series:
        delta = series.diff()
        gain  = delta.clip(lower=0).rolling(self.rsi_period).mean()
        loss  = (-delta.clip(upper=0)).rolling(self.rsi_period).mean()
        rs    = gain / loss.replace(0, float("inf"))
        return 100 - (100 / (1 + rs))

    def _adx(self, df: pd.DataFrame) -> float:
        """ADX — mede a força da tendência. > 20 = tendência presente."""
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

    def _rvol_rising(self, df: pd.DataFrame, lookback: int = 3) -> tuple:
        """
        RVOL Crescente — volume relativo deve estar ACELERANDO nos últimos candles.

        Em vez de um threshold fixo (>= 1.5), exige que o RVOL esteja subindo
        consistentemente nos últimos `lookback` candles (delta positivo).

        Lógica:
          Para cada um dos últimos `lookback` candles, calcula o RVOL individual
          (volume_candle / media_volume_N_periodos_anteriores).
          O breakout é válido somente se RVOL[i] > RVOL[i-1] para todos —
          ou seja, volume crescente barra a barra → confirmação de acumulação real.

        Retorna: (is_rising: bool, rvol_last: float)
          is_rising  → True se RVOL cresceu nos últimos `lookback` candles
          rvol_last  → valor do RVOL no último candle (para logging)
        """
        if len(df) < self.rvol_period + lookback + 2:
            return True, 1.0  # dados insuficientes → não bloqueia

        rvols = []
        for i in range(lookback, 0, -1):
            # Candle alvo: iloc[-(i)]
            # Média de volume: os N candles ANTES do candle alvo
            idx_end   = len(df) - i          # índice do candle alvo
            idx_start = idx_end - self.rvol_period
            if idx_start < 0:
                return True, 1.0
            vol_candle = float(df["volume"].iloc[idx_end])
            vol_avg    = float(df["volume"].iloc[idx_start:idx_end].mean())
            if vol_avg <= 0:
                return True, 1.0
            rvols.append(vol_candle / vol_avg)

        # RVOL crescente: cada valor maior que o anterior
        is_rising = all(rvols[i] > rvols[i - 1] for i in range(1, len(rvols)))
        return is_rising, rvols[-1]

    def _obv_at_new_high(self, df: pd.DataFrame) -> bool:
        """OBV em nova máxima — confirma volume institucional no breakout."""
        direction = df["close"].diff().apply(
            lambda x: 1 if x > 0 else (-1 if x < 0 else 0)
        )
        obv = (direction * df["volume"]).cumsum()
        obv_now  = float(obv.iloc[-1])
        obv_prev = float(obv.iloc[-(self.obv_lookback + 1)])
        return obv_now > obv_prev

    # ── Análise principal ─────────────────────────────────────────────────────

    def analyze(self, df: pd.DataFrame) -> str:
        min_bars = self.period + self.rsi_period + self.adx_period * 2 + self.rvol_period + 5
        if len(df) < min_bars + 1:   # +1 para garantir ao menos 1 candle fechado após o drop
            return "HOLD"

        df = df.copy()

        # ── CLOSE-CONFIRMATION: descarta o candle em formação (ainda não fechado) ──
        # iloc[-1] = candle atual (open, pode ter wick acima da banda sem fechar lá)
        # iloc[-2] = último candle CONFIRMADO (fechado) → usado para o sinal
        # Isso evita entradas baseadas em wicks intrabar que não sustentaram o close.
        df = df.iloc[:-1].reset_index(drop=True)

        df["dc_upper"] = df["high"].rolling(self.period).max().shift(1)
        df["dc_lower"] = df["low"].rolling(self.period).min().shift(1)
        df["rsi"]      = self._rsi(df["close"])
        df = df.dropna().reset_index(drop=True)
        if len(df) < self.obv_lookback + self.rvol_period + 2:
            return "HOLD"

        # curr = último candle FECHADO (após o drop do candle em formação)
        curr = df.iloc[-1]

        # ── BUY: fechamento real acima da banda — não apenas wick ─────────────
        # curr["close"] é o preço de fechamento do candle confirmado
        # curr["dc_upper"] é o máximo dos highs das N barras anteriores (shift=1)
        # → Somente closes verdadeiros acima da banda geram sinal
        price_breakout = curr["close"] > curr["dc_upper"]
        rsi_ok         = curr["rsi"] >= self.rsi_min

        if not (price_breakout and rsi_ok):
            # Checagem rápida antes de calcular indicadores pesados
            if curr["close"] < curr["dc_lower"]:
                return "SELL"
            return "HOLD"

        # ── RVOL Crescente: volume deve estar acelerando nos últimos 3 candles ──
        # Threshold fixo substituído por delta crescente — detecta acumulação real
        rvol_rising, rvol_last = self._rvol_rising(df, lookback=self.rvol_lookback)
        if not rvol_rising:
            return "HOLD"  # Volume estagnado/decrescente — smart money ausente

        # ── ADX: tendência presente ───────────────────────────────────────────
        adx_value = self._adx(df)
        if adx_value < self.adx_min:
            return "HOLD"  # Mercado lateral — risco de whipsaw

        # ── OBV: volume institucional acompanha ───────────────────────────────
        if not self._obv_at_new_high(df):
            return "HOLD"  # Breakout sem OBV crescente — distribuição, não acumulação

        return "BUY"
