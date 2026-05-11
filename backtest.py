import pandas as pd
import yfinance as yf
from strategies.donchian_breakout import DonchianBreakout
from strategies.ema_pullback import EMAPullback
from strategies.macd_momentum import MACDMomentum
from strategies.stoch_bounce import StochBounce
from strategies.rsi_divergence_detector import RSIDivergenceDetector
from datetime import datetime

# Configuration
PAIRS = ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "LINK-USD", "DOGE-USD"]
START_DATE = "2026-01-01"
END_DATE = "2026-04-30"
INITIAL_BRL = 5000.0
USD_BRL_RATE = 5.517
INITIAL_USD = INITIAL_BRL / USD_BRL_RATE
TAKER_FEE = 0.006

# Risk Management from app.py
TRADE_PCT = 0.10
PYRAMID_MAX = 3
PYRAMID_SIZE_PCT = 0.25
TRAILING_STOP_PCT = 2.5
TRAILING_ACTIVATE_PCT = 2.0
BREAKEVEN_ACTIVATE_PCT = 1.5
MAX_OPEN_SLOTS = 8

class BacktestEngineV2:
    def __init__(self, initial_balance):
        self.balance_usd = initial_balance
        self.initial_balance = initial_balance
        self.slots = {} # { "Strategy:Pair": slot_data }
        self.trades = []

    def get_portfolio_value(self, current_prices):
        total = self.balance_usd
        for key, slot in self.slots.items():
            if slot['qty'] > 0:
                symbol = key.split(":")[1].split("-")[0]
                total += slot['qty'] * current_prices.get(symbol, 0)
        return total

    def buy(self, strat_name, pair, price, time, current_prices):
        symbol = pair.split("-")[0]
        key = f"{strat_name}:{pair}"

        # Check open slots
        open_slots = sum(1 for s in self.slots.values() if s['qty'] > 0)
        if open_slots >= MAX_OPEN_SLOTS: return False

        portfolio_val = self.get_portfolio_value(current_prices)
        trade_usd = portfolio_val * TRADE_PCT
        fee = trade_usd * TAKER_FEE
        total_cost = trade_usd + fee

        if self.balance_usd >= total_cost:
            qty = trade_usd / price
            self.balance_usd -= total_cost
            self.slots[key] = {
                "qty": qty,
                "entry": price,
                "peak": price,
                "entry_usd": trade_usd,
                "pyramids": 0,
                "be_sl": price * 0.95 # initial 5% SL
            }
            self.trades.append({"time": str(time), "side": "BUY", "pair": pair, "qty": qty, "price": price, "usd": trade_usd, "strat": strat_name})
            return True
        return False

    def sell(self, key, price, time, reason):
        slot = self.slots.get(key)
        if not slot or slot['qty'] <= 0: return False

        pair = key.split(":")[1]
        symbol = pair.split("-")[0]
        strat = key.split(":")[0]

        gross = slot['qty'] * price
        fee = gross * TAKER_FEE
        net = gross - fee

        self.balance_usd += net
        # Calculate P&L relative to cost including entry fee
        cost_basis = slot['entry'] * slot['qty'] * (1 + TAKER_FEE)
        pnl = net - cost_basis

        self.trades.append({
            "time": str(time), "side": "SELL", "pair": pair, "qty": slot['qty'],
            "price": price, "usd": net, "strat": strat, "reason": reason, "pnl": pnl
        })
        slot['qty'] = 0.0
        return True

    def pyramid(self, key, price, time):
        slot = self.slots.get(key)
        if not slot or slot['qty'] <= 0 or slot['pyramids'] >= PYRAMID_MAX: return False

        pair = key.split(":")[1]
        symbol = pair.split("-")[0]
        strat = key.split(":")[0]

        pyr_usd = slot['entry_usd'] * PYRAMID_SIZE_PCT
        fee = pyr_usd * TAKER_FEE
        total_cost = pyr_usd + fee

        if self.balance_usd >= total_cost:
            qty = pyr_usd / price
            self.balance_usd -= total_cost
            new_qty = slot['qty'] + qty
            # Update weighted average entry price
            slot['entry'] = (slot['qty'] * slot['entry'] + qty * price) / new_qty
            slot['qty'] = new_qty
            slot['pyramids'] += 1
            self.trades.append({"time": str(time), "side": "BUY_PYR", "pair": pair, "qty": qty, "price": price, "usd": pyr_usd, "strat": strat})
            return True
        return False

