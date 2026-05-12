import os
import sys
import json
import pandas as pd
import yfinance as yf
from datetime import datetime
import numpy as np

# Add the repository root to sys.path so we can import modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Prevent dashboard/app.py from doing things on import if possible,
# but it seems it's okay for now since we just need the constants.
from dashboard.app import (
    PAIRS, TRADE_PCT, MAX_OPEN_SLOTS, BE_TRIGGER_MULT, TRAIL_TRIGGER_MULT,
    PAIR_SL_RANGE, MAX_DAILY_TRADES, BUY_COOLDOWN_SECONDS, SL_COOLDOWN_CYCLES,
    _calc_exit, _detect_market_regime
)
from paper_trading.engine import PaperTradingEngine, TAKER_FEE
from strategies.donchian_breakout import DonchianBreakout
from strategies.ema_pullback import EMAPullback
from strategies.macd_momentum import MACDMomentum
from strategies.bb_reversion import BBReversion
from strategies.market_regime import calc_atr

# Configuration override for simulation
INITIAL_BRL = 5000.0

def _empty_slot():
    return {"qty": 0.0, "entry": 0.0, "peak": 0.0,
            "realized": 0.0, "unrealized": 0.0, "pyramids": 0, "be_sl": 0.0,
            "entry_usd": 0.0, "sl_pct": 0.0}

