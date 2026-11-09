"""Entry point: python -m gmbot [config.toml] [-v]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .bot import Bot
from .config import load_config
from .irc import SASLError


def main() -> int:
    ap = argparse.ArgumentParser(prog="gmbot", description="LLM-backed IRC bot")
    ap.add_argument("config", nargs="?", default="config.toml")
    ap.add_argument("-v", "--verbose", action="store_true", help="log raw IRC traffic and LLM output")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as e:
        print(f"error loading config: {e}", file=sys.stderr)
        return 1
    if not cfg.irc.owner:
        logging.warning("irc.owner is not set: Libera.Chat requires bots to be attributable to their owner")

    try:
        asyncio.run(Bot(cfg).run())
    except SASLError as e:
        logging.error("%s", e)
        return 2
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
