"""Read official Ultimate Canada Ultimate Central tournament tenants.

Ultimate Canada publishes CUC tournament tenants on Ultimate Central. This
adapter keeps that tenant provenance separate from the European Ultimate
Central tenant and from the optional Canadian Ultimate Database source.

Examples:
    python -m scraper.ultimate_canada --list
    python -m scraper.ultimate_canada --event 123 --base-url https://cuc2026.ultimatecentral.com
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import requests

from .euf import EUF_DB, ingest_ultimate_central_event, init_db
from .ultimate_central import UltimateCentralClient

SOURCE_PREFIX = "ultimate-central:"
DEFAULT_BASE_URL = os.environ.get(
    "ULTIMATE_CANADA_BASE_URL", "https://cuc2026.ultimatecentral.com"
)

def source_for_client(client: UltimateCentralClient) -> str:
    host = urlparse(client.base_url).hostname
    if not host:
        raise ValueError("Ultimate Canada base URL must include a hostname")
    return SOURCE_PREFIX + host


def ingest_event(con, client: UltimateCentralClient, provider_event_id: str | int):
    """Ingest one official Canada tenant event with Canada provenance."""
    return ingest_ultimate_central_event(
        con,
        client,
        provider_event_id,
        source=source_for_client(client),
        event_url_base=client.base_url,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--db", type=Path, default=EUF_DB)
    parser.add_argument("--event")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--request-budget", type=int, default=500)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.list and not args.backfill and not args.event:
        raise SystemExit("choose --list, --backfill, or --event")
    session = requests.Session()
    client = UltimateCentralClient(
        session, base_url=args.base_url, request_budget=args.request_budget, backoff=1.0
    )
    try:
        if args.list:
            result = client.list_events(per_page=100, order_by="date_asc")
            print(json.dumps({
                "source": source_for_client(client),
                "base_url": client.base_url,
                "state": result.state,
                "events": result.items,
                "requests": client.requests_made,
            }, indent=2, sort_keys=True))
            return 0
        con = init_db(args.db)
        try:
            if args.event:
                result = ingest_event(con, client, args.event)
                print(json.dumps(result, indent=2, sort_keys=True))
                return 0
            result = client.list_events(per_page=100, order_by="date_asc")
            failures = []
            for event in result.items:
                try:
                    ingested = ingest_event(con, client, event["id"])
                    print(json.dumps({"event": event["id"], "result": ingested}))
                except Exception as exc:
                    failures.append(f"{event.get('id')}: {exc}")
            for failure in failures:
                print(f"UNAVAILABLE {failure}")
            return 1 if failures else 0
        finally:
            con.close()
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
