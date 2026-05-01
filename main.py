import os
import time
from dotenv import load_dotenv
from colorama import Fore, init

from exchange.coinbase import CoinbaseClient
from paper_trading.engine import PaperTradingEngine
from strategies.ma_crossover import MACrossoverStrategy
from strategies.rsi import RSIStrategy
from strategies.scalping import ScalpingStrategy
from logger import setup_logger, log_cycle, log_trade, log_portfolio
from notifier import notify_trade

init(autoreset=True)
load_dotenv("code.env")

PAIRS = ["BTC-USD", "ETH-USD"]
TRADE_USD = 500.0
INTERVAL = 60

def get_current_price(client: CoinbaseClient, product_id: str) -> float:
    ticker = client.get_ticker(product_id)
    return float(ticker.get("price", 0))

def run():
    api_key = os.getenv("API_KEY")
    secret_key = os.getenv("SECRET_KEY")

    if not api_key or not secret_key:
        print(Fore.RED + "Erro: API_KEY ou SECRET_KEY não encontrados no code.env")
        return

    logger = setup_logger()
    logger.info("="*55)
    logger.info("CLAUDE CODE TRADING BOT - PAPER TRADING MODE")
    logger.info("="*55)

    client = CoinbaseClient(api_key, secret_key)
    engine = PaperTradingEngine(initial_balance_usd=10000.0)

    strategies = [
        MACrossoverStrategy(short_window=9, long_window=21),
        RSIStrategy(period=14, oversold=30, overbought=70),
        ScalpingStrategy(bb_period=20, bb_std=2.0),
    ]

    print(Fore.CYAN + "="*55)
    print(Fore.CYAN + "   CLAUDE CODE TRADING BOT - PAPER TRADING MODE")
    print(Fore.CYAN + "="*55)
    print(f"  Pares:       {', '.join(PAIRS)}")
    print(f"  Estratégias: {', '.join(s.name for s in strategies)}")
    print(f"  Valor/trade: ${TRADE_USD}")
    print(f"  Intervalo:   {INTERVAL}s")
    print(Fore.CYAN + "="*55 + "\n")

    cycle = 0
    while True:
        cycle += 1
        print(Fore.BLUE + f"\n--- Ciclo #{cycle} ---")
        logger.info(f"--- Ciclo #{cycle} ---")

        for pair in PAIRS:
            symbol = pair.split("-")[0]
            try:
                candles = client.get_candles(pair, granularity="FIFTEEN_MINUTE", limit=100)
                price = get_current_price(client, pair)

                if not candles or price == 0:
                    print(Fore.YELLOW + f"[{pair}] Sem dados disponíveis")
                    logger.warning(f"[{pair}] Sem dados disponíveis")
                    continue

                engine.update_price(symbol, price)
                print(f"[{pair}] Preço atual: ${price:,.2f}")

                votes = {"BUY": 0, "SELL": 0, "HOLD": 0}
                signals = {}
                for strategy in strategies:
                    df = strategy.candles_to_df(candles)
                    signal = strategy.analyze(df)
                    votes[signal] += 1
                    signals[strategy.name] = signal
                    print(f"  {strategy.name}: {signal}")

                decision = max(votes, key=votes.get)
                print(f"  Decisão final: {decision} ({votes})")
                log_cycle(logger, cycle, pair, price, signals, decision)

                if decision == "BUY" and votes["BUY"] >= 2:
                    qty = TRADE_USD / price
                    ok = engine.buy(symbol, TRADE_USD, price, "consensus")
                    if ok:
                        log_trade(logger, "BUY", pair, qty, price, TRADE_USD, "consensus")
                        notify_trade("BUY", pair, qty, price, TRADE_USD)
                elif decision == "SELL" and votes["SELL"] >= 2:
                    held = engine.holdings.get(symbol, 0)
                    if held > 0:
                        ok = engine.sell(symbol, held, price, "consensus")
                        if ok:
                            usd = held * price
                            log_trade(logger, "SELL", pair, held, price, usd, "consensus")
                            notify_trade("SELL", pair, held, price, usd)

            except Exception as e:
                print(Fore.RED + f"[{pair}] Erro: {e}")
                logger.error(f"[{pair}] Erro: {e}")

        total = engine.portfolio_value()
        pnl = total - engine.initial_balance
        log_portfolio(logger, engine.balance_usd, total, pnl,
                      (pnl / engine.initial_balance) * 100, engine.holdings)
        engine.print_status()

        print(f"Aguardando {INTERVAL}s...\n")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    run()