def run_backtest_v2():
    engine = BacktestEngineV2(INITIAL_USD)
    strategies = [
        DonchianBreakout(),
        EMAPullback(),
        MACDMomentum(),
        StochBounce(),
    ]

    data_frames = {}
    for pair in PAIRS:
        df = yf.download(pair, start=START_DATE, end=END_DATE, interval="1h")
        if df.empty: continue
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df = df.reset_index().rename(columns={"Datetime": "start", "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
        data_frames[pair] = df

    all_times = sorted(pd.concat([df['start'] for df in data_frames.values()]).unique())

    for current_time in all_times:
        current_prices = {}
        # Collect all prices for this timestamp first
        for pair in PAIRS:
            df = data_frames.get(pair)
            if df is None: continue
            row = df[df['start'] == current_time]
            if not row.empty:
                current_prices[pair.split("-")[0]] = float(row['close'].iloc[0])

        for pair, df in data_frames.items():
            price = current_prices.get(pair.split("-")[0])
            if price is None: continue

            # 1. Update Existing Slots (Risk Management)
            for strat in strategies:
                key = f"{strat.name}:{pair}"
                slot = engine.slots.get(key)
                if slot and slot['qty'] > 0:
                    slot['peak'] = max(slot['peak'], price)
                    gain_pct = (price - slot['entry']) / slot['entry'] * 100

                    # Trailing / SL / BE Logic
                    be_active = gain_pct >= BREAKEVEN_ACTIVATE_PCT
                    current_sl = slot['entry'] if be_active else slot['entry'] * 0.95

                    tr_active = gain_pct >= TRAILING_ACTIVATE_PCT
                    tr_hit = tr_active and price <= slot['peak'] * (1 - TRAILING_STOP_PCT / 100)
                    sl_hit = price <= current_sl

                    if tr_hit: engine.sell(key, price, current_time, "TRAILING")
                    elif sl_hit: engine.sell(key, price, current_time, "SL/BE")

            # 2. Check Signals for New Trades or Pyramiding
            hist_df = df[df['start'] <= current_time].tail(100)
            if len(hist_df) < 50: continue

            for strat in strategies:
                key = f"{strat.name}:{pair}"
                signal = strat.analyze(hist_df)
                slot = engine.slots.get(key)

                if signal == "BUY":
                    if not slot or slot['qty'] <= 0:
                        engine.buy(strat.name, pair, price, current_time, current_prices)
                    else:
                        gain_pct = (price - slot['entry']) / slot['entry'] * 100
                        if gain_pct >= 1.0:
                            engine.pyramid(key, price, current_time)
                elif signal == "SELL":
                    if slot and slot['qty'] > 0:
                        engine.sell(key, price, current_time, "SIGNAL")

    final_val = engine.get_portfolio_value(current_prices)
    pnl = final_val - engine.initial_balance
    sells = [t for t in engine.trades if t['side'] == 'SELL']
    wins = [t for t in sells if t.get('pnl', 0) > 0]
    wr = len(wins)/len(sells)*100 if sells else 0

    gp = sum(t['pnl'] for t in wins)
    gl = abs(sum(t['pnl'] for t in sells if t.get('pnl', 0) <= 0))
    pf = gp/gl if gl > 0 else gp

    print(f"Final V2: ${final_val:.2f} (R${final_val*USD_BRL_RATE:.2f})")
    print(f"PNL V2: ${pnl:.2f} ({pnl/engine.initial_balance*100:.2f}%)")
    print(f"Trades: {len(engine.trades)}, WR: {wr:.2f}%, PF: {pf:.2f}")

if __name__ == "__main__":
    run_backtest_v2()
