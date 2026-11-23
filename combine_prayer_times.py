#!/usr/bin/env python3
"""
Queries both shul boards (Sephardi and Ashkenazi) via ``fetch_prayer_times.py``,
merges their prayer schedules into unified lists, and writes three JSON files plus
an HTML report:

1. ``unified_prayer_times.json`` — Combined schedule matching ``fetch_prayer_times.py``'s
   JSON structure (both ``"weekday"`` and ``"shabbos"`` top-level keys, so it can also
   be passed directly to ``get_prayer_times.py --cache-file unified_prayer_times.json``).
2. ``unified_chol.json`` — Unified weekday (chol) schedule JSON.
3. ``unified_shabbos.json`` — Unified Shabbos / Yom-Tov schedule JSON.
4. ``unified_prayer_times.html``, ``unified_chol.html``, ``unified_shabbos.html`` —
   Self-contained RTL Hebrew HTML reports for combined, weekday-only, and
   Shabbos/Yom-Tov-only schedules (disable with ``--no-html``).

By default:
- Rows are merged chronologically:
  * On Shabbos/Yom-Tov, Friday / Erev Shabbos afternoon & evening rows stay before
    Shabbos morning rows across the midnight wrap.
  * Label-only / timeless rows (e.g. ``"קבלת שבת"``, ``"ערבית - לאחר מנחה"``) remain
    attached right below the preceding timed row from their own board.
  * Completely blank padding rows (both ``desc`` and ``time`` empty) are stripped.
- Each row's ``"desc"`` is suffixed in Hebrew with its minyan (``"(ספרדי)"`` / ``"(אשכנזי)"``)
  and each JSON row object also carries ``"minyan": "sephardi" | "ashkenazi"``.

Usage:
    python combine_prayer_times.py                          # auto-loads config.json if present
    python combine_prayer_times.py --config custom.json
    python combine_prayer_times.py --pattern=".*המדרש.*"
    python combine_prayer_times.py --title "בית הכנסת משכן עלי" --subtitle "זמני חורף תשפ\"ז"
    python combine_prayer_times.py --stdout
    python combine_prayer_times.py --no-desc-suffix
    python combine_prayer_times.py --order concat
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
import html as html_lib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from config import (  # noqa: E402
    CombineConfig,
    DEFAULT_ASHKENAZI_LABEL,
    DEFAULT_CONFIG_PATH,
    DEFAULT_SEPHARDI_LABEL,
    ExtraTableConfig,
    ScheduleGroupConfig,
    VALID_SINGLE_ROW_LABEL_STYLES,
    _parse_extra_tables_field,
    _parse_group_unmatched_order,
    _parse_patterns_field,
    _parse_schedule_groups_field,
    apply_cli_overrides,
    load_config,
)
from fetch_prayer_times import redact_url_secrets  # noqa: E402

DEFAULT_FETCH_SCRIPT = HERE / "fetch_prayer_times.py"
DEFAULT_COMBINED_OUT = HERE / "unified_prayer_times.json"
DEFAULT_CHOL_OUT = HERE / "unified_chol.json"
DEFAULT_SHABBOS_OUT = HERE / "unified_shabbos.json"
DEFAULT_HTML_OUT = HERE / "unified_prayer_times.html"
DEFAULT_CHOL_HTML_OUT = HERE / "unified_chol.html"
DEFAULT_SHABBOS_HTML_OUT = HERE / "unified_shabbos.html"
DEFAULT_FETCH_TIMEOUT_SECONDS = 30.0

_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_DAY_WRAP_DROP_MINUTES = 60


@dataclass
class _RowBlock:
    """An anchor timed row together with any immediately following timeless rows
    from the same board, kept together as an atomic unit when sorting."""

    board_order: int
    seq_index: int
    is_leading_timeless: bool
    day_index: int
    minutes: int
    rows: list[dict] = field(default_factory=list)


def _parse_hhmm(time_str: str) -> int | None:
    """Parses ``"HH:MM"`` (or ``"H:MM"``) into minutes since midnight, or returns
    ``None`` for empty / non-clock strings."""
    match = _HHMM_RE.match(time_str.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def fetch_board_via_script(
    fetch_script: Path,
    board_name: str,
    url_override: str | None = None,
    timeout: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
) -> dict:
    """Invokes ``fetch_prayer_times.py`` as a subprocess to query one board and
    returns its parsed JSON dictionary."""
    cmd = [sys.executable, str(fetch_script)]
    if url_override:
        cmd.extend(["--url", url_override])
    else:
        cmd.extend(["--board", board_name])

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8:backslashreplace"

    try:
        proc = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        stderr_msg = redact_url_secrets((exc.stderr or "").strip())
        safe_exc = redact_url_secrets(str(exc))
        raise RuntimeError(f"Failed fetching '{board_name}' board: {stderr_msg or safe_exc}") from None

    return json.loads(proc.stdout)


def fetch_both_boards(
    fetch_script: Path,
    sephardi_url: str | None = None,
    ashkenazi_url: str | None = None,
    timeout: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
) -> tuple[dict, dict]:
    """Queries both the Sephardi and Ashkenazi boards concurrently via
    ``fetch_prayer_times.py``."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_sephardi = pool.submit(fetch_board_via_script, fetch_script, "sephardi", sephardi_url, timeout)
        fut_ashkenazi = pool.submit(fetch_board_via_script, fetch_script, "ashkenazi", ashkenazi_url, timeout)
        return fut_sephardi.result(), fut_ashkenazi.result()


def _compile_patterns(
    patterns: str | re.Pattern[str] | Sequence[str | re.Pattern[str]] | None,
) -> list[re.Pattern[str]]:
    if patterns is None:
        return []
    if isinstance(patterns, (str, re.Pattern)):
        items: Sequence[str | re.Pattern[str]] = [patterns]
    else:
        items = patterns
    return [p if isinstance(p, re.Pattern) else re.compile(p) for p in items]


def prepare_board_rows(
    raw_rows: list[dict],
    minyan: str,
    hebrew_label: str,
    suffix_desc: bool = True,
    no_suffix_patterns: str | re.Pattern[str] | Sequence[str | re.Pattern[str]] | None = None,
) -> list[dict]:
    """Cleans raw ``{"desc", "time"}`` rows from one board, dropping completely
    blank rows and attaching ``minyan`` metadata (plus optional Hebrew suffix on
    ``desc``, skipped whenever ``desc`` matches ``no_suffix_patterns``)."""
    compiled_patterns = _compile_patterns(no_suffix_patterns)
    prepared: list[dict] = []
    for row in raw_rows:
        desc = (row.get("desc") or "").strip()
        time_str = (row.get("time") or "").strip()
        if not desc and not time_str:
            continue
        skip_suffix = any(p.search(desc) for p in compiled_patterns)
        if suffix_desc and hebrew_label and not skip_suffix:
            final_desc = f"{desc} ({hebrew_label})" if desc else f"({hebrew_label})"
        else:
            final_desc = desc
        prepared.append(
            {
                "desc": final_desc,
                "time": time_str,
                "minyan": minyan,
            }
        )
    return prepared


def _group_into_blocks(rows: list[dict], board_order: int, is_shabbos: bool) -> list[_RowBlock]:
    """Groups one board's prepared rows into atomic blocks so timeless rows stay
    attached immediately below their preceding timed row, while tracking day wraps."""
    blocks: list[_RowBlock] = []
    day_index = 0
    prev_minutes: int | None = None
    saw_evening = False

    for row in rows:
        mins = _parse_hhmm(row["time"])
        if mins is None:
            if blocks:
                blocks[-1].rows.append(row)
            else:
                blocks.append(
                    _RowBlock(
                        board_order=board_order,
                        seq_index=0,
                        is_leading_timeless=True,
                        day_index=-1,
                        minutes=-1,
                        rows=[row],
                    )
                )
            continue

        if is_shabbos:
            if prev_minutes is not None and mins < prev_minutes - _DAY_WRAP_DROP_MINUTES:
                day_index += 1
            block_day = day_index
            prev_minutes = mins
        else:
            if mins >= 18 * 60:
                saw_evening = True
            # Keep late-night post-midnight entries (00:00–02:59, e.g. Selichot)
            # after evening entries rather than sorting before morning Shacharit,
            # while still sorting daytime entries (e.g. 13:00 Friday Mincha) into
            # midday even if entered out of order on a single board.
            block_day = 1 if (saw_evening and mins < 3 * 60) else 0

        blocks.append(
            _RowBlock(
                board_order=board_order,
                seq_index=len(blocks),
                is_leading_timeless=False,
                day_index=block_day,
                minutes=mins,
                rows=[row],
            )
        )

    return blocks


