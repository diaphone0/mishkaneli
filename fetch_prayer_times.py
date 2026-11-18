#!/usr/bin/env python3
"""
Fetches the current prayer-times ("zmanei tefila") board content from the
old mygabay.com signage system and prints it as JSON.

The board's preview URL (http://mygabay.com/board/tfilot/preview?app=<token>)
embeds its own access token directly in the `app=` query parameter - this is
a self-contained, server-validated credential (an ASP.NET Core Data
Protection payload), NOT a session cookie. Confirmed via direct testing: a
plain unauthenticated HTTP GET to this exact URL returns the fully
server-rendered HTML with live data - no browser, cookies, or login session
is required at all, despite the page itself being part of a system that
normally requires a login to administer.

Board URLs are intentionally NOT stored in source code so this script can live
safely in a public repository. They are resolved in order from:
1. ``--url`` CLI flag
2. Environment variables ``MYGABAY_SEPHARDI_URL`` / ``MYGABAY_ASHKENAZI_URL``
   (e.g. GitHub Actions Secrets)
3. A local ``secrets.json`` (or ``.env``) file next to this script (gitignored on public repos).

Usage:
    python fetch_prayer_times.py                    # prints JSON to stdout (sephardi)
    python fetch_prayer_times.py chol               # prints weekday times as "<time> - <desc>" lines
    python fetch_prayer_times.py shabbos            # prints Shabbos/Yom-Tov times the same way
    python fetch_prayer_times.py --board ashkenazi  # fetch the Ashkenazi minyan board
    python fetch_prayer_times.py --out times.json   # writes output to a file instead
    python fetch_prayer_times.py --url "https://..." # override the board URL
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

try:
    import requests
except ImportError:  # pragma: no cover - allows stdlib-only unit tests to import helpers
    requests = None  # type: ignore[assignment]

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None  # type: ignore[assignment,misc]

HERE = Path(__file__).resolve().parent
DEFAULT_SECRETS_FILE = HERE / "secrets.json"
DEFAULT_DOTENV_FILE = HERE / ".env"

BOARD_ENV_VARS: dict[str, str] = {
    "sephardi": "MYGABAY_SEPHARDI_URL",
    "ashkenazi": "MYGABAY_ASHKENAZI_URL",
}
BOARD_NAMES: tuple[str, ...] = tuple(BOARD_ENV_VARS.keys())

_APP_TOKEN_RE = re.compile(r"([?&]app=)[^\s'\"&<>)]+", re.IGNORECASE)
_URL_QUERY_RE = re.compile(r"(https?://[^\s'\"?<>)]+)\?[^\s'\"<>)]+", re.IGNORECASE)


def redact_url_secrets(text: str) -> str:
    """Scrubs ``app=`` tokens and URL query strings from ``text`` so board
    tokens never leak into stderr or public CI logs."""
    scrubbed = _APP_TOKEN_RE.sub(r"\1<redacted>", text)
    return _URL_QUERY_RE.sub(r"\1?<redacted>", scrubbed)


def _load_dotenv_dict(dotenv_path: Path) -> dict[str, str]:
    if not dotenv_path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            values[key] = val
    return values


def _load_secrets_json(secrets_path: Path) -> dict[str, str]:
    if not secrets_path.is_file():
        return {}
    raw = json.loads(secrets_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{secrets_path}: expected a JSON object")
    return {str(k): str(v).strip() for k, v in raw.items() if isinstance(v, str)}


def resolve_board_url(
    board: str = "sephardi",
    *,
    url_override: str | None = None,
    env: Mapping[str, str] | None = None,
    secrets_path: Path | None = None,
    dotenv_path: Path | None = None,
) -> str:
    """Resolves the target preview URL for ``board`` from ``url_override``,
    environment variables, local ``secrets.json``, or local ``.env``."""
    if url_override and url_override.strip():
        return url_override.strip()

    if board not in BOARD_ENV_VARS:
        valid = ", ".join(BOARD_NAMES)
        raise ValueError(f"Unknown board {board!r} (expected one of: {valid})")

    env_var = BOARD_ENV_VARS[board]
    env_map = os.environ if env is None else env
    from_env = env_map.get(env_var, "").strip()
    if from_env:
        return from_env

    effective_secrets = DEFAULT_SECRETS_FILE if secrets_path is None else secrets_path
    secrets_data = _load_secrets_json(effective_secrets)
    for key in (f"{board}_url", board, env_var):
        val = secrets_data.get(key, "").strip()
        if val:
            return val

    effective_dotenv = DEFAULT_DOTENV_FILE if dotenv_path is None else dotenv_path
    dotenv_data = _load_dotenv_dict(effective_dotenv)
    from_dotenv = dotenv_data.get(env_var, "").strip()
    if from_dotenv:
        return from_dotenv

    raise ValueError(
        f"Missing URL for '{board}' board: set environment variable {env_var}, "
        f"add \"{board}_url\" to {effective_secrets.name}, or pass --url."
    )


def fetch_board_html(url: str, timeout: float = 15.0) -> str:
    if requests is None:
        raise RuntimeError("The 'requests' package is required to fetch boards (pip install requests).")
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.text


def _parse_column(col) -> list[dict]:
    """Parses one `<div class="col">` into a list of {"desc", "time"} dicts,
    one per `<div class="line">` inside it. `time` is "" for label-only rows
    that have no actual clock time (e.g. "קבלת שבת", "הקפות")."""
    rows = []
    for line in col.select(".line"):
        desc_el = line.select_one(".desc")
        spans = line.find_all("span")
        desc = desc_el.get_text(strip=True) if desc_el else ""
        # The time is whichever <span> isn't the .desc one - by construction
        # each .line has exactly two <span> children (desc, time).
        time_el = next((s for s in spans if s is not desc_el), None)
        time_text = time_el.get_text(strip=True) if time_el else ""
        rows.append({"desc": desc, "time": time_text})
    return rows


def parse_board(html: str) -> dict:
    if BeautifulSoup is None:
        raise RuntimeError(
            "The 'beautifulsoup4' package is required to parse boards (pip install beautifulsoup4)."
        )
    soup = BeautifulSoup(html, "html.parser")

    top = soup.select_one("#top")
    date_text = ""
    if top is not None:
        date_div = top.find("div", recursive=False)
        if date_div is not None:
            date_text = date_div.get_text(" ", strip=True)

    name_el = soup.select_one("#name")
    shul_name = name_el.get_text(strip=True) if name_el else ""

    cols = soup.select("#main .col")
    weekday = _parse_column(cols[0]) if len(cols) > 0 else []
    shabbos = _parse_column(cols[1]) if len(cols) > 1 else []

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "page_date": date_text,
        "shul_name": shul_name,
        "weekday": weekday,
        "shabbos": shabbos,
    }


def format_times(rows: list[dict]) -> str:
    """Renders a list of {"desc", "time"} rows (as produced by _parse_column)
    as plain text, one line per row: "<time> - <desc>". Rows with no actual
    clock time (label-only rows like "קבלת שבת"/"הקפות" - see _parse_column's
    docstring) are skipped entirely, since "<empty> - <desc>" isn't a
    meaningful line for this format."""
    lines = [f'{row["time"]} - {row["desc"]}' for row in rows if row.get("time")]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        nargs="?",
        default="json",
        choices=["json", "chol", "shabbos"],
        help=(
            "Output format: 'json' (default, full structured data), 'chol' (weekday times as "
            "'<time> - <desc>' lines), or 'shabbos' (same, for Shabbos/Yom-Tov times)."
        ),
    )
    parser.add_argument(
        "--board",
        choices=list(BOARD_NAMES),
        default="sephardi",
        help="Named board preset to fetch (ignored if --url is explicitly given). Default: sephardi.",
    )
    parser.add_argument("--url", default=None, help="Override the board preview URL directly.")
    parser.add_argument(
        "--secrets-file",
        type=Path,
        default=None,
        help=f"Optional path to JSON secrets file (defaults to {DEFAULT_SECRETS_FILE.name} next to script).",
    )
    parser.add_argument("--out", type=str, default=None, help="Write output to this file instead of stdout.")
    parser.add_argument("--indent", type=int, default=2, help="JSON indent (0 for compact). Default: 2.")
    args = parser.parse_args()

    try:
        target_url = resolve_board_url(
            args.board,
            url_override=args.url,
            secrets_path=args.secrets_file,
        )
    except Exception as exc:
        print(f"Error resolving board URL: {redact_url_secrets(str(exc))}", file=sys.stderr)
        sys.exit(1)

    try:
        html = fetch_board_html(target_url)
    except Exception as exc:
        print(f"Error fetching '{args.board}' board: {redact_url_secrets(str(exc))}", file=sys.stderr)
        sys.exit(1)

    data = parse_board(html)

    if args.mode == "chol":
        text = format_times(data["weekday"])
    elif args.mode == "shabbos":
        text = format_times(data["shabbos"])
    else:
        text = json.dumps(data, ensure_ascii=False, indent=args.indent or None)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"Wrote {args.out}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
