import os
import pandas as pd
from datetime import datetime
from backtest_engine import BacktestEngine
from strategies.donchian_breakout import DonchianBreakout
from strategies.ema_pullback import EMAPullback
from strategies.macd_momentum import MACDMomentum
from strategies.stoch_bounce import StochBounce

SYMBOLS = ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "LINK-USD", "DOGE-USD"]
INITIAL_BALANCE = 5000.0
TRADE_AMOUNT = 500.0 # R$ 500 per trade as in main.py (it was USD there, but we'll use same relative value)

def run_simulation():
    engine = BacktestEngine(initial_balance=INITIAL_BALANCE)

    strategies = [
        DonchianBreakout(),
        EMAPullback(),
        MACDMomentum(),
        StochBounce()
    ]

    print(f"Iniciando simulação com {INITIAL_BALANCE} BRL")
    print(f"Estratégias: {[s.name for s in strategies]}")
    print(f"Ativos: {SYMBOLS}")

    # Load all data
    data = {}
    for symbol in SYMBOLS:
        df = pd.read_csv(f"data/{symbol}.csv")
        data[symbol] = df

    # We need to iterate through time. Since we have hourly data for all,
    # we can find all unique timestamps.
    all_timestamps = sorted(pd.concat([df['start'] for df in data.values()]).unique())

    for ts in all_timestamps:
        current_time = datetime.fromtimestamp(ts)

        for symbol in SYMBOLS:
            df_full = data[symbol]
            # Data available up to this timestamp
            df_slice = df_full[df_full['start'] <= ts]

            if df_slice.empty:
                continue

            curr_row = df_slice.iloc[-1]
            if curr_row['start'] != ts:
                # No data for this symbol at this exact timestamp
                continue

            price = curr_row['close']
            engine.update_price(symbol.split("-")[0], price, current_time)

            # Analyze strategies
            votes = {"BUY": 0, "SELL": 0, "HOLD": 0}
            for strategy in strategies:
                signal = strategy.analyze(df_slice)
                votes[signal] += 1

            decision = max(votes, key=votes.get)

            symbol_code = symbol.split("-")[0]

            # Consensus logic from main.py: votes["BUY"] >= 2
            if decision == "BUY" and votes["BUY"] >= 2:
                engine.buy(symbol_code, TRADE_AMOUNT, price, "consensus")
            elif decision == "SELL" and votes["SELL"] >= 2:
                held = engine.holdings.get(symbol_code, 0)
                if held > 0:
                    engine.sell(symbol_code, held, price, "consensus")

    metrics = engine.get_metrics()
    print("\n--- Resultados da Simulação ---")
    print(f"Saldo Final: {metrics['final_value']:.2f}")
    print(f"P&L: {metrics['pnl']:.2f} ({metrics['pnl_pct']:.2f}%)")
    print(f"Trades totais: {metrics['trade_count']}")
    print(f"Win Rate: {metrics['win_rate']:.2f}%")
    print(f"Profit Factor: {metrics['profit_factor']:.2f}")
    print(f"Taxas pagas: {metrics['total_fees']:.2f}")

    return metrics, engine.trades

if __name__ == "__main__":
    metrics, trades = run_simulation()

    # Save results for report
    import json
    with open("sim_results.json", "w") as f:
        json.dump({"metrics": metrics, "trades": trades}, f, indent=2)
