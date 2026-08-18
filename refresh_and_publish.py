#!/usr/bin/env python3
"""Refresh current USAU results, rebuild Elo, and publish the GitHub Pages site.

Usage: .venv/bin/python refresh_and_publish.py [--season YEAR ...] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"
USAU_DB = DATA / "usau.db"
EUF_DB = DATA / "euf.db"

PUBLISHED_PATHS = (
    "data/ambiguities.csv",
    "data/cross_division_links.csv",
    "data/player_elo.csv",
    "data/player_roles.csv",
    "data/team_elo.csv",
    "data/team_elo_best.csv",
    "data/team_elo_upcoming.csv",
    "docs",
)


class PublishError(RuntimeError):
    """A precondition failed before a publication command ran."""


def _run(command: Sequence[str], *, env: dict[str, str] | None = None) -> None:
    print(f"+ {shlex.join(command)}", flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def _capture(command: Sequence[str]) -> str:
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout.strip()


def pipeline_commands(seasons: Sequence[int]) -> tuple[tuple[str, ...], ...]:
    """Top up finished events, then rebuild every published site artifact."""
    years = tuple(str(season) for season in seasons)
    python = sys.executable
    return (
        (python, "-m", "scraper.refresh", *years),
        (python, "-m", "scraper.structure", *years),
        (python, "-m", "identity.resolve"),
        (python, "-m", "analysis.rankings"),
        (python, "-m", "analysis.site"),
        (
            python,
            "-m",
            "unittest",
            "tests/test_refresh_and_publish.py",
            "tests/test_refresh.py",
            "tests/test_tournaments.py",
            "tests/test_site_assets.py",
        ),
    )


def publication_env() -> dict[str, str]:
    """Pin publication inputs and outputs to this checkout."""
    env = os.environ.copy()
    env["USAU_GQL_DB"] = str(USAU_DB)
    env["RANKINGS_DATA_DIR"] = str(DATA)
    env["RANKINGS_SITE_OUT"] = str(DOCS / "index.html")
    env.pop("RATING_NAME", None)
    return env


def ensure_ready(remote: str, branch: str) -> None:
    """Refuse states that could publish partial data or unrelated edits."""
    missing = [path.relative_to(ROOT) for path in (USAU_DB, EUF_DB) if not path.is_file()]
    if missing:
        names = ", ".join(map(str, missing))
        raise PublishError(
            f"missing source database(s): {names}; rebuild the corpus before refreshing"
        )

    dirty = _capture(("git", "status", "--porcelain", "--untracked-files=all"))
    if dirty:
        raise PublishError(
            "the worktree is not clean; commit, stash, or remove these changes first:\n"
            + dirty
        )

    current = _capture(("git", "symbolic-ref", "--quiet", "--short", "HEAD"))
    if current != branch:
        raise PublishError(
            f"checked out branch is {current!r}, but publication branch is {branch!r}"
        )
    _capture(("git", "remote", "get-url", remote))


def verify_outputs() -> None:
    required = (
        DATA / "player_elo.csv",
        DATA / "team_elo.csv",
        DATA / "history.json",
        DOCS / "index.html",
        DOCS / "history.js",
    )
    bad = [path.relative_to(ROOT) for path in required if not path.is_file() or path.stat().st_size == 0]
    if bad:
        raise PublishError(
            "publication produced missing or empty output(s): " + ", ".join(map(str, bad))
        )


def print_plan(seasons: Sequence[int], remote: str, branch: str, message: str) -> None:
    print(f"Seasons: {', '.join(map(str, seasons))}")
    print(f"Publication: {remote}/{branch}")
    print(f"+ git pull --ff-only {shlex.quote(remote)} {shlex.quote(branch)}")
    for command in pipeline_commands(seasons):
        print(f"+ {shlex.join(command)}")
    print(f"+ git diff --check -- {' '.join(map(shlex.quote, PUBLISHED_PATHS))}")
    print(f"+ git add -- {' '.join(map(shlex.quote, PUBLISHED_PATHS))}")
    print(f"+ git commit -m {shlex.quote(message)}")
    print(f"+ git push {shlex.quote(remote)} HEAD:{shlex.quote(branch)}")


def publish(
    seasons: Sequence[int],
    *,
    remote: str,
    branch: str,
    message: str,
) -> bool:
    """Run the refresh pipeline and push its tracked artifacts. Returns changed."""
    ensure_ready(remote, branch)
    _run(("git", "pull", "--ff-only", remote, branch))

    env = publication_env()
    for command in pipeline_commands(seasons):
        _run(command, env=env)
    verify_outputs()

    _run(("git", "diff", "--check", "--", *PUBLISHED_PATHS))
    _run(("git", "add", "--", *PUBLISHED_PATHS))
    unchanged = subprocess.run(
        ("git", "diff", "--cached", "--quiet"), cwd=ROOT, check=False
    ).returncode == 0
    if unchanged:
        print("No published data changed; nothing to commit or push.")
        return False

    _run(("git", "diff", "--cached", "--check"))
    _run(("git", "commit", "-m", message))
    _run(("git", "push", remote, f"HEAD:{branch}"))
    return True


def _season(value: str) -> int:
    season = int(value)
    if season < 2014:
        raise argparse.ArgumentTypeError("USAU mirror coverage begins in 2014")
    return season


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh USAU tournaments, rebuild Elo, and publish GitHub Pages."
    )
    parser.add_argument(
        "--season",
        action="append",
        type=_season,
        dest="seasons",
        help="season to refresh; repeat for multiple seasons (default: current year)",
    )
    parser.add_argument("--remote", default="origin", help="git remote (default: origin)")
    parser.add_argument("--branch", default="main", help="Pages branch (default: main)")
    parser.add_argument("--message", help="commit message")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print every command without reading data, changing files, or using the network",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    seasons = tuple(sorted(set(args.seasons or (date.today().year,))))
    message = args.message or f"Refresh rankings through {date.today().isoformat()}"
    if args.dry_run:
        print_plan(seasons, args.remote, args.branch, message)
        return 0
    try:
        changed = publish(
            seasons,
            remote=args.remote,
            branch=args.branch,
            message=message,
        )
    except (PublishError, subprocess.CalledProcessError) as error:
        print(f"publication stopped: {error}", file=sys.stderr)
        return 1
    if changed:
        print(f"Published refreshed rankings to {args.remote}/{args.branch}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
