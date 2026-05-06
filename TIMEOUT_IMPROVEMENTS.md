# Signal Refresh Freezing Fix - Comprehensive Timeout Handling

## Problem Analysis
The Oracle server was freezing during signal refresh cycles (`trading_loop`). Root causes identified:

1. **Slow Coinbase API calls** - `client.get_ticker()` and `_get_candles()` are synchronous and could block the event loop
2. **Unresponsive WebSocket clients** - `broadcast()` could hang if any connected client was slow or unresponsive
3. **No timeout protection** - No mechanisms to interrupt slow operations
4. **Cache fallback missing** - No fallback when API calls were slow

## Solution Implemented
Comprehensive multi-layer timeout handling added to `dashboard/app.py`:

### 1. WebSocket Broadcast Timeout (5 seconds)
**Lines 508-524 - `broadcast()` function**

```python
async def broadcast(data: dict):
    """Broadcast estado com timeout para evitar travamentos de clientes lentos"""
    dead = []
    for ws in connected_clients:
        try:
            await asyncio.wait_for(ws.send_json(data), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("WebSocket send timeout - removendo cliente")
            dead.append(ws)
        except Exception as e:
            logger.debug(f"WebSocket send error: {e}")
            dead.append(ws)
    for ws in dead:
        try:
            connected_clients.remove(ws)
        except ValueError:
            pass
```

**Benefits:**
- Unresponsive clients automatically removed
- Next broadcast will not wait for dead connections
- Prevents cascade failures

### 2. USD/BRL Fetch Timeout (10 seconds)
**Lines 566-573 - USD/BRL exchange rate**

```python
try:
    usd_brl = await asyncio.wait_for(
        loop.run_in_executor(None, _fetch_usd_brl),
        timeout=10.0
    )
except asyncio.TimeoutError:
    logger.warning("USD/BRL fetch timeout - usando cache")
    usd_brl = _usd_brl_cache["rate"]
```

**Benefits:**
- API call moved to thread pool via executor
- Doesn't block event loop
- Falls back to cached rate on timeout

### 3. Fear & Greed Index Timeout (10 seconds)
**Lines 582-589 - Fear & Greed Index**

```python
try:
    fg = await asyncio.wait_for(
        loop.run_in_executor(None, _fetch_fear_greed),
        timeout=10.0
    )
except asyncio.TimeoutError:
    logger.warning("Fear&Greed fetch timeout - usando cache")
    fg = _fg_cache
```

**Benefits:**
- API call moved to thread pool
- Falls back to cached value
- Ensures dynamic TP calculation always proceeds

### 4. Ticker Fetch Timeout (8 seconds per pair)
**Lines 598-602 - Crypto price ticker**

```python
ticker = await asyncio.wait_for(
    loop.run_in_executor(None, client.get_ticker, pair),
    timeout=8.0
)
```

**Benefits:**
- Ticker fetch moved to executor (non-blocking)
- 8-second timeout per pair
- If BTC-USD times out, ETH-USD and SOL-USD still execute

### 5. Candles Fetch Timeouts (8 seconds per granularity)
**Lines 616-641 - OHLC data for all timeframes**

All candle fetches (1H, 6H, 1D, 30M) wrapped with:

```python
try:
    candles_1h = await asyncio.wait_for(
        loop.run_in_executor(None, _get_candles, pair, CANDLE_1H, 250),
        timeout=8.0
    )
except asyncio.TimeoutError:
    logger.warning(f"[{pair}] Candles 1H timeout - usando cache")
    candles_1h = _candle_cache.get(f"{pair}:{CANDLE_1H}", {}).get("data", [])
```

**Benefits:**
- All candle granularities protected
- Falls back to cached candles
- Strategies receive data even if API is slow
- Single slow pair doesn't block other pairs

## Execution Flow
1. **Cycle starts** - `state["cycle"]` incremented
2. **USD/BRL fetch** - 10s timeout via executor + cache fallback
3. **Fear & Greed fetch** - 10s timeout via executor + cache fallback
4. **For each pair (BTC, ETH, SOL):**
   - **Ticker fetch** - 8s timeout via executor
   - **Candles 1H, 6H, 1D, 30M** - 8s timeout each via executor + cache fallback
   - **Strategy analysis** - Uses available data (live or cached)
   - **Trade execution** - Based on current signals
5. **Portfolio update** - Calculates total P&L
6. **WebSocket broadcast** - 5s timeout, removes dead clients
7. **Sleep** - 180 seconds until next cycle

## Why This Fixes Freezing

### Before
- Single slow API call → entire loop hangs
- One unresponsive client → broadcast blocks all updates
- No fallback → loop waiting indefinitely

### After
- Slow API call → timeout + cache fallback → next step continues
- Unresponsive client → disconnected → broadcast proceeds
- Every operation has timeout → loop always progresses

## Fallback Mechanisms
1. **Ticker fails** → `continue` to next pair (price not updated)
2. **Candles fail** → use cached candles (may be 4min old)
3. **USD/BRL fails** → use cached rate (30min old)
4. **Fear&Greed fails** → use cached value (60min old)
5. **WebSocket fails** → client removed, not broadcast again

## Logging
New timeout warnings logged at WARNING level:
- `"WebSocket send timeout - removendo cliente"`
- `"USD/BRL fetch timeout - usando cache"`
- `"Fear&Greed fetch timeout - usando cache"`
- `"[{pair}] Candles 1H timeout - usando cache"` (and other granularities)

These warnings help identify if API is consistently slow.

## Performance Impact
- **Slightly faster** - Timeouts terminate hanging operations early
- **More reliable** - Cache fallback ensures strategies always get data
- **Better resource usage** - Thread pool prevents event loop blocking

## Testing Recommendation
After deployment to Oracle:
1. Monitor logs for timeout warnings over 2-3 hours
2. If no timeouts occur → API is responsive
3. If frequent timeouts (>2-3 per cycle) → investigate Coinbase API or network
4. If freezes still occur → additional debugging needed

## Commit Hash
`c648b47` - "Add comprehensive timeout handling to prevent signal refresh freezing"

## Deployment Steps
1. Pull latest from GitHub: `git pull origin master`
2. Restart FastAPI server: `pkill -f uvicorn` or equivalent
3. Monitor logs for timeout warnings
4. Report any unusual patterns
