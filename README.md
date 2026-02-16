# Bull Shark Trading Bot

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)
![Version](https://img.shields.io/badge/version-0.0.1-blue)

Automated BTC-USD + ETH-USD spot trader. Sells into strength, re-buys lower, repeats.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add your CDP API key + secret
```

```bash
python -m src.main test-auth        # verify credentials
python -m src.main dry-run --once   # simulate one loop
python -m src.main run              # go live
python -m src.main watch            # live TUI dashboard
```

## How It Works

```mermaid
graph LR
    A[Monitor Price] --> B{Price above anchor?}
    B -->|+2/4/6/8%| C[Take Profit — sell portion]
    C --> D[Place Re-buy Limit Order Below]
    B -->|No| E{Re-buy filled?}
    E -->|Yes| F[Update Anchor — new position]
    F --> A
    E -->|No| A
    D --> A
```

Trend detection (EMA 12/26) adjusts behavior — sells less in uptrends, buys further below in downtrends.

## State

All state lives in `data/bot.db` (SQLite). Kill and restart anytime — the bot reconciles on startup.
