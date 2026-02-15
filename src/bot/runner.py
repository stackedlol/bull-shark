import logging
import signal
import time
from datetime import datetime
from decimal import Decimal, ROUND_DOWN

from src.bot.strategy import (
    CancelRebuyAction,
    NoAction,
    RebuyAction,
    SellAction,
    detect_trend,
)
from src.coinbase.client import CoinbaseAPIError, CoinbaseClient
from src.config import LOOP_INTERVAL, PRODUCTS
from src.storage.db import StateDB

logger = logging.getLogger(__name__)


class BotRunner:
    def __init__(
        self, client: CoinbaseClient, db: StateDB, strategy, products: list[str] = None, dry_run: bool = False
    ):
        self.client = client
        self.db = db
        self.strategy = strategy
        self.products = products or PRODUCTS
        self.dry_run = dry_run
        self._running = True

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        logger.info("Received signal %d, shutting down gracefully...", signum)
        self._running = False

    def reconcile(self):
        logger.info("Reconciling state with exchange...")
        for product_id in self.products:
            state = self.db.get_product_state(product_id)
            if state is None or not state.get("rebuy_order_id"):
                continue

            order_id = state["rebuy_order_id"]
            if order_id.startswith("dry-run-"):
                logger.info("%s | Clearing dry-run rebuy order %s", product_id, order_id)
                self.db.clear_rebuy_order(product_id)
                continue

            try:
                resp = self.client.get_order(order_id)
                order = resp.get("order", resp)
                status = order.get("status", "UNKNOWN")
                logger.info("%s | Rebuy order %s status: %s", product_id, order_id, status)

                if status in ("FILLED", "COMPLETED"):
                    fill_price = Decimal(order.get("average_filled_price", state.get("rebuy_price", "0")))
                    fill_size = Decimal(order.get("filled_size", state.get("rebuy_size", "0")))
                    fee = Decimal(order.get("total_fees", "0"))

                    self.db.record_trade(
                        product_id=product_id, side="BUY", order_type="limit",
                        order_id=order_id, price=fill_price, size=fill_size,
                        quote_total=fill_price * fill_size, fee=fee,
                        reason="rebuy_filled_on_reconcile",
                    )
                    # Update anchor to blended average
                    old_anchor = Decimal(state.get("anchor_price", "0"))
                    if old_anchor > 0:
                        new_anchor = (old_anchor + fill_price) / Decimal(2)
                    else:
                        new_anchor = fill_price
                    self.db.upsert_product_state(product_id, anchor_price=str(new_anchor))
                    self.db.clear_rebuy_order(product_id)
                    self.db.increment_daily_trades(product_id)
                    logger.info("%s | Rebuy filled at %s, new anchor %s", product_id, fill_price, new_anchor)

                elif status in ("CANCELLED", "EXPIRED", "FAILED"):
                    self.db.clear_rebuy_order(product_id)
                    logger.info("%s | Rebuy order %s was %s, cleared", product_id, order_id, status)

                # OPEN/PENDING → leave as-is

            except CoinbaseAPIError as e:
                logger.warning("%s | Failed to check rebuy order %s: %s", product_id, order_id, e)

        logger.info("Reconciliation complete")

    def run_loop(self, once: bool = False):
        self.reconcile()

        while self._running:
            for product_id in self.products:
                if not self._running:
                    break
                try:
                    self._process_product(product_id)
                except Exception:
                    logger.exception("%s | Error processing product", product_id)

            if once:
                break

            logger.debug("Sleeping %ds...", LOOP_INTERVAL)
            # Sleep in small increments for responsive shutdown
            for _ in range(LOOP_INTERVAL):
                if not self._running:
                    break
                time.sleep(1)

        logger.info("Bot loop ended")

    def _process_product(self, product_id: str):
        # Fetch current price
        bid_ask = self.client.get_best_bid_ask([product_id])
        pricebooks = bid_ask.get("pricebooks", [])
        if not pricebooks:
            logger.warning("%s | No price data", product_id)
            return

        book = pricebooks[0]
        best_bid = Decimal(book["bids"][0]["price"]) if book.get("bids") else None
        best_ask = Decimal(book["asks"][0]["price"]) if book.get("asks") else None
        if best_bid is None or best_ask is None:
            logger.warning("%s | Incomplete bid/ask", product_id)
            return
        mid_price = (best_bid + best_ask) / Decimal(2)

        # Fetch candles
        candles = self.client.get_candles(product_id, "ONE_HOUR", 50)

        # Fetch balances
        base_currency = product_id.split("-")[0]
        quote_currency = product_id.split("-")[1]
        base_balance = self.client.get_balance(base_currency)
        quote_balance = self.client.get_balance(quote_currency)

        # Load state
        state = self.db.get_product_state(product_id)
        daily_count = self.db.get_daily_trade_count(product_id)

        # Initialize anchor if needed
        if state is None or not state.get("anchor_price"):
            self.db.upsert_product_state(product_id, anchor_price=str(mid_price), avg_entry_price=str(mid_price))
            state = self.db.get_product_state(product_id)
            logger.info("%s | Initialized anchor price at %s", product_id, mid_price)

        # Evaluate strategy
        actions = self.strategy.evaluate(
            product_id=product_id,
            current_price=mid_price,
            state=state,
            base_balance=base_balance,
            quote_balance=quote_balance,
            candles=candles,
            daily_trade_count=daily_count,
        )

        # Detect trend for logging
        sorted_candles = sorted(candles, key=lambda c: int(c.get("start", 0)))
        closes = [Decimal(c["close"]) for c in sorted_candles]
        trend = detect_trend(closes)
        anchor = state.get("anchor_price", "N/A") if state else "N/A"
        rebuy_id = state.get("rebuy_order_id", "none") if state else "none"

        action_strs = []
        for action in actions:
            result = self._execute_action(product_id, action, state)
            action_strs.append(result)

        logger.info(
            "%s | price=%s | bid=%s ask=%s | base=%s quote=%s | anchor=%s | trend=%s | "
            "tp_band=%s | rebuy=%s | trades=%d | actions=[%s]",
            product_id, mid_price, best_bid, best_ask,
            base_balance, quote_balance, anchor, trend.value,
            state.get("last_tp_band", 0) if state else 0,
            rebuy_id, daily_count,
            ", ".join(action_strs),
        )

    def _execute_action(self, product_id: str, action, state: dict | None) -> str:
        if isinstance(action, SellAction):
            return self._execute_sell(product_id, action, state)
        elif isinstance(action, RebuyAction):
            return self._execute_rebuy(product_id, action)
        elif isinstance(action, CancelRebuyAction):
            return self._execute_cancel(product_id, action)
        elif isinstance(action, NoAction):
            logger.debug("%s | No action: %s", product_id, action.reason)
            return f"no_action:{action.reason}"
        return "unknown_action"

    def _execute_sell(self, product_id: str, action: SellAction, state: dict | None) -> str:
        # Round base_size down to 8 decimal places
        size_str = str(action.base_size.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN))

        try:
            resp = self.client.create_market_order(product_id, "SELL", base_size=size_str)
            order_id = resp.get("success_response", {}).get("order_id", resp.get("order_id", "unknown"))

            # Estimate fill for recording
            bid_ask = self.client.get_best_bid_ask([product_id])
            price_est = Decimal("0")
            pricebooks = bid_ask.get("pricebooks", [])
            if pricebooks and pricebooks[0].get("bids"):
                price_est = Decimal(pricebooks[0]["bids"][0]["price"])

            quote_total = action.base_size * price_est
            fee_est = quote_total * Decimal("0.006")

            self.db.record_trade(
                product_id=product_id, side="SELL", order_type="market",
                order_id=order_id, price=price_est, size=action.base_size,
                quote_total=quote_total, fee=fee_est, reason=action.reason,
            )
            self.db.upsert_product_state(
                product_id, last_tp_band=action.band_index, last_tp_timestamp=time.time()
            )
            self.db.increment_daily_trades(product_id)

            logger.info("%s | SELL %s @ ~%s | reason=%s", product_id, size_str, price_est, action.reason)
            return f"sell:{size_str}@~{price_est}"

        except CoinbaseAPIError as e:
            logger.error("%s | Sell failed: %s", product_id, e)
            return f"sell_error:{e}"

    def _execute_rebuy(self, product_id: str, action: RebuyAction) -> str:
        # Round appropriately
        price_str = str(action.limit_price.quantize(Decimal("0.01"), rounding=ROUND_DOWN))
        size_str = str(action.base_size.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN))

        try:
            resp = self.client.create_limit_order(
                product_id, "BUY", base_size=size_str, limit_price=price_str, post_only=True
            )
            order_id = resp.get("success_response", {}).get("order_id", resp.get("order_id", "unknown"))

            self.db.set_rebuy_order(product_id, order_id, action.limit_price, action.base_size)

            logger.info(
                "%s | REBUY limit %s @ %s | reason=%s",
                product_id, size_str, price_str, action.reason,
            )
            return f"rebuy:{size_str}@{price_str}"

        except CoinbaseAPIError as e:
            logger.error("%s | Rebuy failed: %s", product_id, e)
            return f"rebuy_error:{e}"

    def _execute_cancel(self, product_id: str, action: CancelRebuyAction) -> str:
        if action.order_id.startswith("dry-run-"):
            self.db.clear_rebuy_order(product_id)
            logger.info("%s | Cleared dry-run rebuy: %s", product_id, action.reason)
            return f"cancel_dry_run:{action.reason}"

        try:
            self.client.cancel_orders([action.order_id])
            self.db.clear_rebuy_order(product_id)
            logger.info("%s | Cancelled rebuy %s: %s", product_id, action.order_id, action.reason)
            return f"cancel:{action.reason}"
        except CoinbaseAPIError as e:
            logger.error("%s | Cancel failed: %s", product_id, e)
            # Clear from DB anyway since order may already be gone
            self.db.clear_rebuy_order(product_id)
            return f"cancel_error:{e}"

    def print_status(self):
        print("=" * 80)
        print(f"  Coinbase Trading Bot Status — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  Mode: {'DRY-RUN' if self.dry_run else 'LIVE'}")
        print("=" * 80)

        for product_id in self.products:
            state = self.db.get_product_state(product_id)
            daily_count = self.db.get_daily_trade_count(product_id)
            recent_trades = self.db.get_recent_trades(product_id, limit=5)

            print(f"\n  {product_id}")
            print(f"  {'─' * 40}")

            if state:
                print(f"  Anchor price:     {state.get('anchor_price', 'N/A')}")
                print(f"  Avg entry price:  {state.get('avg_entry_price', 'N/A')}")
                print(f"  TP band:          {state.get('last_tp_band', 0)}/4")
                print(f"  Daily trades:     {daily_count}/{20}")
                rebuy = state.get("rebuy_order_id")
                if rebuy:
                    print(f"  Active rebuy:     {rebuy}")
                    print(f"    Price:          {state.get('rebuy_price', 'N/A')}")
                    print(f"    Size:           {state.get('rebuy_size', 'N/A')}")
                else:
                    print("  Active rebuy:     none")
            else:
                print("  No state yet (bot hasn't run for this product)")

            if recent_trades:
                print(f"\n  Recent trades:")
                for t in recent_trades:
                    ts = datetime.fromtimestamp(t["created_at"]).strftime("%m-%d %H:%M")
                    print(f"    {ts} | {t['side']:4s} | {t['size']} @ {t['price']} | {t['reason']}")

        print(f"\n{'=' * 80}")
