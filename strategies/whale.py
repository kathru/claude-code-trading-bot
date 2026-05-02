import pandas as pd
from .base import BaseStrategy


class WhaleStrategy(BaseStrategy):
    """
    Detecta ordens anormalmente grandes no order book (whale walls).
    Uma ordem é considerada "whale" se for >= whale_multiplier * média das ordens.
    BUY  → whales acumulando (bid wall dominante)
    SELL → whales distribuindo (ask wall dominante)
    """

    def __init__(self, whale_multiplier: float = 5.0, top_levels: int = 50, dominance_ratio: float = 1.5):
        super().__init__("Whale")
        self.whale_multiplier = whale_multiplier
        self.top_levels = top_levels
        self.dominance_ratio = dominance_ratio
        self.last_whale_bid_usd = 0.0
        self.last_whale_ask_usd = 0.0

    def analyze(self, df: pd.DataFrame) -> str:
        # analyze() exigido pela base mas não usado — usamos analyze_book()
        return "HOLD"

    def analyze_book(self, order_book: dict) -> str:
        bids = order_book.get("bids", [])
        asks = order_book.get("asks", [])

        if not bids or not asks:
            return "HOLD"

        bids = bids[:self.top_levels]
        asks = asks[:self.top_levels]

        def usd_sizes(orders):
            result = []
            for o in orders:
                try:
                    result.append(float(o["price"]) * float(o["size"]))
                except Exception:
                    pass
            return result

        bid_usd = usd_sizes(bids)
        ask_usd = usd_sizes(asks)

        if not bid_usd or not ask_usd:
            return "HOLD"

        avg_bid = sum(bid_usd) / len(bid_usd)
        avg_ask = sum(ask_usd) / len(ask_usd)

        # Soma USD de ordens whale nas primeiras 10 posições
        whale_bid = sum(v for v in bid_usd[:10] if v >= avg_bid * self.whale_multiplier)
        whale_ask = sum(v for v in ask_usd[:10] if v >= avg_ask * self.whale_multiplier)

        self.last_whale_bid_usd = whale_bid
        self.last_whale_ask_usd = whale_ask

        if whale_bid == 0 and whale_ask == 0:
            return "HOLD"

        if whale_bid > whale_ask * self.dominance_ratio:
            return "BUY"
        if whale_ask > whale_bid * self.dominance_ratio:
            return "SELL"

        return "HOLD"
