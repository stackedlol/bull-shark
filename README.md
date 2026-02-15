# Coinbase Advanced Trade Spot Trading Bot

BTC-USD and ETH-USD spot trading bot using a take-profits → re-buy lower → build position strategy. Runs 24/7, restart-safe via SQLite, observable via SSH.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create `.env` from the example:

```bash
cp .env.example .env
```

Edit `.env` with your Coinbase Advanced Trade API credentials (CDP API key with trading permissions).

## Commands

**Test authentication:**
```bash
python -m src.main test-auth
```

**Dry-run (no real orders, reads real market data):**
```bash
python -m src.main dry-run --once      # single loop
python -m src.main dry-run             # continuous
```

**Live trading:**
```bash
python -m src.main run --once          # single loop
python -m src.main run                 # continuous 24/7
python -m src.main run --products BTC-USD  # single product
```

**Status dashboard:**
```bash
python -m src.main status
```

## Monitoring

```bash
tail -f logs/bot.log
python -m src.main status
```

## Strategy Overview

- **Take-profit ladder:** Sells portions at +2%, +4%, +6%, +8% above anchor price
- **Re-buy:** Places limit buy orders below anchor, scaled by ATR volatility
- **Trend filter:** EMA(12)/EMA(26) crossover adjusts sell fractions and rebuy distance
- **Guards:** 5-min cooldown, 20 trades/day cap, $15 minimum order size

## Linux Deployment

```bash
# Run in background with nohup
nohup python -m src.main run > /dev/null 2>&1 &

# Or use systemd service
# Or run in tmux/screen session
```

## File Structure

```
src/
  main.py              CLI entry point
  config.py            All tunables + env loading
  logging_setup.py     Rotating file + console logging
  coinbase/
    auth.py            JWT generation (ES256)
    client.py          API client with retry + dry-run
  storage/
    db.py              SQLite state persistence
  bot/
    strategy.py        TP ladder, re-buy, trend, ATR logic
    runner.py          Main loop, reconciliation, execution
data/                  SQLite database (gitignored)
logs/                  Log files (gitignored)
```