def _align_shabbos_days(board_blocks_list: list[list[_RowBlock]]) -> None:
    """If one Shabbos board includes Friday / Erev Shabbos afternoon entries
    (wrapping to ``day_index == 1`` for Shabbos morning) while another board
    starts directly with Shabbos morning (``minutes < 12:00`` with ``day_index == 0``),
    aligns the morning-only board onto Shabbos day so Saturday morning doesn't
    sort ahead of Friday afternoon."""
    timed_per_board = [
        [b for b in blocks if not b.is_leading_timeless]
        for blocks in board_blocks_list
    ]
    max_day = max((b.day_index for timed in timed_per_board for b in timed), default=0)
    if max_day == 0:
        return

    for timed in timed_per_board:
        if not timed:
            continue
        board_max_day = max(b.day_index for b in timed)
        if board_max_day < max_day and timed[0].minutes < 12 * 60:
            shift = max_day - board_max_day
            for b in timed:
                b.day_index += shift


def merge_schedule_rows(
    board_rows_list: list[list[dict]],
    *,
    is_shabbos: bool,
    order: str = "chronological",
) -> list[dict]:
    """Merges prepared row lists from multiple boards according to ``order``:
    - ``"chronological"``: Multi-day-aware chronological merge keeping timeless
      rows attached to their preceding row.
    - ``"time"``: Simple ascending ``HH:MM`` sort (leading timeless rows at end,
      attached timeless rows kept with their preceding timed row).
    - ``"concat"``: Simple concatenation in board order.
    """
    if order == "concat":
        return [row for rows in board_rows_list for row in rows]

    board_blocks_list = [
        _group_into_blocks(rows, board_order=idx, is_shabbos=(is_shabbos and order == "chronological"))
        for idx, rows in enumerate(board_rows_list)
    ]

    if is_shabbos and order == "chronological":
        _align_shabbos_days(board_blocks_list)

    all_blocks = [b for blocks in board_blocks_list for b in blocks]
    if order == "time":
        all_blocks.sort(
            key=lambda b: (
                1 if b.is_leading_timeless else 0,
                b.minutes,
                b.board_order,
                b.seq_index,
            )
        )
    else:
        all_blocks.sort(
            key=lambda b: (
                0 if b.is_leading_timeless else 1,
                b.day_index,
                b.minutes,
                b.board_order,
                b.seq_index,
            )
        )

    return [row for block in all_blocks for row in block.rows]


def combine_boards(
    sephardi_data: dict,
    ashkenazi_data: dict,
    *,
    sephardi_label: str = DEFAULT_SEPHARDI_LABEL,
    ashkenazi_label: str = DEFAULT_ASHKENAZI_LABEL,
    suffix_desc: bool = True,
    no_suffix_patterns: str | re.Pattern[str] | Sequence[str | re.Pattern[str]] | None = None,
    order: str = "chronological",
) -> tuple[dict, dict, dict]:
    """Builds ``(combined_payload, chol_payload, shabbos_payload)`` from two
    parsed board dicts (as returned by ``fetch_prayer_times.py``)."""
    compiled_patterns = _compile_patterns(no_suffix_patterns)

    sephardi_chol = prepare_board_rows(
        sephardi_data.get("weekday", []),
        minyan="sephardi",
        hebrew_label=sephardi_label,
        suffix_desc=suffix_desc,
        no_suffix_patterns=compiled_patterns,
    )
    ashkenazi_chol = prepare_board_rows(
        ashkenazi_data.get("weekday", []),
        minyan="ashkenazi",
        hebrew_label=ashkenazi_label,
        suffix_desc=suffix_desc,
        no_suffix_patterns=compiled_patterns,
    )

    sephardi_shabbos = prepare_board_rows(
        sephardi_data.get("shabbos", []),
        minyan="sephardi",
        hebrew_label=sephardi_label,
        suffix_desc=suffix_desc,
        no_suffix_patterns=compiled_patterns,
    )
    ashkenazi_shabbos = prepare_board_rows(
        ashkenazi_data.get("shabbos", []),
        minyan="ashkenazi",
        hebrew_label=ashkenazi_label,
        suffix_desc=suffix_desc,
        no_suffix_patterns=compiled_patterns,
    )

    unified_weekday = merge_schedule_rows(
        [sephardi_chol, ashkenazi_chol],
        is_shabbos=False,
        order=order,
    )
    unified_shabbos = merge_schedule_rows(
        [sephardi_shabbos, ashkenazi_shabbos],
        is_shabbos=True,
        order=order,
    )

    fetched_at = datetime.now(timezone.utc).isoformat()
    page_date = (sephardi_data.get("page_date") or ashkenazi_data.get("page_date") or "").strip()

    names: list[str] = []
    for raw_name in (sephardi_data.get("shul_name"), ashkenazi_data.get("shul_name")):
        cleaned = " ".join((raw_name or "").split())
        if cleaned and cleaned not in names:
            names.append(cleaned)
    combined_shul_name = " / ".join(names)

    boards_meta = {
        "sephardi": {
            "fetched_at": sephardi_data.get("fetched_at", ""),
            "page_date": sephardi_data.get("page_date", ""),
            "shul_name": sephardi_data.get("shul_name", ""),
        },
        "ashkenazi": {
            "fetched_at": ashkenazi_data.get("fetched_at", ""),
            "page_date": ashkenazi_data.get("page_date", ""),
            "shul_name": ashkenazi_data.get("shul_name", ""),
        },
    }

    combined_payload = {
        "fetched_at": fetched_at,
        "page_date": page_date,
        "shul_name": combined_shul_name,
        "boards": boards_meta,
        "weekday": unified_weekday,
        "shabbos": unified_shabbos,
    }
    chol_payload = {
        "fetched_at": fetched_at,
        "mode": "chol",
        "page_date": page_date,
        "shul_name": combined_shul_name,
        "boards": boards_meta,
        "weekday": unified_weekday,
    }
    shabbos_payload = {
        "fetched_at": fetched_at,
        "mode": "shabbos",
        "page_date": page_date,
        "shul_name": combined_shul_name,
        "boards": boards_meta,
        "shabbos": unified_shabbos,
    }
    return combined_payload, chol_payload, shabbos_payload


def _format_updated_at(iso_timestamp: str) -> str:
    if not iso_timestamp:
        return ""
    try:
        dt = datetime.fromisoformat(iso_timestamp).astimezone()
        return dt.strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return iso_timestamp


_QUANTIFIER_BRACE_RE = re.compile(r"^\{\d+(?:,\d*)?\}\??")


def _clean_single_regex_fragment(fragment: str) -> str:
    s = re.sub(r"\(\?(?:[aiLmsux]+|[=!<][^)]*)\)", "", fragment)
    s = re.sub(r"\(\?(?:P<[^>]+>|:)", "(", s)

    out: list[str] = []
    i = 0
    n = len(s)

    def _skip_quantifier(pos: int) -> int:
        while pos < n:
            if s[pos] in "*+?":
                pos += 1
            elif s[pos] == "{":
                m = _QUANTIFIER_BRACE_RE.match(s[pos:])
                if m:
                    pos += m.end()
                else:
                    break
            else:
                break
        return pos

    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:
            nxt = s[i + 1]
            i += 2
            if nxt == "s":
                out.append(" ")
                i = _skip_quantifier(i)
            elif nxt in "bBAZdDwW":
                i = _skip_quantifier(i)
            else:
                out.append(nxt)
        elif ch == "[":
            i += 1
            if i < n and s[i] == "^":
                i += 1
            if i < n and s[i] == "]":
                i += 1
            while i < n and s[i] != "]":
                if s[i] == "\\" and i + 1 < n:
                    i += 2
                else:
                    i += 1
            if i < n and s[i] == "]":
                i += 1
            i = _skip_quantifier(i)
        elif ch == "{":
            m = _QUANTIFIER_BRACE_RE.match(s[i:])
            if m:
                i += m.end()
            else:
                out.append(ch)
                i += 1
        elif ch in "^$.*+?()":
            i += 1
        else:
            out.append(ch)
            i += 1

    return " ".join("".join(out).split()).strip("()").strip()


