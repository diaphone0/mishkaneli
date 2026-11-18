#!/usr/bin/env python3
"""
Reads prayer-times data via a LOCAL CACHE FILE, refreshing it by invoking
fetch_prayer_times.py only when the cache is missing or older than a
configurable max-age (default 300s/5min) - NOT on every call. This is the
script meant to be invoked repeatedly/frequently (e.g. as a shul-luah
`#CMD:<...>` tag executable) without re-hitting the mygabay.com server on
every single invocation, which risks tripping rate limits/blocks.

fetch_prayer_times.py itself is UNCHANGED by this script - it still always
makes a real HTTP request and still supports its own `chol`/`shabbos`/`json`
modes directly, for direct/one-off use. This script instead always fetches
in `json` mode (into the cache file) and does its OWN chol/shabbos text
formatting from the cached JSON, so the on-disk cache always has the full
structured data available regardless of which mode this script is called
with next time.

Usage:
    python get_prayer_times.py                        # prints full cached JSON
    python get_prayer_times.py chol                    # weekday times, tab-separated
    python get_prayer_times.py shabbos                 # Shabbos/Yom-Tov times, tab-separated
    python get_prayer_times.py chol_times               # weekday TIMES only, one per line
    python get_prayer_times.py chol_desc                 # weekday DESCRIPTIONS only, one per line
    python get_prayer_times.py shabbos_times             # Shabbos/Yom-Tov TIMES only
    python get_prayer_times.py shabbos_desc               # Shabbos/Yom-Tov DESCRIPTIONS only
    python get_prayer_times.py chol --sep " - "         # custom separator
    python get_prayer_times.py chol --max-desc-width 15 # wrap descriptions longer than 15 chars
    python get_prayer_times.py chol --max-age 60        # refresh cache if older than 60s
    python get_prayer_times.py chol --cache-file x.json # use a different cache file location
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CACHE_FILE = HERE / "prayer_times.json"
DEFAULT_FETCH_SCRIPT = HERE / "fetch_prayer_times.py"
DEFAULT_MAX_AGE_SECONDS = 300.0
DEFAULT_SEP = "\t"
DEFAULT_FETCH_TIMEOUT_SECONDS = 30.0


def _is_cache_fresh(cache_file: Path, max_age_seconds: float) -> bool:
    if not cache_file.is_file():
        return False
    age = time.time() - cache_file.stat().st_mtime
    return age < max_age_seconds


def _refresh_cache(cache_file: Path, fetch_script: Path, timeout: float) -> None:
    """Invokes `fetch_prayer_times.py --out cache_file` (default json mode)
    as a subprocess, under the SAME Python interpreter running this script.
    Raises on any failure (missing script, network error, non-zero exit,
    timeout) - the caller decides whether a stale cache is an acceptable
    fallback."""
    env = os.environ.copy()
    # Same PYTHONIOENCODING fix used elsewhere in this codebase for any
    # subprocess that might print non-ASCII text - harmless here too, since
    # fetch_prayer_times.py's --out path already writes the file itself
    # with explicit encoding="utf-8", but its own stderr status messages
    # ("Wrote ...") could otherwise still be subject to console-encoding
    # mojibake if this script's own caller ever inspects captured stderr.
    env["PYTHONIOENCODING"] = "utf-8:backslashreplace"
    subprocess.run(
        [sys.executable, str(fetch_script), "--out", str(cache_file)],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def ensure_cache(cache_file: Path, fetch_script: Path, max_age_seconds: float, timeout: float) -> None:
    """Refreshes cache_file (via _refresh_cache) only if it's missing or
    older than max_age_seconds. If a refresh is attempted and fails but an
    existing (now-stale) cache file is still present, falls back to using
    it anyway (with a warning) rather than failing outright - preferable
    for unattended signage use, where stale data is far better than no
    data at all. Only exits/raises if there's truly no usable cache file
    at all (first-ever run with no network, e.g.)."""
    if _is_cache_fresh(cache_file, max_age_seconds):
        return
    try:
        _refresh_cache(cache_file, fetch_script, timeout)
    except Exception as exc:
        if cache_file.is_file():
            print(f"Warning: refreshing prayer-times cache failed ({exc}); using existing cache.", file=sys.stderr)
        else:
            print(f"Error: could not fetch prayer times and no cache file exists yet: {exc}", file=sys.stderr)
            sys.exit(1)


def _wrap_desc(desc: str, max_desc_width: int | None) -> list[str]:
    """Wraps a single description string at word boundaries so no line exceeds
    ``max_desc_width`` characters (breaking a single word only if that word
    itself exceeds ``max_desc_width``). When ``max_desc_width`` is None or <= 0,
    returns ``[desc]`` unchanged."""
    if max_desc_width is None or max_desc_width <= 0 or not desc:
        return [desc]
    wrapped = textwrap.wrap(
        desc,
        width=max_desc_width,
        break_long_words=True,
        break_on_hyphens=False,
    )
    return wrapped if wrapped else [""]


def format_times(
    rows: list[dict],
    sep: str = DEFAULT_SEP,
    swap_order: bool = False,
    hide_empty_sep: bool = False,
    fields: str = "both",
    pad_with_tab: bool = False,
    max_desc_width: int | None = None,
) -> str:
    """Renders every row (including label-only rows with an empty "time")
    as one line each (or multiple lines when ``max_desc_width`` wraps a long
    "desc") - unlike fetch_prayer_times.py's own formatter, nothing is skipped
    here, since some entries legitimately have only a desc and no time (e.g.
    "קבלת שבת", "הקפות").

    When ``max_desc_width`` is set to a positive integer, any row whose "desc"
    exceeds that character count is word-wrapped into multiple lines:
    - The first wrapped line keeps the row's original "time".
    - Each continuation line is treated as having an empty "time" (``""``), so:
      * In ``fields="desc"`` mode, the wrapped lines appear on consecutive lines.
      * In ``fields="time"`` mode, an extra empty line is emitted for each
        continuation line of the corresponding "desc", keeping every row's time
        vertically aligned with the start of its description across a paired
        ``*_times`` / ``*_desc`` layout.
      * In ``fields="both"`` mode, continuation lines are formatted just like
        empty-time rows (respecting ``hide_empty_sep``, ``pad_with_tab``, ``sep``,
        and ``swap_order``) so they stay aligned with the description column.

    fields controls what each line actually contains:
    - "both" (default): "<time><sep><desc>" (or "<desc><sep><time>" if
      swap_order=True) - sep/swap_order/hide_empty_sep/pad_with_tab only
      matter in this mode.
    - "time": just each row's "time" value, one per line - including an
      EMPTY line for a row whose time is empty (label-only rows are
      still represented, just as a blank line, so line N always
      corresponds to the same row N across a "time"/"desc" pair of
      calls).
    - "desc": just each row's "desc" value, one per line.

    swap_order=True (only meaningful for fields="both") prints
    "<desc><sep><time>" instead (desc first).

    hide_empty_sep=True (only meaningful for fields="both") omits the
    LITERAL separator character(s) for a row whose "time" is empty, but
    still reserves a placeholder in its place so every line stays
    visually column-aligned - rather than the label-only row's desc
    abruptly starting at a different column than every other row's desc.
    Exactly what that placeholder looks like depends on pad_with_tab:

    - pad_with_tab=False (default): the placeholder is made of literal
      SPACE characters, sized to reserve the same amount of horizontal
      space the hidden field(s) would have occupied (see below for
      exactly how that width is measured). This only lines up visually
      in a MONOSPACE font, where every character (including a space) has
      the same fixed width - in a proportional font (Arial, Calibri,
      etc.), a counted number of spaces generally does NOT occupy the
      same width as the digits/letters they're standing in for, so the
      desc column will NOT actually line up despite the character counts
      matching.
    - pad_with_tab=True: the placeholder is a single TAB character
      instead of counted spaces - this works correctly in ANY font
      (proportional or monospace), but requires the deck author to set
      up a real tab stop in the PowerPoint/Impress textbox at the
      column where the desc should start (Format > Paragraph > Tabs, or
      the ruler) - a tab character always jumps to the next configured
      tab stop at a fixed physical position, independent of font
      metrics, unlike a run of space characters. For this to look right,
      ``sep`` itself should also be (or include) a tab character for the
      WITH-TIME rows, so every row's desc - whether it followed a real
      time or a hidden one - lands on that exact same tab stop.

    The SPACE placeholder's reserved width (pad_with_tab=False only) is
    based on whichever field normally comes BEFORE the (now-hidden)
    separator in the chosen order:
    - swap_order=False (default, "<time><sep><desc>"): the desc is
      LEFT-padded by (length of the times + length of sep) spaces - the
      "length of the times" is the longest of the dataset's own actual
      (non-empty) "time" strings (normally a constant "HH:MM" width, but
      measured rather than hardcoded, so this adapts automatically if
      that format ever changes). If EVERY row happens to have an empty
      time (a degenerate dataset with no real times at all), there's
      nothing to measure a time width from - falls back to the longest
      "desc" instead, just so the padding amount is still a reasonable,
      non-zero value rather than collapsing to no padding at all.
    - swap_order=True ("<desc><sep><time>"): the desc itself is
      RIGHT-padded up to the length of the dataset's longest "desc" (so
      every hidden-separator row's desc reaches the same width a
      maximal-length desc would), followed by a trailing blank-space
      placeholder (length of sep) where the separator would have gone -
      mirroring the non-swapped case's placeholder, just positioned
      after the desc instead of before it, since desc comes first in
      this order.

    Rows that DO have a non-empty "time" are entirely unaffected by any
    of the above (sep/time/desc are rendered normally in both orders)
    - only rows actually benefiting from ``hide_empty_sep`` get a
    placeholder at all."""
    expanded_rows: list[dict] = []
    for row in rows:
        wrapped_desc = _wrap_desc(row["desc"], max_desc_width)
        expanded_rows.append({"desc": wrapped_desc[0], "time": row["time"]})
        for cont_line in wrapped_desc[1:]:
            expanded_rows.append({"desc": cont_line, "time": ""})

    if fields == "time":
        return "\n".join(row["time"] for row in expanded_rows)
    if fields == "desc":
        return "\n".join(row["desc"] for row in expanded_rows)

    ref_width = 0
    if hide_empty_sep and not pad_with_tab:
        if swap_order:
            ref_width = max((len(row["desc"]) for row in expanded_rows), default=0)
        else:
            time_lengths = [len(row["time"]) for row in expanded_rows if row["time"]]
            ref_width = max(time_lengths) if time_lengths else max(
                (len(row["desc"]) for row in expanded_rows), default=0
            )

    lines = []
    for row in expanded_rows:
        time_, desc = row["time"], row["desc"]
        if hide_empty_sep and not time_:
            if pad_with_tab:
                lines.append(f"{desc}\t" if swap_order else f"\t{desc}")
            elif swap_order:
                lines.append(desc.ljust(ref_width) + " " * len(sep))
            else:
                lines.append(" " * (ref_width + len(sep)) + desc)
            continue
        if swap_order:
            lines.append(f"{desc}{sep}{time_}")
        else:
            lines.append(f"{time_}{sep}{desc}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "mode",
        nargs="?",
        default="json",
        choices=["json", "chol", "shabbos", "chol_times", "chol_desc", "shabbos_times", "shabbos_desc"],
        help=(
            "Output format: 'json' (full cached data), 'chol'/'shabbos' (time+desc lines, "
            "tab-separated by default), or 'chol_times'/'chol_desc'/'shabbos_times'/"
            "'shabbos_desc' (only the time, or only the desc, one per line - including an "
            "empty line for a row with no time)."
        ),
    )
    parser.add_argument("--sep", default=DEFAULT_SEP, help='Separator between time and desc. Default: tab ("\\t").')
    parser.add_argument(
        "--swap",
        action="store_true",
        help='Print "<desc><sep><time>" instead of the default "<time><sep><desc>".',
    )
    parser.add_argument(
        "--hide-empty-sep",
        action="store_true",
        help=(
            "Omit the literal separator for rows with an empty time, but still pad the desc "
            "so it stays column-aligned with rows that do have a time (see format_times's "
            "docstring for exactly how the padding width is computed)."
        ),
    )
    parser.add_argument(
        "--pad-with-tab",
        action="store_true",
        help=(
            "Only meaningful together with --hide-empty-sep: use a single TAB character as "
            "the alignment placeholder instead of counted spaces. Spaces only line up visually "
            "in a monospace font; a tab jumps to a real tab stop position regardless of font, "
            "so this is the better choice for a proportional (non-monospace) font - but "
            "requires the deck's textbox to have an actual tab stop configured at the desired "
            "column (and --sep should itself be/include a tab too, for the with-time rows to "
            "land on that same stop)."
        ),
    )
    parser.add_argument(
        "--max-desc-width",
        "--desc-width",
        dest="max_desc_width",
        type=int,
        default=None,
        help=(
            "Maximum character width for each description ('desc'). Descriptions longer than "
            "this limit are word-wrapped onto continuation lines; in '*_times' modes, extra "
            "blank lines are emitted accordingly so times stay aligned with wrapped descriptions."
        ),
    )
    parser.add_argument(
        "--cache-file", type=Path, default=DEFAULT_CACHE_FILE, help="Path to the local cache JSON file."
    )
    parser.add_argument(
        "--fetch-script", type=Path, default=DEFAULT_FETCH_SCRIPT, help="Path to fetch_prayer_times.py."
    )
    parser.add_argument(
        "--max-age",
        type=float,
        default=DEFAULT_MAX_AGE_SECONDS,
        help=f"Refresh the cache if it's older than this many seconds. Default: {DEFAULT_MAX_AGE_SECONDS:.0f}.",
    )
    parser.add_argument(
        "--fetch-timeout",
        type=float,
        default=DEFAULT_FETCH_TIMEOUT_SECONDS,
        help=f"Timeout (seconds) for the underlying fetch subprocess. Default: {DEFAULT_FETCH_TIMEOUT_SECONDS:.0f}.",
    )
    args = parser.parse_args()

    ensure_cache(args.cache_file, args.fetch_script, args.max_age, args.fetch_timeout)
    data = json.loads(args.cache_file.read_text(encoding="utf-8"))

    # mode -> (dataset key in the cached JSON, fields passed to format_times)
    mode_map = {
        "chol": ("weekday", "both"),
        "shabbos": ("shabbos", "both"),
        "chol_times": ("weekday", "time"),
        "chol_desc": ("weekday", "desc"),
        "shabbos_times": ("shabbos", "time"),
        "shabbos_desc": ("shabbos", "desc"),
    }

    if args.mode in mode_map:
        dataset_key, fields = mode_map[args.mode]
        text = format_times(
            data[dataset_key],
            args.sep,
            args.swap,
            args.hide_empty_sep,
            fields,
            args.pad_with_tab,
            args.max_desc_width,
        )
    else:
        text = json.dumps(data, ensure_ascii=False, indent=2)

    print(text)


if __name__ == "__main__":
    main()
