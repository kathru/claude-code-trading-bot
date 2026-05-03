import pandas as pd
from .base import BaseStrategy


class VolatilityGuard(BaseStrategy):
    """
    Monitor de volatilidade — candles diários (ONE_DAY).
    Se a volatilidade diária superar 8% por 3 dias consecutivos,
    sinaliza SELL para reduzir exposição e mover para estáveis.

    SELL → 3+ dias consecutivos com variação > threshold (padrão 8%).
    HOLD → volatilidade normal.
    """

    def __init__(self, threshold_pct: float = 8.0, consecutive_days: int = 3):
        super().__init__("Vol Guard")
        self.threshold   = threshold_pct / 100
        self.min_days    = consecutive_days

    def analyze(self, df: pd.DataFrame) -> str:
        if len(df) < self.min_days + 1:
            return "HOLD"

        recent = df.tail(self.min_days + 1).copy()
        recent["pct_change"] = recent["close"].pct_change().abs()
        recent = recent.dropna()

        high_vol_days = (recent["pct_change"] >= self.threshold).sum()

        if high_vol_days >= self.min_days:
            return "SELL"   # reduzir posição — volatilidade extrema
        return "HOLD"