def trim_regex_to_label(pattern_str: str, matched_text: str = "") -> str:
    """Extracts a human-readable label from a regex pattern by stripping regex
    metacharacters (e.g. ``".*המדרש.*"`` -> ``"המדרש"``)."""
    stripped = re.sub(r"\(\?(?:[aiLmsux]+|[=!<][^)]*)\)", "", pattern_str)
    branches = re.split(r"(?<!\\)\|", stripped)
    cleaned_branches = [b for b in (_clean_single_regex_fragment(br) for br in branches) if b]
    if not cleaned_branches:
        return ""
    if len(cleaned_branches) == 1:
        return cleaned_branches[0]
    if matched_text:
        for branch in cleaned_branches:
            if branch in matched_text:
                return branch
    return " / ".join(cleaned_branches)


def _join_hebrew_labels(labels: list[str]) -> str:
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} ו{labels[1]}"
    return f"{', '.join(labels[:-1])} ו{labels[-1]}"


def _resolve_grouped_row_label(
    row: dict,
    *,
    sephardi_label: str,
    ashkenazi_label: str,
    suffix_desc: bool,
    no_suffix_pairs: list[tuple[str, re.Pattern[str]]],
    group_no_suffix_label: str | None,
) -> str:
    minyan = str(row.get("minyan") or "")
    if minyan == "sephardi":
        minyan_label = sephardi_label
    elif minyan == "ashkenazi":
        minyan_label = ashkenazi_label
    else:
        minyan_label = ""

    desc = str(row.get("desc") or "").strip()
    expected_suffix = f" ({minyan_label})" if (suffix_desc and minyan_label) else ""

    if expected_suffix and desc.endswith(expected_suffix):
        candidate_base = desc[: -len(expected_suffix)].rstrip()
        if not any(pat_re.search(candidate_base) for _, pat_re in no_suffix_pairs):
            return minyan_label

    matched_pat_str = next(
        (pat_str for pat_str, pat_re in no_suffix_pairs if pat_re.search(desc)),
        None,
    )
    was_unsuffixed = bool(expected_suffix and not desc.endswith(expected_suffix))

    if matched_pat_str is not None or was_unsuffixed:
        mode_val = (group_no_suffix_label or "").strip()
        if not mode_val or mode_val.lower() == "none":
            return ""
        if mode_val.lower() == "pattern":
            return trim_regex_to_label(matched_pat_str, desc) if matched_pat_str else ""
        if mode_val.lower() == "minyan":
            return minyan_label
        return mode_val

    return minyan_label if suffix_desc else ""


def _format_stacked_row_title_html(
    raw_desc: str,
    minyan: str,
    *,
    sephardi_label: str = DEFAULT_SEPHARDI_LABEL,
    ashkenazi_label: str = DEFAULT_ASHKENAZI_LABEL,
    suffix_desc: bool = True,
    add_colon: bool = True,
) -> str:
    stripped = raw_desc.strip()
    if not stripped:
        return ""

    minyan_label = (
        sephardi_label
        if minyan == "sephardi"
        else (ashkenazi_label if minyan == "ashkenazi" else "")
    )
    expected_suffix = f"({minyan_label})" if (suffix_desc and minyan_label) else ""
    colon_suffix = ":" if add_colon else ""

    if expected_suffix and stripped.endswith(expected_suffix):
        base = stripped[: -len(expected_suffix)].rstrip()
        escaped_lbl = html_lib.escape(minyan_label)
        if base:
            escaped_base = html_lib.escape(base)
            return f'<div class="group-title">{escaped_base} <span class="group-label">({escaped_lbl})</span>{colon_suffix}</div>'
        return f'<div class="group-title"><span class="group-label">({escaped_lbl})</span>{colon_suffix}</div>'

    text_out = (stripped if stripped.endswith(":") else f"{stripped}:") if add_colon else stripped
    return f'<div class="group-title">{html_lib.escape(text_out)}</div>'


def _split_minyan_suffix(
    raw_desc: str,
    minyan: str,
    *,
    sephardi_label: str,
    ashkenazi_label: str,
    suffix_desc: bool,
) -> tuple[str, str]:
    """Returns ``(desc_without_minyan_suffix, minyan_label)``; the label is ``""`` when ``raw_desc`` has no suffix."""
    minyan_label = (
        sephardi_label
        if minyan == "sephardi"
        else (ashkenazi_label if minyan == "ashkenazi" else "")
    )
    label_suffix = f"({minyan_label})" if (suffix_desc and minyan_label) else ""
    stripped = raw_desc.strip()
    if label_suffix and stripped.endswith(label_suffix):
        return stripped[: -len(label_suffix)].rstrip(), minyan_label
    return raw_desc, ""


def _render_single_schedule_tr(
    row: dict,
    *,
    allow_sub_row: bool = True,
    stack_time_on_newline: bool = False,
    sephardi_label: str = DEFAULT_SEPHARDI_LABEL,
    ashkenazi_label: str = DEFAULT_ASHKENAZI_LABEL,
    suffix_desc: bool = True,
    label_style: str = "inline",
) -> str:
    raw_desc = str(row.get("desc") or "")
    desc = html_lib.escape(raw_desc)
    time_str = html_lib.escape(str(row.get("time") or ""))
    minyan = str(row.get("minyan") or "")
    minyan_cls = f" minyan-{html_lib.escape(minyan)}" if minyan else ""
    if time_str:
        if stack_time_on_newline:
            below_time = label_style != "inline"
            title_text, tile_label = (
                _split_minyan_suffix(
                    raw_desc,
                    minyan,
                    sephardi_label=sephardi_label,
                    ashkenazi_label=ashkenazi_label,
                    suffix_desc=suffix_desc,
                )
                if below_time
                else (raw_desc, "")
            )
            title_div = _format_stacked_row_title_html(
                title_text,
                "" if below_time else minyan,
                sephardi_label=sephardi_label,
                ashkenazi_label=ashkenazi_label,
                suffix_desc=suffix_desc,
                add_colon=True,
            )
            label_html = f'<span class="group-label">{html_lib.escape(tile_label)}</span>' if tile_label else ""
            return (
                f'<tr class="row group-row{minyan_cls}">'
                f'<td class="group-cell" colspan="2">{title_div}'
                f'<div class="group-times"><span class="group-item">'
                f'<span class="group-time" dir="ltr">{time_str}</span>{label_html}'
                f"</span></div></td>"
                f"</tr>"
            )
        return (
            f'<tr class="row{minyan_cls}">'
            f'<td class="desc">{desc}</td>'
            f'<td class="time" dir="ltr">{time_str}</td>'
            f"</tr>"
        )
    if stack_time_on_newline:
        below_all = label_style == "below_all"
        title_text, label = (
            _split_minyan_suffix(
                raw_desc,
                minyan,
                sephardi_label=sephardi_label,
                ashkenazi_label=ashkenazi_label,
                suffix_desc=suffix_desc,
            )
            if below_all
            else (raw_desc, "")
        )
        title_div = _format_stacked_row_title_html(
            title_text,
            "" if below_all else minyan,
            sephardi_label=sephardi_label,
            ashkenazi_label=ashkenazi_label,
            suffix_desc=suffix_desc,
            add_colon=False,
        )
        label_html = (
            f'<div class="group-label-line"><span class="group-label">{html_lib.escape(label)}</span></div>'
            if label
            else ""
        )
        return (
            f'<tr class="row group-row{minyan_cls}">'
            f'<td class="group-cell" colspan="2">{title_div}{label_html}</td></tr>'
        )
    sub_cls = " sub-row" if allow_sub_row else ""
    return f'<tr class="row{sub_cls}{minyan_cls}"><td class="desc" colspan="2">{desc}</td></tr>'