def run_simulation():
    start_date = "2026-01-01"
    end_date = "2026-04-30"

    print(f"Downloading data from {start_date} to {end_date}...")

    data_1h = {}
    data_6h = {}
    for pair in PAIRS:
        ticker = pair # Using BTC-USD style for yfinance
        df = yf.download(ticker, start="2025-10-01", end="2026-05-01", interval="1h")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        data_1h[pair] = df

        # Resample 6h from 1h to match bot's MTF needs
        df6h = df.resample('6h').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna()
        data_6h[pair] = df6h

    # USD/BRL historical rates
    usdbrl_df = yf.download("USDBRL=X", start="2025-10-01", end="2026-05-01", interval="1d")
    if isinstance(usdbrl_df.columns, pd.MultiIndex):
        usdbrl_df.columns = usdbrl_df.columns.get_level_values(0)
    usdbrl_df.columns = [c.lower() for c in usdbrl_df.columns]

    # Initialize Engine with converted BRL to USD
    try:
        initial_usd_rate = float(usdbrl_df.loc[start_date:].iloc[0]['close'])
    except Exception:
        # Fallback if specific date is missing
        initial_usd_rate = float(usdbrl_df.asof(pd.Timestamp(start_date))['close'])

    initial_usd = INITIAL_BRL / initial_usd_rate

    # Force clean state for simulation
    if os.path.exists("data/engine_state.json"):
        os.remove("data/engine_state.json")

    engine = PaperTradingEngine(initial_balance_usd=initial_usd)

    strategies = [
        DonchianBreakout(),
        MACDMomentum(),
        EMAPullback(),
        BBReversion()
    ]

    slots = {f"{s.name}:{p}": _empty_slot() for s in strategies for p in PAIRS}
    last_buy_time = {}
    sl_cooldowns = {}
    daily_trade_count = {}

    # Simulation loop over hourly candles
    simulation_times = data_1h["BTC-USD"].loc[start_date:end_date].index

    for current_time in simulation_times:
        current_date_str = current_time.strftime("%Y-%m-%d")
        if current_date_str not in daily_trade_count:
            daily_trade_count[current_date_str] = 0

        asof_time = current_time.tz_localize(None) if current_time.tzinfo else current_time
        try:
            usd_brl = float(usdbrl_df.asof(asof_time)['close'])
        except Exception:
            usd_brl = 5.70 # Last resort fallback

        # Detect global regime using BTC (matching app.py logic)
        btc_1h_candles = data_1h["BTC-USD"].loc[:current_time].tail(250).to_dict('records')
        btc_6h_candles = data_6h["BTC-USD"].loc[:current_time].tail(100).to_dict('records')

        # _detect_market_regime expects candle lists (dicts)
        market_mode, _ = _detect_market_regime(btc_1h_candles, btc_6h_candles)

        portfolio_total = engine.portfolio_value()

        for pair in PAIRS:
            symbol = pair.split("-")[0]
            df_1h = data_1h[pair].loc[:current_time]
            if len(df_1h) < 100: continue

            price = float(df_1h.iloc[-1]['close'])
            engine.update_price(symbol, price)

            # Update slots and exits
            open_slots_count = sum(1 for s in slots.values() if s["qty"] > 0)

            for strat in strategies:
                key = f"{strat.name}:{pair}"
                slot = slots[key]

                if slot["qty"] > 0:
                    slot["peak"] = max(slot["peak"], price)
                    # Use app.py's unified exit logic
                    tp_hit, sl_hit, effective_sl, tp_level, sl_pct = _calc_exit(slot, price, pair)
                    slot["be_sl"] = effective_sl

                    if tp_hit or sl_hit:
                        label = f"TP+{sl_pct*2:.1f}%" if tp_hit else ("BE-stop" if (price >= slot["entry"]) else f"SL-{sl_pct:.1f}%")
                        qty = slot["qty"]
                        if engine.sell(symbol, qty, price, f"{strat.name}:{label}"):
                            slots[key] = _empty_slot()
                            daily_trade_count[current_date_str] += 1
                            if sl_hit and price < slot["entry"]:
                                sl_cooldowns[key] = SL_COOLDOWN_CYCLES
                    else:
                        # Strategy SELL signal
                        sig = strat.analyze(df_1h)
                        if sig == "SELL":
                            qty = slot["qty"]
                            if engine.sell(symbol, qty, price, f"{strat.name}:SELL"):
                                slots[key] = _empty_slot()
                                daily_trade_count[current_date_str] += 1

                # BUY logic
                else:
                    cooldown = sl_cooldowns.get(key, 0)
                    if cooldown > 0:
                        sl_cooldowns[key] -= 1
                        continue

                    if daily_trade_count[current_date_str] >= MAX_DAILY_TRADES: continue
                    if open_slots_count >= MAX_OPEN_SLOTS: continue
                    if (current_time.timestamp() - last_buy_time.get(key, 0)) < BUY_COOLDOWN_SECONDS: continue

                    sig = strat.analyze(df_1h)
                    if sig == "BUY":
                        # Regime filter logic from app.py
                        if market_mode == "bear":
                            ema200_1h = df_1h['close'].ewm(span=200, adjust=False).mean().iloc[-1]
                            if price <= ema200_1h: continue

                        if strat.name == "BB Reversion" and market_mode == "bull": continue

                        regime_mult = 1.0 if market_mode == "bull" else (0.7 if market_mode == "chop" else 0.5)
                        trade_usd = portfolio_total * TRADE_PCT * regime_mult

                        if engine.balance_usd >= trade_usd * (1 + TAKER_FEE):
                            # Calculate SL% using ATR (replicates app.py dynamic SL)
                            atr_val = calc_atr(df_1h.tail(100))
                            sl_at_entry = (atr_val * 2.0 / price) * 100 if price > 0 else 5.0
                            sl_min, sl_max = PAIR_SL_RANGE.get(pair, (0.03, 0.07))
                            sl_pct_entry = max(sl_min * 100, min(sl_max * 100, sl_at_entry))

                            if engine.buy(symbol, trade_usd, price, strat.name):
                                qty = trade_usd / price
                                slots[key] = {
                                    "qty": qty, "entry": price, "peak": price,
                                    "realized": 0.0, "unrealized": 0.0, "pyramids": 0, "be_sl": 0.0,
                                    "entry_usd": trade_usd, "sl_pct": sl_pct_entry
                                }
                                last_buy_time[key] = current_time.timestamp()
                                daily_trade_count[current_date_str] += 1
                                open_slots_count += 1

    # Final results analysis
    final_usd = engine.portfolio_value()
    final_brl_rate = float(usdbrl_df.iloc[-1]['close'])
    final_brl = final_usd * final_brl_rate

    print("\n" + "="*50)
    print(f"SIMULATION COMPLETE: {start_date} to {end_date}")
    print(f"Initial Portfolio: R$ {INITIAL_BRL:.2f} ($ {initial_usd:.2f})")
    print(f"Final Portfolio:   R$ {final_brl:.2f} ($ {final_usd:.2f})")
    print(f"Total P&L (BRL):   {((final_brl/INITIAL_BRL)-1)*100:+.2f}%")
    print(f"Total Trades:      {len(engine.trades)}")

    # Calculate performance metrics
    trade_results = []
    positions = {} # symbol: {strat: {qty, cost}}

    for t in engine.trades:
        sym = t['symbol']
        strat = t['strategy'].split(':')[0]
        if t['side'] == 'BUY':
            if sym not in positions: positions[sym] = {}
            if strat not in positions[sym]: positions[sym][strat] = {'qty': 0, 'cost': 0}
            positions[sym][strat]['qty'] += t['qty']
            positions[sym][strat]['cost'] += t['usd'] + t['fee']
        else:
            if sym in positions and strat in positions[sym]:
                p = positions[sym][strat]
                sell_qty = t['qty']
                sell_usd = t['usd'] # net received
                buy_cost_prop = (sell_qty / p['qty']) * p['cost']
                profit = sell_usd - buy_cost_prop
                trade_results.append({
                    'symbol': sym,
                    'strategy': strat,
                    'profit': profit,
                    'profit_pct': (profit / buy_cost_prop) * 100
                })
                p['qty'] -= sell_qty
                p['cost'] -= buy_cost_prop

    win_rate = 0
    profit_factor = 0
    if trade_results:
        win_rate = len([r for r in trade_results if r['profit'] > 0]) / len(trade_results)
        gross_profit = sum([r['profit'] for r in trade_results if r['profit'] > 0])
        gross_loss = abs(sum([r['profit'] for r in trade_results if r['profit'] < 0]))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

        print(f"Win Rate:          {win_rate*100:.2f}%")
        print(f"Profit Factor:     {profit_factor:.2f}")

    # Performance breakdown
    strat_perf = {}
    for r in trade_results:
        s = r['strategy']
        strat_perf[s] = strat_perf.get(s, 0) + r['profit']

    crypto_perf = {}
    for r in trade_results:
        c = r['symbol']
        crypto_perf[c] = crypto_perf.get(c, 0) + r['profit']

    # Export results for report generation
    results = {
        "initial_brl": INITIAL_BRL,
        "final_brl": final_brl,
        "initial_usd": initial_usd,
        "final_usd": final_usd,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "strat_perf": strat_perf,
        "crypto_perf": crypto_perf,
        "total_trades": len(trade_results)
    }

    with open("sim_results.json", "w") as f:
        json.dump(results, f, indent=2)

if __name__ == "__main__":
    run_simulation()
