import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from src.config import (
    ATR_PERIOD,
    COOLDOWN_SECONDS,
    DAILY_TRADE_CAP,
    EMA_LONG,
    EMA_SHORT,
    ESTIMATED_FEE_RATE,
    MIN_NOTIONAL,
    REBUY_ATR_MULTIPLIER,
    REBUY_DOWNTREND_MULTIPLIER,
    REBUY_DRIFT_THRESHOLD,
    REBUY_MIN_DISTANCE,
    REBUY_ORDER_TTL,
    TP_LADDER,
    TREND_THRESHOLD,
)

logger = logging.getLogger(__name__)


class Trend(Enum):
    UPTREND = "UPTREND"
    DOWNTREND = "DOWNTREND"
    SIDEWAYS = "SIDEWAYS"


@dataclass
class SellAction:
    product_id: str
    base_size: Decimal
    reason: str
    band_index: int


@dataclass
class RebuyAction:
    product_id: str
    limit_price: Decimal
    base_size: Decimal
    reason: str


@dataclass
class CancelRebuyAction:
    product_id: str
    order_id: str
    reason: str


@dataclass
class NoAction:
    product_id: str
    reason: str


def compute_ema(closes: list[Decimal], period: int) -> Decimal | None:
    if len(closes) < period:
        return None
    multiplier = Decimal(2) / (Decimal(period) + Decimal(1))
    ema = closes[0]
    for price in closes[1:period]:
        ema = (price - ema) * multiplier + ema
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def compute_atr(candles: list[dict], period: int = ATR_PERIOD) -> Decimal | None:
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        high = Decimal(candles[i]["high"])
        low = Decimal(candles[i]["low"])
        prev_close = Decimal(candles[i - 1]["close"])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    # Simple moving average of true ranges for the last `period` values
    recent = trs[-period:]
    return sum(recent) / Decimal(len(recent))


def detect_trend(closes: list[Decimal]) -> Trend:
    ema_short = compute_ema(closes, EMA_SHORT)
    ema_long = compute_ema(closes, EMA_LONG)
    if ema_short is None or ema_long is None:
        return Trend.SIDEWAYS
    spread = (ema_short - ema_long) / ema_long
    if spread > TREND_THRESHOLD:
        return Trend.UPTREND
    elif spread < -TREND_THRESHOLD:
        return Trend.DOWNTREND
    return Trend.SIDEWAYS