def _render_group_tr(
    title: str,
    matched_rows: list[dict],
    *,
    sephardi_label: str,
    ashkenazi_label: str,
    suffix_desc: bool,
    no_suffix_pairs: list[tuple[str, re.Pattern[str]]],
    group_no_suffix_label: str | None,
    tag_renderer: Callable[[str], str] | None,
) -> str:
    raw_title = tag_renderer(title) if (tag_renderer and title) else title
    escaped_title = html_lib.escape(raw_title)

    time_order: list[str] = []
    labels_by_time: dict[str, list[str]] = {}
    for row in matched_rows:
        t = str(row.get("time") or "").strip()
        if not t:
            continue
        if t not in labels_by_time:
            time_order.append(t)
            labels_by_time[t] = []
        lbl = _resolve_grouped_row_label(
            row,
            sephardi_label=sephardi_label,
            ashkenazi_label=ashkenazi_label,
            suffix_desc=suffix_desc,
            no_suffix_pairs=no_suffix_pairs,
            group_no_suffix_label=group_no_suffix_label,
        )
        if lbl and lbl not in labels_by_time[t]:
            labels_by_time[t].append(lbl)

    items_html: list[str] = []
    for t in time_order:
        escaped_t = html_lib.escape(t)
        combined_lbl = _join_hebrew_labels(labels_by_time[t])
        label_html = (
            f'<span class="group-label">{html_lib.escape(combined_lbl)}</span>'
            if combined_lbl
            else ""
        )
        items_html.append(
            f'<span class="group-item">'
            f'<span class="group-time" dir="ltr">{escaped_t}</span>{label_html}'
            f"</span>"
        )

    title_div = f'<div class="group-title">{escaped_title}</div>' if escaped_title else ""
    items_joined = " ".join(items_html)
    return (
        f'<tr class="row group-row">'
        f'<td class="group-cell" colspan="2">{title_div}<div class="group-times">{items_joined}</div></td>'
        f"</tr>"
    )


def _render_html_rows(
    rows: list[dict],
    *,
    groups: Sequence[ScheduleGroupConfig | Sequence[str] | dict] | None = None,
    exclude_patterns: Sequence[str] | None = None,
    group_unmatched_order: str = "chronological",
    group_no_suffix_label: str | None = "pattern",
    no_suffix_patterns: Sequence[str] | None = None,
    sephardi_label: str = DEFAULT_SEPHARDI_LABEL,
    ashkenazi_label: str = DEFAULT_ASHKENAZI_LABEL,
    suffix_desc: bool = True,
    tag_renderer: Callable[[str], str] | None = None,
    single_row_label_style: str = "inline",
) -> str:
    if not rows:
        return '<tr><td class="empty" colspan="2">אין זמנים להצגה</td></tr>'

    if not groups and not exclude_patterns:
        return "\n".join(_render_single_schedule_tr(row, allow_sub_row=True) for row in rows)

    parsed_groups = (
        _parse_schedule_groups_field(list(groups), "groups", "render_html_report")
        if groups
        else []
    )
    compiled_groups = [(grp.title, re.compile(grp.pattern)) for grp in parsed_groups]
    parsed_excludes = _parse_patterns_field(
        list(exclude_patterns) if exclude_patterns is not None else None,
        "render_html_report",
        "exclude_patterns",
    )
    compiled_excludes = [re.compile(pat) for pat in parsed_excludes]
    order_mode = _parse_group_unmatched_order(group_unmatched_order, "render_html_report")
    parsed_no_suffix = _parse_patterns_field(
        list(no_suffix_patterns) if no_suffix_patterns is not None else None,
        "render_html_report",
    )
    no_suffix_pairs = [(pat, re.compile(pat)) for pat in parsed_no_suffix]

    group_buckets: list[list[dict]] = [[] for _ in compiled_groups]
    # None = unmatched (keep), -1 = excluded (drop), >=0 = matched group index
    row_to_group: list[int | None] = []
    for row in rows:
        time_str = str(row.get("time") or "").strip()
        desc = str(row.get("desc") or "").strip()
        matched_idx: int | None = None
        if time_str:
            for g_idx, (_, g_re) in enumerate(compiled_groups):
                if g_re.search(desc):
                    matched_idx = g_idx
                    group_buckets[g_idx].append(row)
                    break
        if matched_idx is None and any(ex_re.search(desc) for ex_re in compiled_excludes):
            matched_idx = -1
        row_to_group.append(matched_idx)

    plan: list[tuple[str, int]] = []
    if order_mode == "below":
        for g_idx, bucket in enumerate(group_buckets):
            if bucket:
                plan.append(("group", g_idx))
        for r_idx, g_idx in enumerate(row_to_group):
            if g_idx is None:
                plan.append(("row", r_idx))
    else:
        emitted_groups: set[int] = set()
        for r_idx, g_idx in enumerate(row_to_group):
            if g_idx == -1:
                continue
            if g_idx is None:
                plan.append(("row", r_idx))
            elif g_idx not in emitted_groups:
                emitted_groups.add(g_idx)
                plan.append(("group", g_idx))

    out: list[str] = []
    prev_item: tuple[str, int] | None = None
    for kind, idx in plan:
        if kind == "group":
            out.append(
                _render_group_tr(
                    compiled_groups[idx][0],
                    group_buckets[idx],
                    sephardi_label=sephardi_label,
                    ashkenazi_label=ashkenazi_label,
                    suffix_desc=suffix_desc,
                    no_suffix_pairs=no_suffix_pairs,
                    group_no_suffix_label=group_no_suffix_label,
                    tag_renderer=tag_renderer,
                )
            )
        else:
            parent_immediately_above = prev_item == ("row", idx - 1) and idx > 0
            out.append(
                _render_single_schedule_tr(
                    rows[idx],
                    allow_sub_row=parent_immediately_above,
                    stack_time_on_newline=bool(compiled_groups),
                    sephardi_label=sephardi_label,
                    ashkenazi_label=ashkenazi_label,
                    suffix_desc=suffix_desc,
                    label_style=single_row_label_style,
                )
            )
        prev_item = (kind, idx)

    if not out:
        return '<tr><td class="empty" colspan="2">אין זמנים להצגה</td></tr>'
    return "\n".join(out)


def _resolve_config_path(raw_path: str | None, base_dir: Path) -> Path | None:
    if not raw_path or not raw_path.strip():
        return None
    p = Path(raw_path.strip())
    if not p.is_absolute():
        p = base_dir / p
    return p.resolve()


