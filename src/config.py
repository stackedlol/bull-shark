import os
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# --- API credentials ---
API_KEY = os.getenv("COINBASE_API_KEY", "")
API_SECRET = os.getenv("COINBASE_API_SECRET", "")

# --- Trading products ---
PRODUCTS = os.getenv("PRODUCTS", "BTC-USD,ETH-USD").split(",")

# --- Take-profit ladder: (threshold %, sell fraction) ---
TP_LADDER = [
    (Decimal("0.02"), Decimal("0.15")),  # +2% → sell 15%
    (Decimal("0.04"), Decimal("0.20")),  # +4% → sell 20%
    (Decimal("0.06"), Decimal("0.25")),  # +6% → sell 25%
    (Decimal("0.08"), Decimal("0.40")),  # +8% → sell 40%
]

# --- Re-buy parameters ---
REBUY_MIN_DISTANCE = Decimal("0.015")     # 1.5% minimum drop
REBUY_ATR_MULTIPLIER = Decimal("1.5")     # ATR scaling factor
REBUY_DOWNTREND_MULTIPLIER = Decimal("1.5")  # widen in downtrend
REBUY_ORDER_TTL = 3600                    # 1 hour stale threshold (seconds)
REBUY_DRIFT_THRESHOLD = Decimal("0.02")   # 2% price drift → cancel

# --- Trend / indicator settings ---
EMA_SHORT = 12
EMA_LONG = 26
ATR_PERIOD = 14
TREND_THRESHOLD = Decimal("0.005")  # 0.5% EMA spread for trend detection

# --- Guard rails ---
MIN_NOTIONAL = Decimal("15")    # Coinbase minimum order size in USD
COOLDOWN_SECONDS = 300          # 5 min between trades per product
DAILY_TRADE_CAP = 20            # max trades per product per day
ESTIMATED_FEE_RATE = Decimal("0.006")  # 0.6% taker fee estimate

# --- Loop ---
LOOP_INTERVAL = int(os.getenv("LOOP_INTERVAL", "60"))

# --- Misc ---
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
DB_PATH = DATA_DIR / "bot.db"

# --- Coinbase API base ---
API_HOST = "api.coinbase.com"
API_BASE = f"https://{API_HOST}"
