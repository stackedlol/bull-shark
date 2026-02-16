import argparse
import logging
import sys

from src.logging_setup import setup_logging


def cmd_run(args):
    from src.bot.runner import BotRunner
    from src.bot.strategy import Strategy
    from src.coinbase.client import CoinbaseClient
    from src.config import DRY_RUN
    from src.storage.db import StateDB

    dry_run = args.dry_run if hasattr(args, "dry_run") else DRY_RUN
    products = args.products.split(",") if args.products else None

    client = CoinbaseClient(dry_run=dry_run)
    db = StateDB()
    strategy = Strategy()
    runner = BotRunner(client, db, strategy, products=products, dry_run=dry_run)

    try:
        runner.run_loop(once=args.once)
    finally:
        db.close()


def cmd_status(args):
    from src.bot.runner import BotRunner
    from src.bot.strategy import Strategy
    from src.coinbase.client import CoinbaseClient
    from src.storage.db import StateDB

    products = args.products.split(",") if args.products else None

    client = CoinbaseClient(dry_run=True)
    db = StateDB()
    strategy = Strategy()
    runner = BotRunner(client, db, strategy, products=products, dry_run=True)
    runner.print_status()
    db.close()


def cmd_test_auth(args):
    from src.coinbase.client import CoinbaseClient

    logger = logging.getLogger(__name__)
    client = CoinbaseClient(dry_run=False)

    try:
        accounts = client.get_accounts()
        print(f"Auth OK — found {len(accounts)} account(s):")
        for acct in accounts:
            currency = acct.get("currency", "?")
            balance = acct.get("available_balance", {}).get("value", "0")
            print(f"  {currency}: {balance}")
    except Exception as e:
        logger.error("Auth test failed: %s", e)
        sys.exit(1)


def cmd_watch(args):
    from src.bot.tui import LiveDashboard
    from src.coinbase.client import CoinbaseClient
    from src.storage.db import StateDB

    products = args.products.split(",") if args.products else None

    client = CoinbaseClient(dry_run=True)
    db = StateDB()
    dashboard = LiveDashboard(client, db, products=products, interval=args.interval)

    try:
        dashboard.run()
    finally:
        db.close()


def cmd_dry_run(args):
    args.dry_run = True
    cmd_run(args)


def main():
    parser = argparse.ArgumentParser(description="Bull Shark — Spot Trading Bot")
    parser.add_argument("--log-level", default=None, help="Override log level")

    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Run the trading bot")
    run_parser.add_argument("--once", action="store_true", help="Run one loop iteration then exit")
    run_parser.add_argument("--products", default=None, help="Comma-separated product IDs")
    run_parser.add_argument("--dry-run", action="store_true", help="Enable dry-run mode")
    run_parser.set_defaults(func=cmd_run)

    dry_parser = sub.add_parser("dry-run", help="Run in dry-run mode (no real orders)")
    dry_parser.add_argument("--once", action="store_true", help="Run one loop iteration then exit")
    dry_parser.add_argument("--products", default=None, help="Comma-separated product IDs")
    dry_parser.set_defaults(func=cmd_dry_run)

    status_parser = sub.add_parser("status", help="Show bot status dashboard")
    status_parser.add_argument("--products", default=None, help="Comma-separated product IDs")
    status_parser.set_defaults(func=cmd_status)

    watch_parser = sub.add_parser("watch", help="Live TUI dashboard")
    watch_parser.add_argument("--products", default=None, help="Comma-separated product IDs")
    watch_parser.add_argument("--interval", type=int, default=5, help="Refresh interval in seconds (default: 5)")
    watch_parser.set_defaults(func=cmd_watch)

    auth_parser = sub.add_parser("test-auth", help="Test API authentication")
    auth_parser.set_defaults(func=cmd_test_auth)

    args = parser.parse_args()

    if args.log_level:
        import src.config as cfg
        cfg.LOG_LEVEL = args.log_level.upper()

    setup_logging()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