class Strategy:
    def evaluate(
        self,
        product_id: str,
        current_price: Decimal,
        state: dict | None,
        base_balance: Decimal,
        quote_balance: Decimal,
        candles: list[dict],
        daily_trade_count: int,
        now: float | None = None,
    ) -> list:
        if now is None:
            now = time.time()

        actions = []

        # Parse candle closes (oldest first - API returns newest first)
        sorted_candles = sorted(candles, key=lambda c: int(c.get("start", 0)))
        closes = [Decimal(c["close"]) for c in sorted_candles]

        trend = detect_trend(closes)
        atr = compute_atr(sorted_candles)

        # Initialize state defaults
        anchor_price = Decimal(state["anchor_price"]) if state and state.get("anchor_price") else None
        last_tp_band = state["last_tp_band"] if state else 0
        last_tp_ts = state["last_tp_timestamp"] if state and state.get("last_tp_timestamp") else 0
        rebuy_order_id = state["rebuy_order_id"] if state else None
        rebuy_placed_at = state["rebuy_placed_at"] if state and state.get("rebuy_placed_at") else 0
        rebuy_price = Decimal(state["rebuy_price"]) if state and state.get("rebuy_price") else None

        # If no anchor price set, current price becomes anchor
        if anchor_price is None:
            logger.info("%s | No anchor price, initializing to %s", product_id, current_price)
            return [NoAction(product_id=product_id, reason=f"anchor_init:{current_price}")]

        # --- Check stale rebuy order ---
        if rebuy_order_id and rebuy_order_id.startswith("dry-run-"):
            # In dry-run mode, always cancel stale rebuys
            if now - rebuy_placed_at > REBUY_ORDER_TTL:
                actions.append(CancelRebuyAction(
                    product_id=product_id, order_id=rebuy_order_id,
                    reason="stale_dry_run_rebuy"
                ))
                rebuy_order_id = None
        elif rebuy_order_id:
            age = now - rebuy_placed_at
            if age > REBUY_ORDER_TTL:
                actions.append(CancelRebuyAction(
                    product_id=product_id, order_id=rebuy_order_id,
                    reason=f"stale_order_age:{int(age)}s"
                ))
                rebuy_order_id = None
            elif rebuy_price is not None:
                drift = abs(current_price - rebuy_price) / rebuy_price
                if drift > REBUY_DRIFT_THRESHOLD:
                    actions.append(CancelRebuyAction(
                        product_id=product_id, order_id=rebuy_order_id,
                        reason=f"price_drift:{drift:.4f}"
                    ))
                    rebuy_order_id = None

        # --- Guards ---
        cooldown_ok = (now - last_tp_ts) >= COOLDOWN_SECONDS
        under_cap = daily_trade_count < DAILY_TRADE_CAP

        # --- Take-profit evaluation ---
        if cooldown_ok and under_cap and base_balance > 0:
            gain = (current_price - anchor_price) / anchor_price
            for i, (threshold, fraction) in enumerate(TP_LADDER):
                if i <= last_tp_band - 1:
                    continue  # Already sold this band
                if gain >= threshold:
                    sell_fraction = fraction
                    if trend == Trend.UPTREND:
                        sell_fraction = sell_fraction / Decimal(2)

                    sell_size = base_balance * sell_fraction
                    sell_notional = sell_size * current_price

                    # Check net profit after fees
                    gross = sell_notional
                    fees = gross * ESTIMATED_FEE_RATE * Decimal(2)  # buy + sell fees
                    cost_basis = sell_size * anchor_price
                    net = gross - cost_basis - fees
                    if net <= 0:
                        continue
                    if sell_notional < MIN_NOTIONAL:
                        continue

                    actions.append(SellAction(
                        product_id=product_id,
                        base_size=sell_size,
                        reason=f"tp_band_{i}:gain={gain:.4f}:trend={trend.value}",
                        band_index=i + 1,
                    ))
                    break  # One sell per loop

        # --- Re-buy evaluation ---
        if rebuy_order_id is None and cooldown_ok and under_cap and quote_balance >= MIN_NOTIONAL:
            distance = REBUY_MIN_DISTANCE
            if atr is not None and anchor_price > 0:
                atr_pct = atr / anchor_price
                atr_distance = atr_pct * REBUY_ATR_MULTIPLIER
                distance = max(distance, atr_distance)

            if trend == Trend.DOWNTREND:
                distance = distance * REBUY_DOWNTREND_MULTIPLIER

            rebuy_target = anchor_price * (Decimal(1) - distance)
            # Size: use up to 20% of quote balance
            rebuy_quote = min(quote_balance * Decimal("0.2"), quote_balance)
            rebuy_quote = max(rebuy_quote, MIN_NOTIONAL)
            if rebuy_quote > quote_balance:
                pass  # Not enough balance
            else:
                rebuy_size = rebuy_quote / rebuy_target

                if rebuy_quote >= MIN_NOTIONAL:
                    # Only place if target is below current price
                    if rebuy_target < current_price:
                        actions.append(RebuyAction(
                            product_id=product_id,
                            limit_price=rebuy_target,
                            base_size=rebuy_size,
                            reason=f"rebuy:dist={distance:.4f}:trend={trend.value}",
                        ))

        if not actions:
            actions.append(NoAction(
                product_id=product_id,
                reason=f"hold:gain={(current_price - anchor_price) / anchor_price if anchor_price else 0:.4f}:trend={trend.value}"
            ))

        return actions
