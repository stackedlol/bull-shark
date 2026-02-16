import signal
import time
from datetime import datetime
from decimal import Decimal

from rich.columns import Columns
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.bot.strategy import compute_atr, detect_trend
from src.coinbase.client import CoinbaseClient
from src.config import PRODUCTS
from src.storage.db import StateDB

SPARK_CHARS = "▁▂▃▄▅▆▇█"


def format_price(price: Decimal, product_id: str) -> str:
    if "BTC" in product_id:
        return f"${price:,.2f}"
    return f"${price:,.2f}"


def trend_text(trend) -> Text:
    name = trend.value
    colors = {"UPTREND": "green", "DOWNTREND": "red", "SIDEWAYS": "yellow"}
    arrows = {"UPTREND": " ^", "DOWNTREND": " v", "SIDEWAYS": " -"}
    return Text(name + arrows.get(name, ""), style=colors.get(name, "white"))


def tp_bar(band: int, total: int = 4) -> str:
    filled = "\u2588" * band
    empty = "\u2591" * (total - band)
    return f"{filled}{empty} {band}/{total}"


def build_candlestick_chart(candles: list[dict], height: int = 16, width: int = 24) -> Text:
    """Render a Unicode candlestick chart with price axis and volume bars."""
    if not candles or len(candles) < 2:
        return Text("  No candle data", style="dim")

    # Take last `width` candles
    display = candles[-width:]

    # Parse OHLCV
    parsed = []
    for c in display:
        parsed.append({
            "open": Decimal(c["open"]),
            "high": Decimal(c["high"]),
            "low": Decimal(c["low"]),
            "close": Decimal(c["close"]),
            "volume": Decimal(c.get("volume", "0")),
            "time": int(c.get("start", 0)),
        })

    # Price range across all candles
    all_highs = [p["high"] for p in parsed]
    all_lows = [p["low"] for p in parsed]
    price_max = max(all_highs)
    price_min = min(all_lows)
    price_range = price_max - price_min
    if price_range == 0:
        price_range = Decimal("1")

    # Volume range for bottom bar
    volumes = [p["volume"] for p in parsed]
    vol_max = max(volumes) if any(v > 0 for v in volumes) else Decimal("1")

    # Chart dimensions: `height` rows for price, 2 rows for volume, 1 for time axis
    chart_height = height

    # Build grid: each cell is (char, style)
    # Columns: axis_label (8 chars) + candle columns (2 chars each: candle + gap)
    col_count = len(parsed)

    # Initialize grid
    grid = [[(" ", "white") for _ in range(col_count)] for _ in range(chart_height)]

    # Map price to row (row 0 = top = price_max, row chart_height-1 = bottom = price_min)
    def price_to_row(price: Decimal) -> int:
        if price_range == 0:
            return chart_height // 2
        ratio = (price_max - price) / price_range
        row = int(ratio * (chart_height - 1))
        return max(0, min(chart_height - 1, row))

    # Draw each candle
    for col, p in enumerate(parsed):
        bullish = p["close"] >= p["open"]
        color = "green" if bullish else "red"

        body_top = price_to_row(max(p["open"], p["close"]))
        body_bot = price_to_row(min(p["open"], p["close"]))
        wick_top = price_to_row(p["high"])
        wick_bot = price_to_row(p["low"])

        # Draw wick above body
        for row in range(wick_top, body_top):
            grid[row][col] = ("│", color)

        # Draw body
        if body_top == body_bot:
            # Doji — single line
            grid[body_top][col] = ("─", color)
        else:
            for row in range(body_top, body_bot + 1):
                if bullish:
                    grid[row][col] = ("┃", f"bold {color}")
                else:
                    grid[row][col] = ("█", color)

        # Draw wick below body
        for row in range(body_bot + 1, wick_bot + 1):
            grid[row][col] = ("│", color)

    # Build price axis labels (show 5 levels)
    axis_labels = {}
    for i in range(5):
        row = int(i * (chart_height - 1) / 4)
        price = price_max - (price_range * Decimal(i) / Decimal(4))
        axis_labels[row] = f"{price:>10,.2f}"

    # Render chart as Text
    result = Text()

    # Top border
    result.append("  ┌" + "─" * (col_count + 12) + "┐\n", style="dim")

    for row in range(chart_height):
        # Price axis label
        if row in axis_labels:
            label = axis_labels[row]
            result.append(f"  │{label} ", style="dim cyan")
        else:
            result.append("  │           ", style="dim")

        # Candle chars
        for col in range(col_count):
            char, style = grid[row][col]
            result.append(char, style=style)

        result.append(" │\n", style="dim")

    # Volume bar row
    result.append("  │  Vol      ", style="dim")
    for col, p in enumerate(parsed):
        bullish = p["close"] >= p["open"]
        color = "green" if bullish else "red"
        if vol_max > 0:
            vol_ratio = float(p["volume"] / vol_max)
        else:
            vol_ratio = 0
        vol_idx = min(int(vol_ratio * 7), 7)
        result.append(SPARK_CHARS[vol_idx], style=color)
    result.append(" │\n", style="dim")

    # Time labels
    result.append("  └", style="dim")
    if parsed:
        first_ts = datetime.fromtimestamp(parsed[0]["time"]).strftime("%H:%M")
        last_ts = datetime.fromtimestamp(parsed[-1]["time"]).strftime("%H:%M")
        mid_idx = len(parsed) // 2
        mid_ts = datetime.fromtimestamp(parsed[mid_idx]["time"]).strftime("%H:%M")

        # Build time axis
        time_axis = first_ts.ljust(col_count // 2 + 6)
        remaining = col_count + 12 - len(time_axis) - len(last_ts)
        time_axis += mid_ts.center(max(remaining, 0))
        time_axis = time_axis[: col_count + 12 - len(last_ts)] + last_ts
        time_axis = time_axis[: col_count + 12]
        result.append(time_axis, style="dim cyan")

    result.append("┘\n", style="dim")

    return result


class LiveDashboard:
    def __init__(self, client: CoinbaseClient, db: StateDB, products: list[str] = None, interval: int = 5):
        self.client = client
        self.db = db
        self.products = products or PRODUCTS
        self.interval = interval
        self._running = True
        self.console = Console()

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        self._running = False

    def _fetch_product_data(self, product_id: str) -> dict:
        data = {"product_id": product_id, "error": None}
        try:
            # Price
            bid_ask = self.client.get_best_bid_ask([product_id])
            pricebooks = bid_ask.get("pricebooks", [])
            if pricebooks:
                book = pricebooks[0]
                bid = Decimal(book["bids"][0]["price"]) if book.get("bids") else None
                ask = Decimal(book["asks"][0]["price"]) if book.get("asks") else None
                if bid and ask:
                    data["bid"] = bid
                    data["ask"] = ask
                    data["mid"] = (bid + ask) / 2
                    data["spread"] = ask - bid

            # Candles
            candles = self.client.get_candles(product_id, "ONE_HOUR", 30)
            sorted_candles = sorted(candles, key=lambda c: int(c.get("start", 0)))
            closes = [Decimal(c["close"]) for c in sorted_candles]
            data["closes"] = closes
            data["candles"] = sorted_candles
            data["trend"] = detect_trend(closes)
            data["atr"] = compute_atr(sorted_candles)

            # 24h change
            if len(closes) >= 24:
                old = closes[-24]
                new = closes[-1]
                data["change_24h"] = (new - old) / old * 100
            elif len(closes) >= 2:
                data["change_24h"] = (closes[-1] - closes[0]) / closes[0] * 100

            # Balances
            base = product_id.split("-")[0]
            quote = product_id.split("-")[1]
            data["base_balance"] = self.client.get_balance(base)
            data["base_currency"] = base
            data["quote_balance"] = self.client.get_balance(quote)
            data["quote_currency"] = quote

            # DB state
            data["state"] = self.db.get_product_state(product_id)
            data["daily_trades"] = self.db.get_daily_trade_count(product_id)
            data["recent_trades"] = self.db.get_recent_trades(product_id, limit=5)

        except Exception as e:
            data["error"] = str(e)

        return data

    def _build_chart_panel(self, data: dict) -> Panel:
        product_id = data["product_id"]
        candles = data.get("candles", [])

        if data.get("error") or not candles:
            return Panel(Text("  No chart data", style="dim"), title=f"{product_id} Chart", border_style="dim")

        chart = build_candlestick_chart(candles, height=14, width=24)
        border_color = "green" if data.get("change_24h", 0) >= 0 else "red"
        return Panel(chart, title=f"[bold]{product_id} 1H Candles[/bold]", border_style=border_color, padding=(0, 1))

    def _build_product_panel(self, data: dict) -> Panel:
        product_id = data["product_id"]

        if data.get("error"):
            return Panel(
                Text(f"Error: {data['error']}", style="red"),
                title=product_id, border_style="red",
            )

        lines = []

        # Price line
        mid = data.get("mid")
        if mid:
            price_str = format_price(mid, product_id)
            change = data.get("change_24h")
            if change is not None:
                color = "green" if change >= 0 else "red"
                sign = "+" if change >= 0 else ""
                price_line = Text(f"  {price_str}  ", style="bold white")
                price_line.append(f"{sign}{change:.2f}%", style=color)
            else:
                price_line = Text(f"  {price_str}", style="bold white")
            lines.append(price_line)

            # Bid/Ask
            bid = data.get("bid")
            ask = data.get("ask")
            if bid and ask:
                lines.append(Text(f"  Bid: {format_price(bid, product_id)}  Ask: {format_price(ask, product_id)}", style="dim"))

        # Trend + ATR
        trend = data.get("trend")
        atr = data.get("atr")
        if trend:
            line = Text("  Trend: ")
            line.append_text(trend_text(trend))
            if atr:
                line.append(f"  ATR: {atr:.2f}", style="dim")
            lines.append(line)

        lines.append(Text(""))

        # Balances
        base_bal = data.get("base_balance", Decimal(0))
        quote_bal = data.get("quote_balance", Decimal(0))
        base_cur = data.get("base_currency", "?")
        quote_cur = data.get("quote_currency", "?")
        lines.append(Text(f"  {base_cur}: {base_bal:.8f}", style="white"))
        lines.append(Text(f"  {quote_cur}: ${quote_bal:.2f}", style="white"))

        # Bot state
        state = data.get("state")
        if state:
            lines.append(Text(""))
            anchor = state.get("anchor_price")
            if anchor and mid:
                anchor_d = Decimal(anchor)
                gain = (mid - anchor_d) / anchor_d * 100
                gain_color = "green" if gain >= 0 else "red"
                sign = "+" if gain >= 0 else ""
                line = Text(f"  Anchor: {format_price(anchor_d, product_id)}  ")
                line.append(f"{sign}{gain:.2f}%", style=gain_color)
                lines.append(line)

            band = state.get("last_tp_band", 0)
            lines.append(Text(f"  TP:     {tp_bar(band)}", style="white"))
            lines.append(Text(f"  Trades: {data.get('daily_trades', 0)}/20 today", style="dim"))

            rebuy_id = state.get("rebuy_order_id")
            if rebuy_id:
                rebuy_price = state.get("rebuy_price", "?")
                rebuy_size = state.get("rebuy_size", "?")
                placed_at = state.get("rebuy_placed_at", 0)
                age_min = int((time.time() - placed_at) / 60) if placed_at else 0
                lines.append(Text(f"  Rebuy:  {rebuy_size} @ ${rebuy_price} ({age_min}m ago)", style="yellow"))
            else:
                lines.append(Text("  Rebuy:  none", style="dim"))
        else:
            lines.append(Text("\n  No bot state yet", style="dim"))

        content = Text("\n").join(lines)
        border_color = "green" if data.get("change_24h", 0) >= 0 else "red"
        return Panel(content, title=f"[bold]{product_id}[/bold]", border_style=border_color, padding=(1, 0))

    def _build_trades_table(self, all_data: list[dict]) -> Panel:
        table = Table(show_header=True, header_style="bold", expand=True, padding=(0, 1))
        table.add_column("Time", style="dim", width=12)
        table.add_column("Product", width=10)
        table.add_column("Side", width=5)
        table.add_column("Size", width=14)
        table.add_column("Price", width=14)
        table.add_column("Reason", ratio=1)

        trades = []
        for data in all_data:
            for t in data.get("recent_trades", []):
                t["_product"] = data["product_id"]
                trades.append(t)

        trades.sort(key=lambda t: t["created_at"], reverse=True)

        for t in trades[:10]:
            ts = datetime.fromtimestamp(t["created_at"]).strftime("%m-%d %H:%M")
            side_style = "green" if t["side"] == "BUY" else "red"
            table.add_row(
                ts, t["_product"], Text(t["side"], style=side_style),
                str(t["size"]), f"${t['price']}", t.get("reason", ""),
            )

        if not trades:
            table.add_row("—", "—", "—", "—", "—", "No trades yet")

        return Panel(table, title="[bold]Recent Trades[/bold]", border_style="blue")

    def _build_layout(self) -> Table:
        all_data = []
        for product_id in self.products:
            all_data.append(self._fetch_product_data(product_id))

        # Header
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = Text(f"  Bull Shark  |  {now}  |  Refresh: {self.interval}s", style="bold cyan")

        # Charts side by side
        charts = [self._build_chart_panel(d) for d in all_data]

        # Info panels side by side
        panels = [self._build_product_panel(d) for d in all_data]

        # Full layout as a vertical stack
        layout = Table.grid(expand=True)
        layout.add_row(Panel(header, style="bold", border_style="cyan"))
        layout.add_row(Columns(charts, equal=True, expand=True))
        layout.add_row(Columns(panels, equal=True, expand=True))
        layout.add_row(self._build_trades_table(all_data))

        return layout

    def run(self):
        with Live(self._build_layout(), console=self.console, refresh_per_second=1, screen=True) as live:
            while self._running:
                try:
                    live.update(self._build_layout())
                except Exception:
                    pass  # Don't crash on transient API errors

                for _ in range(self.interval):
                    if not self._running:
                        break
                    time.sleep(1)