def build_luah_tag_renderer(
    cfg: CombineConfig,
    *,
    now: datetime | None = None,
) -> Callable[[str], str] | None:
    """Attempts to load ``pyzmanim`` + ``luah_signage.tagging`` from the path(s)
    configured on ``cfg`` and returns a ``Callable[[str], str]`` that expands
    supported ``#TAG`` placeholders (such as ``#HEBDATE``, ``#PARSHA``,
    ``#SHABBOS-30``).

    If ``luah_suite_path`` (and explicit library paths) are unconfigured, or if
    the libraries are not found at the configured paths, returns ``None`` so
    callers leave any ``#TAG`` text untouched as-is.
    """
    suite_dir = _resolve_config_path(cfg.luah_suite_path, cfg.base_dir)
    pyzmanim_dir = _resolve_config_path(cfg.pyzmanim_lib_path, cfg.base_dir) or (
        suite_dir / "pyzmanim-lib" if suite_dir is not None else None
    )
    luah_signage_dir = _resolve_config_path(cfg.luah_signage_path, cfg.base_dir) or (
        suite_dir / "luah_signage" if suite_dir is not None else None
    )

    if pyzmanim_dir is None or luah_signage_dir is None:
        return None
    if not (pyzmanim_dir / "pyzmanim").is_dir() or not (luah_signage_dir / "luah_signage").is_dir():
        return None

    for lib_dir in (pyzmanim_dir, luah_signage_dir):
        lib_str = str(lib_dir)
        if lib_str not in sys.path:
            sys.path.insert(0, lib_str)

    try:
        from pyzmanim.noaa_calculator import Location
        from luah_signage import config as luah_cfg_mod
        from luah_signage.tagging import render_template
        from luah_signage.tzdata_support import ensure_tzdata_available
        from luah_signage.zman_context import build_zman_context
    except Exception:
        return None

    ensure_tzdata_available()

    location = Location(latitude=32.06, longitude=35.26, elevation=720.0)
    eretz_yisroel = True
    tz_name = "Asia/Jerusalem"

    if suite_dir is not None and (
        cfg.location is None or cfg.eretz_yisroel is None or cfg.timezone is None
    ):
        for candidate_name in ("config.json", "config.example.json"):
            candidate = suite_dir / candidate_name
            if candidate.is_file():
                try:
                    loaded_luah_cfg = luah_cfg_mod.load_config(candidate)
                    location = loaded_luah_cfg.location
                    eretz_yisroel = loaded_luah_cfg.eretz_yisroel
                    tz_name = loaded_luah_cfg.timezone
                    break
                except Exception:
                    continue

    if cfg.location is not None:
        location = Location(
            latitude=cfg.location.latitude,
            longitude=cfg.location.longitude,
            elevation=cfg.location.elevation,
        )
    if cfg.eretz_yisroel is not None:
        eretz_yisroel = cfg.eretz_yisroel
    if cfg.timezone is not None:
        tz_name = cfg.timezone

    try:
        effective_now = (
            now
            if now is not None
            else datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None)
        )
        ctx = build_zman_context(
            now=effective_now,
            location=location,
            eretz_yisroel=eretz_yisroel,
            timezone=tz_name,
        )
    except Exception:
        return None

    return lambda text: render_template(text, ctx)


def _extra_table_applies_to_mode(table_mode: str, report_mode: str) -> bool:
    if table_mode == "all":
        return True
    if table_mode == "both":
        return report_mode == "both"
    if table_mode == "chol":
        return report_mode in ("both", "chol")
    if table_mode == "shabbos":
        return report_mode in ("both", "shabbos")
    return True


def _render_extra_table_card(
    table: ExtraTableConfig,
    tag_renderer: Callable[[str], str] | None = None,
) -> str | None:
    raw_title = tag_renderer(table.title) if (tag_renderer and table.title) else table.title
    escaped_title = html_lib.escape(raw_title)

    row_trs: list[str] = []
    for raw_time, raw_desc in table.rows:
        rendered_time = (tag_renderer(raw_time) if (tag_renderer and raw_time) else raw_time).strip()
        rendered_desc = (tag_renderer(raw_desc) if (tag_renderer and raw_desc) else raw_desc).strip()
        if not rendered_time and not rendered_desc:
            continue

        escaped_time = html_lib.escape(rendered_time)
        escaped_desc = html_lib.escape(rendered_desc)
        time_dir = "rtl" if any("\u0590" <= ch <= "\u05ff" for ch in rendered_time) else "ltr"

        if rendered_desc and rendered_time:
            row_trs.append(
                f'<tr class="row">'
                f'<td class="desc">{escaped_desc}</td>'
                f'<td class="time" dir="{time_dir}">{escaped_time}</td>'
                f"</tr>"
            )
        elif rendered_desc:
            row_trs.append(
                f'<tr class="row"><td class="desc" colspan="2">{escaped_desc}</td></tr>'
            )
        else:
            row_trs.append(
                f'<tr class="row"><td class="time" colspan="2" dir="{time_dir}">{escaped_time}</td></tr>'
            )

    if table.rows and not row_trs:
        return None
    if not escaped_title and not row_trs:
        return None

    header_html = f'\n        <h2 class="card-header">{escaped_title}</h2>' if escaped_title else ""
    if row_trs:
        rows_joined = "\n".join(row_trs)
        table_html = (
            f"\n        <table class=\"schedule\">\n"
            f"          <tbody>\n"
            f"{rows_joined}\n"
            f"          </tbody>\n"
            f"        </table>"
        )
    else:
        table_html = ""

    return f"      <section class=\"card\">{header_html}{table_html}\n      </section>"


