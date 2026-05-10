import pandas as pd
import yfinance as yf
from strategies.ma_crossover import MACrossoverStrategy
from strategies.rsi import RSIStrategy
from strategies.scalping import ScalpingStrategy
from datetime import datetime

# Configuration
PAIRS = ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "LINK-USD", "DOGE-USD"]
START_DATE = "2026-01-01"
END_DATE = "2026-04-30"
INITIAL_BRL = 5000.0
USD_BRL_RATE = 5.517
INITIAL_USD = INITIAL_BRL / USD_BRL_RATE
TRADE_USD = 500.0 # ACTUAL BOT RULE
TAKER_FEE = 0.006

class BacktestEngine:
    def __init__(self, initial_balance):
        self.balance_usd = initial_balance
        self.initial_balance = initial_balance
        self.holdings = {symbol.split("-")[0]: 0.0 for symbol in PAIRS}
        self.trades = []

    def buy(self, symbol, price, time):
        fee = TRADE_USD * TAKER_FEE
        total_cost = TRADE_USD + fee
        if self.balance_usd >= total_cost:
            qty = TRADE_USD / price
            self.balance_usd -= total_cost
            self.holdings[symbol] += qty
            self.trades.append({"time": str(time), "side": "BUY", "symbol": symbol, "qty": qty, "price": price, "usd_spent": total_cost})
            return True
        return False

    def sell(self, symbol, price, time):
        qty = self.holdings.get(symbol, 0)
        if qty > 0:
            gross = qty * price
            fee = gross * TAKER_FEE
            net_received = gross - fee
            self.balance_usd += net_received
            self.holdings[symbol] = 0.0
            self.trades.append({"time": str(time), "side": "SELL", "symbol": symbol, "qty": qty, "price": price, "usd_received": net_received})
            return True
        return False

def run_backtest():
    engine = BacktestEngine(INITIAL_USD)
    strategies = [
        MACrossoverStrategy(short_window=9, long_window=21),
        RSIStrategy(period=14, oversold=30, overbought=70),
        ScalpingStrategy(bb_period=20, bb_std=2.0),
    ]

    data_frames = {}
    for pair in PAIRS:
        df = yf.download(pair, start=START_DATE, end=END_DATE, interval="1h")
        if df.empty: continue
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df = df.reset_index().rename(columns={"Datetime": "start", "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
        data_frames[pair.split("-")[0]] = df

    all_times = sorted(pd.concat([df['start'] for df in data_frames.values()]).unique())
    last_prices = {}

    for current_time in all_times:
        for symbol, df in data_frames.items():
            row = df[df['start'] == current_time]
            if row.empty: continue
            price = float(row['close'].iloc[0])
            last_prices[symbol] = price
            hist_df = df[df['start'] <= current_time].tail(100)
            if len(hist_df) < 30: continue

            votes = {"BUY": 0, "SELL": 0, "HOLD": 0}
            for s in strategies:
                votes[s.analyze(hist_df)] += 1

            # STRICT CONSENSUS (>= 2)
            if votes["BUY"] >= 2: engine.buy(symbol, price, current_time)
            elif votes["SELL"] >= 2: engine.sell(symbol, price, current_time)

    final_portfolio_val = engine.balance_usd + sum(engine.holdings[s] * last_prices.get(s, 0) for s in engine.holdings)
    pnl_usd = final_portfolio_val - engine.initial_balance

    # Win rate and Profit Factor calc
    sells = [t for t in engine.trades if t['side'] == 'SELL']
    wins = 0
    gross_profit = 0
    gross_loss = 0
    temp_buys = {}
    for t in engine.trades:
        s = t['symbol']
        if t['side'] == 'BUY':
            if s not in temp_buys: temp_buys[s] = []
            temp_buys[s].append(t)
        else:
            if s in temp_buys and temp_buys[s]:
                b = temp_buys[s].pop(0)
                net_profit = t['usd_received'] - b['usd_spent']
                if net_profit > 0:
                    wins += 1
                    gross_profit += net_profit
                else:
                    gross_loss += abs(net_profit)

    wr = (wins / len(sells) * 100) if sells else 0
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0)

    print(f"Final: ${final_portfolio_val:.2f} (R${final_portfolio_val*USD_BRL_RATE:.2f})")
    print(f"PNL: ${pnl_usd:.2f} ({pnl_usd/engine.initial_balance*100:.2f}%)")
    print(f"Trades: {len(engine.trades)}, WR: {wr:.2f}%, PF: {pf:.2f}")

    with open("backtest_results.txt", "w") as f:
        f.write(f"INITIAL_BRL: {INITIAL_BRL}\nFINAL_BRL: {final_portfolio_val*USD_BRL_RATE}\nPNL_BRL: {pnl_usd*USD_BRL_RATE}\n")
        f.write(f"INITIAL_USD: {INITIAL_USD}\nFINAL_USD: {final_portfolio_val}\nPNL_USD: {pnl_usd}\n")
        f.write(f"TRADE_COUNT: {len(engine.trades)}\nWIN_RATE: {wr}\nPROFIT_FACTOR: {pf}\n")

if __name__ == "__main__":
    run_backtest()
