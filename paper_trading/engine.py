import os
import json
import time
from datetime import datetime
from colorama import Fore, init

init(autoreset=True)

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "engine_state.json")


class PaperTradingEngine:
    def __init__(self, initial_balance_usd: float = 10000.0):
        self.initial_balance = initial_balance_usd
        self.balance_usd = initial_balance_usd
        self.holdings = {}
        self.trades = []
        self.prices = {}
        self._load_state()

    def _load_state(self):
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r") as f:
                    s = json.load(f)
                self.balance_usd = s.get("balance_usd", self.initial_balance)
                self.holdings = s.get("holdings", {})
                self.trades = s.get("trades", [])
                print(Fore.CYAN + f"[PAPER] Estado restaurado — USD: ${self.balance_usd:.2f} | Holdings: {self.holdings}")
        except Exception as e:
            print(Fore.YELLOW + f"[PAPER] Sem estado salvo, iniciando do zero. ({e})")

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w") as f:
                json.dump({
                    "balance_usd": self.balance_usd,
                    "holdings": self.holdings,
                    "trades": self.trades[-200:],
                    "saved_at": datetime.now().isoformat(),
                }, f, indent=2)
        except Exception as e:
            print(Fore.RED + f"[PAPER] Erro ao salvar estado: {e}")

    def update_price(self, symbol: str, price: float):
        self.prices[symbol] = price

    def buy(self, symbol: str, usd_amount: float, price: float, strategy: str) -> bool:
        if usd_amount > self.balance_usd:
            print(Fore.RED + f"[PAPER] COMPRA negada: saldo insuficiente (${self.balance_usd:.2f})")
            return False
        qty = usd_amount / price
        self.balance_usd -= usd_amount
        self.holdings[symbol] = self.holdings.get(symbol, 0) + qty
        self._log_trade("BUY", symbol, qty, price, usd_amount, strategy)
        self._save_state()
        return True

    def sell(self, symbol: str, qty: float, price: float, strategy: str) -> bool:
        held = self.holdings.get(symbol, 0)
        if qty > held:
            print(Fore.RED + f"[PAPER] VENDA negada: saldo insuficiente de {symbol} ({held:.8f})")
            return False
        usd_received = qty * price
        self.holdings[symbol] = held - qty
        if self.holdings[symbol] < 1e-10:
            del self.holdings[symbol]
        self.balance_usd += usd_received
        self._log_trade("SELL", symbol, qty, price, usd_received, strategy)
        self._save_state()
        return True

    def _log_trade(self, side: str, symbol: str, qty: float, price: float, usd: float, strategy: str):
        trade = {
            "time": datetime.now().isoformat(),
            "side": side, "symbol": symbol,
            "qty": qty, "price": price, "usd": usd, "strategy": strategy,
        }
        self.trades.append(trade)
        color = Fore.GREEN if side == "BUY" else Fore.YELLOW
        print(color + f"[PAPER] {side} {qty:.6f} {symbol} @ ${price:.2f} = ${usd:.2f} [{strategy}]")

    def portfolio_value(self) -> float:
        total = self.balance_usd
        for symbol, qty in self.holdings.items():
            total += qty * self.prices.get(symbol, 0)
        return total

    def print_status(self):
        pnl = self.portfolio_value() - self.initial_balance
        pnl_pct = (pnl / self.initial_balance) * 100
        color = Fore.GREEN if pnl >= 0 else Fore.RED
        print(f"\n{'='*50}")
        print(f"  Saldo USD:      ${self.balance_usd:.2f}")
        for symbol, qty in self.holdings.items():
            price = self.prices.get(symbol, 0)
            print(f"  {symbol}:          {qty:.6f} (${qty * price:.2f})")
        print(color + f"  Portfolio:      ${self.portfolio_value():.2f}")
        print(color + f"  P&L:            ${pnl:.2f} ({pnl_pct:+.2f}%)")
        print(f"  Trades:         {len(self.trades)}")
        print(f"{'='*50}\n")