def render_html_report(
    payload: dict,
    mode: str = "both",
    *,
    report_title: str | None = None,
    report_subtitle: str | None = None,
    report_footer: str | None = None,
    extra_tables: Sequence[ExtraTableConfig | dict] | None = None,
    chol_groups: Sequence[ScheduleGroupConfig | Sequence[str] | dict] | None = None,
    shabbos_groups: Sequence[ScheduleGroupConfig | Sequence[str] | dict] | None = None,
    chol_groups_exclude: Sequence[str] | None = None,
    shabbos_groups_exclude: Sequence[str] | None = None,
    group_unmatched_order: str = "chronological",
    group_no_suffix_label: str | None = "pattern",
    no_suffix_patterns: Sequence[str] | None = None,
    sephardi_label: str = DEFAULT_SEPHARDI_LABEL,
    ashkenazi_label: str = DEFAULT_ASHKENAZI_LABEL,
    suffix_desc: bool = True,
    tag_renderer: Callable[[str], str] | None = None,
    timezone: str = "Asia/Jerusalem",
    gray_past_after_minutes: float = 5,
    single_row_label_style: str = "inline",
) -> str:
    """Renders a self-contained, responsive RTL Hebrew HTML report.

    ``mode`` may be ``"both"`` (default, 2-column weekday + Shabbos layout),
    ``"chol"`` (single-column weekday layout), or ``"shabbos"`` (single-column
    Shabbos/Yom-Tov layout).

    ``report_title``, ``report_subtitle``, and ``report_footer`` override the
    default header/footer lines when non-``None``; passing ``""`` explicitly
    hides that line. ``extra_tables`` optionally appends custom cards whose
    ``title`` and ``[time, desc]`` rows also expand ``#TAG`` placeholders when
    ``tag_renderer`` is provided. ``chol_groups`` / ``shabbos_groups`` optionally
    collapse matching schedule rows into compact comma-separated time lists.
    ``report_footer`` may also contain ``"{updated_at}"``.
    """
    if mode not in ("both", "chol", "shabbos"):
        raise ValueError(f"Unsupported HTML report mode: {mode!r}")
    if single_row_label_style not in VALID_SINGLE_ROW_LABEL_STYLES:
        raise ValueError(f"Unsupported single_row_label_style: {single_row_label_style!r}")

    if report_title is not None:
        raw_title = tag_renderer(report_title) if (tag_renderer and report_title) else report_title
    else:
        raw_title = payload.get("shul_name") or "זמני תפילות"

    if report_subtitle is not None:
        raw_subtitle = (
            tag_renderer(report_subtitle)
            if (tag_renderer and report_subtitle)
            else report_subtitle
        )
    else:
        raw_subtitle = payload.get("page_date") or ""

    shul_name = html_lib.escape(raw_title)
    page_date = html_lib.escape(raw_subtitle)
    updated_at = html_lib.escape(_format_updated_at(str(payload.get("fetched_at") or "")))

    has_chol_toggle = mode in ("both", "chol") and bool(chol_groups or chol_groups_exclude)
    has_shabbos_toggle = mode in ("both", "shabbos") and bool(shabbos_groups or shabbos_groups_exclude)
    has_view_toggle = has_chol_toggle or has_shabbos_toggle
    card_toggle_html = (
        '<div class="view-toggle" role="group" aria-label="תצוגת לוח">'
        '<button type="button" class="toggle-btn active" data-view="compact" aria-pressed="true">מקוצר</button>'
        '<button type="button" class="toggle-btn" data-view="full" aria-pressed="false">מפורט</button>'
        "</div>"
    )

    sections_html: list[str] = []
    if mode in ("both", "chol"):
        weekday_rows_html = _render_html_rows(
            payload.get("weekday", []),
            groups=chol_groups,
            exclude_patterns=chol_groups_exclude,
            group_unmatched_order=group_unmatched_order,
            group_no_suffix_label=group_no_suffix_label,
            no_suffix_patterns=no_suffix_patterns,
            sephardi_label=sephardi_label,
            ashkenazi_label=ashkenazi_label,
            suffix_desc=suffix_desc,
            tag_renderer=tag_renderer,
            single_row_label_style=single_row_label_style,
        )
        if has_chol_toggle:
            weekday_full_rows_html = _render_html_rows(
                payload.get("weekday", []),
                groups=None,
                exclude_patterns=None,
                sephardi_label=sephardi_label,
                ashkenazi_label=ashkenazi_label,
                suffix_desc=suffix_desc,
                tag_renderer=tag_renderer,
            )
            weekday_header_html = (
                f'<h2 class="card-header has-toggle">'
                f'<span class="card-title">זמני תפילות לימי חול</span>{card_toggle_html}</h2>'
            )
            weekday_table_body = f"""          <tbody class="view-compact" data-gray-past>
{weekday_rows_html}
          </tbody>
          <tbody class="view-full" hidden>
{weekday_full_rows_html}
          </tbody>"""
        else:
            weekday_header_html = '<h2 class="card-header">זמני תפילות לימי חול</h2>'
            weekday_table_body = f"""          <tbody>
{weekday_rows_html}
          </tbody>"""
        sections_html.append(
            f"""      <section class="card">
        {weekday_header_html}
        <table class="schedule">
{weekday_table_body}
        </table>
      </section>"""
        )
    if mode in ("both", "shabbos"):
        shabbos_rows_html = _render_html_rows(
            payload.get("shabbos", []),
            groups=shabbos_groups,
            exclude_patterns=shabbos_groups_exclude,
            group_unmatched_order=group_unmatched_order,
            group_no_suffix_label=group_no_suffix_label,
            no_suffix_patterns=no_suffix_patterns,
            sephardi_label=sephardi_label,
            ashkenazi_label=ashkenazi_label,
            suffix_desc=suffix_desc,
            tag_renderer=tag_renderer,
            single_row_label_style=single_row_label_style,
        )
        if has_shabbos_toggle:
            shabbos_full_rows_html = _render_html_rows(
                payload.get("shabbos", []),
                groups=None,
                exclude_patterns=None,
                sephardi_label=sephardi_label,
                ashkenazi_label=ashkenazi_label,
                suffix_desc=suffix_desc,
                tag_renderer=tag_renderer,
            )
            shabbos_header_html = (
                f'<h2 class="card-header has-toggle">'
                f'<span class="card-title">זמני תפילות לשבת ומועד</span>{card_toggle_html}</h2>'
            )
            shabbos_table_body = f"""          <tbody class="view-compact">
{shabbos_rows_html}
          </tbody>
          <tbody class="view-full" hidden>
{shabbos_full_rows_html}
          </tbody>"""
        else:
            shabbos_header_html = '<h2 class="card-header">זמני תפילות לשבת ומועד</h2>'
            shabbos_table_body = f"""          <tbody>
{shabbos_rows_html}
          </tbody>"""
        sections_html.append(
            f"""      <section class="card">
        {shabbos_header_html}
        <table class="schedule">
{shabbos_table_body}
        </table>
      </section>"""
        )

    if extra_tables:
        normalized_tables: list[ExtraTableConfig] = []
        for idx, item in enumerate(extra_tables):
            if isinstance(item, ExtraTableConfig):
                normalized_tables.append(item)
            else:
                normalized_tables.extend(_parse_extra_tables_field([item], f"extra_tables[{idx}]"))

        for tbl in normalized_tables:
            if not _extra_table_applies_to_mode(tbl.mode, mode):
                continue
            card_html = _render_extra_table_card(tbl, tag_renderer=tag_renderer)
            if card_html is not None:
                sections_html.append(card_html)

    title_suffix = {
        "both": "זמני תפילות",
        "chol": "זמני תפילות לימי חול",
        "shabbos": "זמני תפילות לשבת ומועד",
    }[mode]
    title_for_head = " ".join(shul_name.splitlines())
    page_title = (
        title_for_head
        if (title_for_head and mode == "both")
        else (f"{title_for_head} — {title_suffix}" if title_for_head else title_suffix)
    )
    single_mode = mode != "both"
    container_cls = "container single" if single_mode else "container"
    grid_cls = "grid single" if single_mode else "grid"

    h1_html = f"<h1>{shul_name}</h1>" if shul_name else ""
    date_html = f'<div class="subtitle">{page_date}</div>' if page_date else ""
    if h1_html or date_html:
        banner_lines = "\n      ".join(part for part in (h1_html, date_html) if part)
        banner_html = f"""    <header class="banner">
      {banner_lines}
    </header>"""
    else:
        banner_html = ""

    script_html = (
        """\n  <script>
    (function () {
      var toggles = document.querySelectorAll(".view-toggle");
      for (var t = 0; t < toggles.length; t++) {
        (function (toggle) {
          var card = toggle.closest(".card");
          if (!card) return;
          var buttons = toggle.querySelectorAll(".toggle-btn");
          var compactBody = card.querySelector("tbody.view-compact");
          var fullBody = card.querySelector("tbody.view-full");
          function setView(view) {
            var isFull = view === "full";
            card.classList.toggle("show-full", isFull);
            if (compactBody) compactBody.hidden = isFull;
            if (fullBody) fullBody.hidden = !isFull;
            for (var k = 0; k < buttons.length; k++) {
              var active = buttons[k].getAttribute("data-view") === view;
              buttons[k].classList.toggle("active", active);
              buttons[k].setAttribute("aria-pressed", active ? "true" : "false");
            }
          }
          for (var i = 0; i < buttons.length; i++) {
            buttons[i].addEventListener("click", function () {
              setView(this.getAttribute("data-view"));
            });
          }
        })(toggles[t]);
      }
    })();
  </script>"""
        if has_view_toggle
        else ""
    )
    if has_chol_toggle:
        # Client-side: dims compact-weekday entries once `gray_past_after_minutes` have passed in `timezone`.
        script_html += """\n  <script>
    (function () {
      var body = document.querySelector("tbody[data-gray-past]");
      if (!body) return;
      var fmt;
      try {
        fmt = new Intl.DateTimeFormat("en-GB", {timeZone: __TZ__, hour: "2-digit", minute: "2-digit", hourCycle: "h23"});
      } catch (e) { return; }
      function update() {
        var parts = fmt.formatToParts(new Date()), h = 0, m = 0;
        for (var i = 0; i < parts.length; i++) {
          if (parts[i].type === "hour") h = parseInt(parts[i].value, 10);
          if (parts[i].type === "minute") m = parseInt(parts[i].value, 10);
        }
        var now = h * 60 + m;
        var items = body.querySelectorAll(".group-item");
        for (var k = 0; k < items.length; k++) {
          var t = items[k].querySelector(".group-time");
          var mm = t && /^(\\d{1,2}):(\\d{2})$/.exec(t.textContent.trim());
          items[k].classList.toggle("past", !!mm && parseInt(mm[1], 10) * 60 + parseInt(mm[2], 10) + __GRACE__ <= now);
        }
      }
      update();
      setInterval(update, 60000);
    })();
  </script>""".replace("__TZ__", json.dumps(timezone)).replace("__GRACE__", json.dumps(gray_past_after_minutes))

    if report_footer is None:
        footer_html = (
            f'<footer class="footer">עודכן לאחרונה: <span dir="ltr">{updated_at}</span></footer>'
            if updated_at
            else ""
        )
    elif report_footer == "":
        footer_html = ""
    else:
        rendered_footer = tag_renderer(report_footer) if tag_renderer else report_footer
        escaped_footer = html_lib.escape(rendered_footer)
        if "{updated_at}" in escaped_footer:
            replacement = f'<span dir="ltr">{updated_at}</span>' if updated_at else ""
            escaped_footer = escaped_footer.replace("{updated_at}", replacement)
        footer_html = f'<footer class="footer">{escaped_footer}</footer>'
    cards_joined = "\n".join(sections_html)

    return f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{page_title}</title>
  <style>
    :root {{
      --bg-top: #fdfcf6;
      --bg-bottom: #ece0a6;
      --title: #2f5f85;
      --subtitle: #1c1a14;
      --heading: #1b3a5c;
      --text: #3d3b33;
      --muted: #7d7a6c;
      --border: #d9d5c4;
      --time-text: #1c1a14;
      --time-past: #8f8a73;
    }}
    * {{ box-sizing: border-box; }}
    html {{ background: var(--bg-bottom); }}
    body {{
      margin: 0;
      padding: 24px 16px 36px;
      font-family: "Segoe UI", Tahoma, Arial, sans-serif;
      color: var(--text);
      line-height: 1.45;
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      z-index: -1;
      background: linear-gradient(180deg, var(--bg-top) 0%, var(--bg-bottom) 100%);
    }}
    .container {{
      max-width: 980px;
      margin: 0 auto;
    }}
    .container.single {{
      max-width: 560px;
    }}
    .banner {{
      text-align: center;
      padding: 8px 12px 20px;
      margin-bottom: 8px;
    }}
    .banner h1 {{
      margin: 0;
      font-size: 2rem;
      font-weight: 600;
      color: var(--title);
      white-space: pre-line;
    }}
    .subtitle {{
      margin-top: 4px;
      font-size: 1.5rem;
      font-weight: 300;
      color: var(--subtitle);
      white-space: pre-line;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 20px;
      align-items: start;
    }}
    .grid.single {{
      grid-template-columns: 1fr;
    }}
    @media (max-width: 720px) {{
      .grid {{ grid-template-columns: 1fr; }}
    }}
    .card {{
      border-top: 1px solid var(--border);
      padding-top: 6px;
    }}
    .card-header {{
      margin: 0;
      padding: 14px 18px 6px;
      font-size: 1.5rem;
      font-weight: 700;
      color: var(--heading);
      text-align: center;
      white-space: pre-line;
    }}
    .card-header.has-toggle {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 11px 14px;
    }}
    .card-header .card-title {{
      flex: 1;
      text-align: center;
    }}
    .view-toggle {{
      display: inline-flex;
      flex-shrink: 0;
      padding: 2px;
      background: rgba(255, 255, 255, 0.55);
      border: 1px solid var(--border);
      border-radius: 999px;
      gap: 2px;
    }}
    .toggle-btn {{
      appearance: none;
      border: none;
      background: transparent;
      color: var(--muted);
      font-family: inherit;
      font-size: 0.76rem;
      font-weight: 600;
      line-height: 1.2;
      padding: 3px 9px;
      border-radius: 999px;
      cursor: pointer;
      transition: background 0.15s ease, color 0.15s ease;
    }}
    .toggle-btn:hover {{
      color: var(--heading);
    }}
    .toggle-btn.active {{
      background: var(--heading);
      color: #ffffff;
      font-weight: 700;
    }}
    .card.show-full tbody.view-compact {{
      display: none;
    }}
    .card:not(.show-full) tbody.view-full {{
      display: none;
    }}
    table.schedule {{
      width: 100%;
      border-collapse: collapse;
    }}
    tr.row td {{
      padding: 10px 18px;
      border-top: 1px solid var(--border);
      font-size: 1.05rem;
    }}
    tr.row:first-child td {{
      border-top: none;
    }}
    tr.row:hover {{
      background: rgba(255, 255, 255, 0.35);
    }}
    td.desc {{
      font-weight: 500;
      white-space: pre-line;
    }}
    td.time {{
      width: 1%;
      white-space: nowrap;
      text-align: left;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
      color: var(--time-text);
    }}
    td.time[colspan="2"] {{
      width: auto;
      white-space: pre-line;
      text-align: right;
    }}
    tr.sub-row td {{
      border-top: none;
      padding-top: 0;
      padding-bottom: 10px;
      padding-right: 30px;
      color: var(--muted);
      font-size: 0.96rem;
    }}
    td.group-cell {{
      text-align: center;
    }}
    .group-title {{
      font-weight: 700;
      color: var(--heading);
      margin-bottom: 4px;
      white-space: pre-line;
    }}
    .group-title:last-child {{
      margin-bottom: 0;
    }}
    td.group-cell > .group-title:only-child {{
      padding-bottom: 14px;
    }}
    td.group-cell > .group-label-line {{
      padding-bottom: 8px;
    }}
    .group-times {{
      display: flex;
      flex-wrap: wrap;
      justify-content: center;
      line-height: 1.2;
    }}
    .group-item {{
      display: inline-flex;
      flex-direction: column;
      align-items: center;
      min-width: 5.5rem;
      padding: 6px 14px;
      margin: 2px 0;
      white-space: nowrap;
      border-inline-start: 1px solid var(--border);
    }}
    .group-time {{
      font-family: inherit;
      font-size: 1.25rem;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
      color: var(--time-text);
    }}
    .group-label {{
      font-weight: 400;
      color: var(--muted);
      font-size: 0.9rem;
    }}
    .group-item.past .group-time,
    .group-item.past .group-label {{
      color: var(--time-past);
    }}
    td.empty {{
      padding: 20px;
      text-align: center;
      color: var(--muted);
    }}
    .footer {{
      margin-top: 20px;
      padding-top: 14px;
      border-top: 1px solid var(--border);
      text-align: center;
      font-size: 0.95rem;
      color: var(--title);
      white-space: pre-line;
    }}
    @media print {{
      body {{ background: #ffffff; padding: 0; }}
      html {{ background: #ffffff; }}
      body::before {{ display: none; }}
      .banner, .card {{ box-shadow: none; }}
      .view-toggle {{ display: none; }}
      .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
      .grid.single {{ grid-template-columns: 1fr; }}
      tr.row td {{ padding: 6px 12px; font-size: 0.95rem; }}
    }}
  </style>
</head>
<body>
  <div class="{container_cls}">
{banner_html}
    <main class="{grid_cls}">
{cards_joined}
    </main>
    {footer_html}
  </div>{script_html}
</body>
</html>
"""


_FOOTER_BLOCK_RE = re.compile(r'\s*<footer class="footer">.*?</footer>', re.DOTALL)


def strip_html_footer(html_doc: str) -> str:
    """Returns ``html_doc`` with ``<footer class="footer">...</footer>`` stripped
    and line endings normalized, allowing content comparison that ignores
    footer-only timestamp changes."""
    normalized = html_doc.replace("\r\n", "\n")
    return _FOOTER_BLOCK_RE.sub("", normalized).strip()


def html_has_meaningful_changes(existing_html: str, new_html: str) -> bool:
    """Returns ``True`` when ``new_html`` differs from ``existing_html`` outside
    the ``<footer class="footer">...</footer>`` element."""
    return strip_html_footer(existing_html) != strip_html_footer(new_html)


def _write_json(path: Path, payload: dict, indent: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=indent)
    path.write_text(text + "\n", encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_html_output(
    path: Path,
    content: str,
    *,
    skip_if_unchanged: bool = False,
) -> bool:
    """Writes ``content`` to ``path``, unless ``skip_if_unchanged`` is ``True``,
    ``path`` already exists, and the only difference is inside ``<footer>``.
    Returns ``True`` if ``path`` was written, ``False`` if skipped."""
    if skip_if_unchanged and path.is_file():
        existing = path.read_text(encoding="utf-8")
        if not html_has_meaningful_changes(existing, content):
            return False
    _write_text(path, content)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            f"Path to persistent JSON config file (auto-loads {DEFAULT_CONFIG_PATH.name} "
            "next to this script when present)."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_COMBINED_OUT,
        help=f"Path for the combined (weekday + shabbos) JSON file. Default: {DEFAULT_COMBINED_OUT.name}.",
    )
    parser.add_argument(
        "--chol-out",
        type=Path,
        default=DEFAULT_CHOL_OUT,
        help=f"Path for the unified weekday (chol) JSON file. Default: {DEFAULT_CHOL_OUT.name}.",
    )
    parser.add_argument(
        "--shabbos-out",
        type=Path,
        default=DEFAULT_SHABBOS_OUT,
        help=f"Path for the unified Shabbos/Yom-Tov JSON file. Default: {DEFAULT_SHABBOS_OUT.name}.",
    )
    parser.add_argument(
        "--html-out",
        type=Path,
        default=DEFAULT_HTML_OUT,
        help=f"Path for the combined HTML report. Default: {DEFAULT_HTML_OUT.name}.",
    )
    parser.add_argument(
        "--chol-html-out",
        type=Path,
        default=DEFAULT_CHOL_HTML_OUT,
        help=f"Path for the weekday-only HTML report. Default: {DEFAULT_CHOL_HTML_OUT.name}.",
    )
    parser.add_argument(
        "--shabbos-html-out",
        type=Path,
        default=DEFAULT_SHABBOS_HTML_OUT,
        help=f"Path for the Shabbos/Yom-Tov-only HTML report. Default: {DEFAULT_SHABBOS_HTML_OUT.name}.",
    )
    parser.add_argument(
        "--no-html",
        action="store_true",
        help="Skip generating the HTML report files.",
    )
    parser.add_argument(
        "--only-combined-html",
        action="store_true",
        help=(
            "Write only the combined HTML report (--html-out), skipping all JSON files "
            "and split chol/shabbos HTML files."
        ),
    )
    parser.add_argument(
        "--skip-if-unchanged",
        action="store_true",
        help=(
            "Do not overwrite an existing HTML output file if the only difference "
            "from the newly rendered HTML is the footer timestamp."
        ),
    )
    parser.add_argument(
        "--title",
        dest="report_title",
        default=None,
        help="Override the HTML report title (defaults to config.json's report_title or fetched shul_name).",
    )
    parser.add_argument(
        "--subtitle",
        dest="report_subtitle",
        default=None,
        help="Override the HTML report subtitle (defaults to config.json's report_subtitle or fetched page_date).",
    )
    parser.add_argument(
        "--footer",
        dest="report_footer",
        default=None,
        help="Override the HTML report footer (defaults to config.json's report_footer or last-updated timestamp).",
    )
    parser.add_argument(
        "--luah-suite-path",
        default=None,
        help="Path to the shul-luah root folder (e.g. '..') for rendering #TAGs in report_* fields.",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Also print the combined JSON payload to stdout.",
    )
    parser.add_argument(
        "--order",
        choices=["chronological", "time", "concat"],
        default=None,
        help=(
            "Row ordering strategy: 'chronological' (default, keeps Erev Shabbos before Shabbos "
            "morning and keeps timeless rows attached to their preceding row), 'time' (simple "
            "HH:MM ascending), or 'concat' (Sephardi rows followed by Ashkenazi rows)."
        ),
    )
    parser.add_argument(
        "--sephardi-label",
        default=None,
        help=f'Hebrew suffix label for Sephardi rows. Default: "{DEFAULT_SEPHARDI_LABEL}".',
    )
    parser.add_argument(
        "--ashkenazi-label",
        default=None,
        help=f'Hebrew suffix label for Ashkenazi rows. Default: "{DEFAULT_ASHKENAZI_LABEL}".',
    )
    parser.add_argument(
        "--no-desc-suffix",
        action="store_true",
        help='Do not append "(ספרדי)" / "(אשכנזי)" to row descriptions (keeps only the "minyan" JSON field).',
    )
    parser.add_argument(
        "--pattern",
        "--no-suffix-pattern",
        dest="patterns",
        action="append",
        default=None,
        metavar="REGEX",
        help=(
            'Optional regex pattern: rows whose original "desc" matches will NOT be suffixed '
            'with "(ספרדי)" / "(אשכנזי)" (e.g. --pattern=".*המדרש.*"). Overrides config.json '
            "no_suffix_patterns when specified; can be passed multiple times."
        ),
    )
    parser.add_argument(
        "--sephardi-url",
        default=None,
        help="Optional override URL for the Sephardi board.",
    )
    parser.add_argument(
        "--ashkenazi-url",
        default=None,
        help="Optional override URL for the Ashkenazi board.",
    )
    parser.add_argument(
        "--fetch-script",
        type=Path,
        default=DEFAULT_FETCH_SCRIPT,
        help="Path to fetch_prayer_times.py.",
    )
    parser.add_argument(
        "--fetch-timeout",
        type=float,
        default=DEFAULT_FETCH_TIMEOUT_SECONDS,
        help=f"Timeout in seconds per board fetch. Default: {DEFAULT_FETCH_TIMEOUT_SECONDS:.0f}.",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indent (0 for compact). Default: 2.",
    )
    args = parser.parse_args()
    if args.no_html and args.only_combined_html:
        parser.error("--no-html and --only-combined-html cannot be used together")

    try:
        cfg = load_config(args.config)
        cfg = apply_cli_overrides(
            cfg,
            patterns=args.patterns,
            report_title=args.report_title,
            report_subtitle=args.report_subtitle,
            report_footer=args.report_footer,
            luah_suite_path=args.luah_suite_path,
            sephardi_label=args.sephardi_label,
            ashkenazi_label=args.ashkenazi_label,
            no_desc_suffix=args.no_desc_suffix,
            order=args.order,
            no_html=args.no_html,
        )
        if args.only_combined_html:
            cfg.generate_html = True
        compiled_patterns = _compile_patterns(cfg.no_suffix_patterns)
    except Exception as exc:
        parser.error(str(exc))

    try:
        sephardi_data, ashkenazi_data = fetch_both_boards(
            fetch_script=args.fetch_script,
            sephardi_url=args.sephardi_url,
            ashkenazi_url=args.ashkenazi_url,
            timeout=args.fetch_timeout,
        )
    except Exception as exc:
        print(f"Error querying boards: {redact_url_secrets(str(exc))}", file=sys.stderr)
        sys.exit(1)

    combined_payload, chol_payload, shabbos_payload = combine_boards(
        sephardi_data,
        ashkenazi_data,
        sephardi_label=cfg.sephardi_label,
        ashkenazi_label=cfg.ashkenazi_label,
        suffix_desc=cfg.suffix_desc,
        no_suffix_patterns=compiled_patterns,
        order=cfg.order,
    )

    indent = args.indent or None
    written: list[str] = []
    skipped: list[str] = []

    if not args.only_combined_html:
        _write_json(args.out, combined_payload, indent)
        _write_json(args.chol_out, chol_payload, indent)
        _write_json(args.shabbos_out, shabbos_payload, indent)
        written.extend([str(args.out), str(args.chol_out), str(args.shabbos_out)])

    if cfg.generate_html:
        tag_renderer = build_luah_tag_renderer(cfg)
        html_common_kwargs = dict(
            report_title=cfg.report_title,
            report_subtitle=cfg.report_subtitle,
            report_footer=cfg.report_footer,
            extra_tables=cfg.extra_tables,
            chol_groups=cfg.chol_groups,
            shabbos_groups=cfg.shabbos_groups,
            chol_groups_exclude=cfg.chol_groups_exclude,
            shabbos_groups_exclude=cfg.shabbos_groups_exclude,
            group_unmatched_order=cfg.group_unmatched_order,
            group_no_suffix_label=cfg.group_no_suffix_label,
            no_suffix_patterns=cfg.no_suffix_patterns,
            sephardi_label=cfg.sephardi_label,
            ashkenazi_label=cfg.ashkenazi_label,
            suffix_desc=cfg.suffix_desc,
            tag_renderer=tag_renderer,
            timezone=cfg.timezone or "Asia/Jerusalem",
            gray_past_after_minutes=cfg.gray_past_after_minutes,
            single_row_label_style=cfg.single_row_label_style,
        )
        html_targets: list[tuple[Path, str, dict]] = [
            (args.html_out, "both", combined_payload),
        ]
        if not args.only_combined_html:
            html_targets.extend(
                [
                    (args.chol_html_out, "chol", chol_payload),
                    (args.shabbos_html_out, "shabbos", shabbos_payload),
                ]
            )
        for out_path, html_mode, payload in html_targets:
            rendered_html = render_html_report(payload, mode=html_mode, **html_common_kwargs)
            if write_html_output(
                out_path,
                rendered_html,
                skip_if_unchanged=args.skip_if_unchanged,
            ):
                written.append(str(out_path))
            else:
                skipped.append(str(out_path))

    if written:
        print(f"Wrote {', '.join(written)}", file=sys.stderr)
    if skipped:
        print(f"Skipped {', '.join(skipped)} (unchanged except footer)", file=sys.stderr)

    if args.stdout:
        print(json.dumps(combined_payload, ensure_ascii=False, indent=indent))


if __name__ == "__main__":
    main()

