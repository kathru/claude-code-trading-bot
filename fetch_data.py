import yfinance as yf
import os
import pandas as pd

SYMBOLS = ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "LINK-USD", "DOGE-USD"]
START_DATE = "2026-01-01"
END_DATE = "2026-05-01" # To include April 30

def fetch():
    if not os.path.exists("data"):
        os.makedirs("data")

    for symbol in SYMBOLS:
        print(f"Fetching {symbol}...")
        df = yf.download(symbol, start=START_DATE, end=END_DATE, interval="1h")
        if df.empty:
            print(f"Warning: No data for {symbol}")
            continue

        df = df.reset_index()
        # yfinance columns are: Datetime, Open, High, Low, Close, Adj Close, Volume

        # Convert Datetime to unix timestamp
        df["start"] = df["Datetime"].apply(lambda x: int(x.timestamp()))

        # BaseStrategy.candles_to_df expects: ["start", "low", "high", "open", "close", "volume"]
        df_export = df[["start", "Low", "High", "Open", "Close", "Volume"]]
        df_export.columns = ["start", "low", "high", "open", "close", "volume"]

        filepath = f"data/{symbol}.csv"
        df_export.to_csv(filepath, index=False)
        print(f"Saved to {filepath}")

if __name__ == "__main__":
    fetch()
