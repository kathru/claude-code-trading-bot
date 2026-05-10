import pandas as pd
from datetime import datetime

class BacktestEngine:
    def __init__(self, initial_balance: float = 5000.0, taker_fee: float = 0.006):
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.holdings = {}
        self.entry_prices = {}
        self.trades = []
        self.prices = {}
        self.total_fees = 0.0
        self.current_time = None
        self.taker_fee = taker_fee

    def update_price(self, symbol: str, price: float, time: datetime):
        self.prices[symbol] = price
        self.current_time = time

    def buy(self, symbol: str, amount: float, price: float, strategy: str) -> bool:
        fee = amount * self.taker_fee
        total_cost = amount + fee
        if total_cost > self.balance:
            return False

        qty = amount / price
        prev_qty = self.holdings.get(symbol, 0.0)
        prev_entry = self.entry_prices.get(symbol, 0.0)

        total_qty = prev_qty + qty
        self.entry_prices[symbol] = ((prev_qty * prev_entry) + (qty * price)) / total_qty

        self.balance -= total_cost
        self.total_fees += fee
        self.holdings[symbol] = total_qty

        self._log_trade("BUY", symbol, qty, price, amount, strategy, fee)
        return True

    def sell(self, symbol: str, qty: float, price: float, strategy: str) -> bool:
        held = self.holdings.get(symbol, 0.0)
        qty = min(qty, held)
        if qty <= 1e-10:
            return False

        gross = qty * price
        fee = gross * self.taker_fee
        net_received = gross - fee

        self.holdings[symbol] = held - qty
        if self.holdings[symbol] < 1e-10:
            del self.holdings[symbol]
            # We don't necessarily delete entry_price here if we want to calculate PnL per trade later
            # but for consistency with PaperTradingEngine:
            del self.entry_prices[symbol]

        self.balance += net_received
        self.total_fees += fee

        self._log_trade("SELL", symbol, qty, price, net_received, strategy, fee)
        return True

    def _log_trade(self, side: str, symbol: str, qty: float, price: float, amount: float, strategy: str, fee: float):
        self.trades.append({
            "time": self.current_time.isoformat() if self.current_time else None,
            "side": side,
            "symbol": symbol,
            "qty": qty,
            "price": price,
            "amount": amount,
            "fee": fee,
            "strategy": strategy
        })

    def portfolio_value(self) -> float:
        total = self.balance
        for symbol, qty in self.holdings.items():
            total += qty * self.prices.get(symbol, 0.0)
        return total

    def get_metrics(self):
        df_trades = pd.DataFrame(self.trades)
        if df_trades.empty:
            return {
                "pnl": 0.0,
                "pnl_pct": 0.0,
                "trade_count": 0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "final_value": self.portfolio_value()
            }

        # Calculate PnL per completed trade (BUY then SELL)
        # Simplified: look at all SELL trades and find their corresponding BUY cost
        # Or just calculate total PnL from initial vs final

        total_pnl = self.portfolio_value() - self.initial_balance
        pnl_pct = (total_pnl / self.initial_balance) * 100

        # Win Rate and Profit Factor logic
        # We need to pair BUYs and SELLs. For simplicity, we'll track PnL of each SELL.
        sell_trades = []
        # This is a bit complex for multi-buy/multi-sell, so let's use a simpler approach:
        # Group by symbol and track realized PnL.

        realized_pnls = []
        temp_holdings = {} # symbol -> list of (qty, price)

        for t in self.trades:
            symbol = t["symbol"]
            if t["side"] == "BUY":
                if symbol not in temp_holdings:
                    temp_holdings[symbol] = []
                temp_holdings[symbol].append({"qty": t["qty"], "price": t["price"], "fee": t["fee"]})
            else:
                # SELL
                qty_to_sell = t["qty"]
                sell_price = t["price"]
                sell_fee = t["fee"]

                cost_basis = 0.0
                buy_fees_proportion = 0.0

                # FIFO
                while qty_to_sell > 0 and symbol in temp_holdings and temp_holdings[symbol]:
                    buy_lot = temp_holdings[symbol][0]
                    if buy_lot["qty"] <= qty_to_sell:
                        cost_basis += buy_lot["qty"] * buy_lot["price"]
                        buy_fees_proportion += buy_lot["fee"]
                        qty_to_sell -= buy_lot["qty"]
                        temp_holdings[symbol].pop(0)
                    else:
                        proportion = qty_to_sell / buy_lot["qty"]
                        cost_basis += qty_to_sell * buy_lot["price"]
                        buy_fees_proportion += buy_lot["fee"] * proportion
                        buy_lot["qty"] -= qty_to_sell
                        buy_lot["fee"] -= buy_lot["fee"] * proportion
                        qty_to_sell = 0

                pnl = (t["qty"] * sell_price) - cost_basis - sell_fee - buy_fees_proportion
                realized_pnls.append(pnl)

        wins = [p for p in realized_pnls if p > 0]
        losses = [p for p in realized_pnls if p <= 0]

        win_rate = (len(wins) / len(realized_pnls) * 100) if realized_pnls else 0

        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float('inf') if gross_profit > 0 else 0)

        return {
            "pnl": total_pnl,
            "pnl_pct": pnl_pct,
            "trade_count": len(self.trades),
            "realized_trade_count": len(realized_pnls),
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "final_value": self.portfolio_value(),
            "total_fees": self.total_fees
        }
