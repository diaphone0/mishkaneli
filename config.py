"""
Persistent JSON configuration for ``mg_sync`` unified schedule & HTML report generation.

Mirrors the ``config.json`` / ``config.example.json`` pattern used by ``luah_signage``:
- ``mg_sync/config.json`` is loaded automatically when present (or falls back to defaults
  when absent).
- Keys starting with ``"_"`` (such as ``"_comment"``) are ignored so self-documenting
  comments can live directly in the JSON files.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = HERE / "config.json"

DEFAULT_SEPHARDI_LABEL = "ספרדי"
DEFAULT_ASHKENAZI_LABEL = "אשכנזי"
VALID_ORDERS = ("chronological", "time", "concat")
VALID_EXTRA_TABLE_MODES = ("all", "both", "chol", "shabbos")
VALID_GROUP_UNMATCHED_ORDERS = ("chronological", "below")


@dataclass
class LocationConfig:
    latitude: float
    longitude: float
    elevation: float = 0.0


@dataclass
class ExtraTableConfig:
    title: str
    rows: list[tuple[str, str]] = field(default_factory=list)
    mode: str = "all"


@dataclass
class ScheduleGroupConfig:
    title: str
    pattern: str


@dataclass
class CombineConfig:
    # Regex patterns: any row whose original "desc" matches at least one pattern
    # will NOT have "(ספרדי)" / "(אשכנזי)" appended.
    no_suffix_patterns: list[str] = field(default_factory=list)

    # HTML report header/footer overrides:
    # - None (JSON null): use the default value (shul_name / page_date / updated_at).
    # - "" (empty string): hide that line completely.
    # - non-empty string: display this custom text instead (report_footer also
    #   supports an optional "{updated_at}" placeholder).
    report_title: str | None = None
    report_subtitle: str | None = None
    report_footer: str | None = None

    # Optional path(s) to the parent shul-luah suite so report_title /
    # report_subtitle / report_footer can render #TAGs (e.g. "#HEBDATE",
    # "#PARSHA", "#SHABBOS-30") via pyzmanim + luah_signage.tagging:
    # - luah_suite_path (e.g. ".."): root folder containing both "pyzmanim-lib/"
    #   and "luah_signage/" (plus optional "config.json" for location/timezone).
    # - pyzmanim_lib_path / luah_signage_path: optional explicit overrides if
    #   either library lives outside luah_suite_path.
    # If unconfigured or not found on disk, #TAGs are left untouched as-is.
    luah_suite_path: str | None = None
    pyzmanim_lib_path: str | None = None
    luah_signage_path: str | None = None

    # Optional location / halachic settings for #TAG calculations. When left None,
    # falls back to <luah_suite_path>/config.json (if present) or Israel defaults.
    location: LocationConfig | None = None
    eretz_yisroel: bool | None = None
    timezone: str | None = None

    # Optional extra tables rendered as additional cards in the HTML reports.
    # Each table has a tag-enabled title and rows of [time, desc] tag-enabled strings.
    extra_tables: list[ExtraTableConfig] = field(default_factory=list)

    # Optional HTML time-grouping rules for weekday ("chol") and Shabbos schedules.
    # Each entry is [group_title, match_pattern] (e.g. ["שחרית:", ".*שחרית.*"]).
    chol_groups: list[ScheduleGroupConfig] = field(default_factory=list)
    shabbos_groups: list[ScheduleGroupConfig] = field(default_factory=list)
    chol_groups_exclude: list[str] = field(default_factory=list)
    shabbos_groups_exclude: list[str] = field(default_factory=list)
    # Where unmatched rows appear relative to matched groups:
    # - "chronological": keep unmatched rows in place (groups anchored at their first match)
    # - "below": show all matched groups at the top of the card, unmatched rows below
    group_unmatched_order: str = "chronological"
    # How rows that matched no_suffix_patterns are labeled inside a time-group:
    # - None / "" / "none": omit any parenthetical label (e.g. "20:15")
    # - "pattern": derive label from the matched no_suffix_patterns regex by trimming
    #   regex syntax (e.g. ".*המדרש.*" -> "20:15 (המדרש)")
    # - "minyan": fall back to the row's minyan label (e.g. "20:15 (אשכנזי)")
    # - any other string: use that fixed label (e.g. "בית המדרש" -> "20:15 (בית המדרש)")
    group_no_suffix_label: str | None = "pattern"

    # Minyan tagging & row merge behavior:
    sephardi_label: str = DEFAULT_SEPHARDI_LABEL
    ashkenazi_label: str = DEFAULT_ASHKENAZI_LABEL
    suffix_desc: bool = True
    order: str = "chronological"
    generate_html: bool = True

    # Directory used to resolve relative paths inside this config (defaults to mg_sync/).
    base_dir: Path = field(default_factory=lambda: HERE, repr=False, compare=False)


def _parse_patterns_field(
    raw: object,
    source: str,
    field_name: str = "no_suffix_patterns",
) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        candidates = [raw]
    elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        candidates = list(raw)
    else:
        raise ValueError(f"{source}: {field_name!r} must be a string or list of strings")

    for pat in candidates:
        try:
            re.compile(pat)
        except re.error as exc:
            raise ValueError(f"{source}: invalid regex {pat!r} in {field_name!r}: {exc}") from exc
    return candidates


def _parse_optional_str(raw: object, field_name: str, source: str) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    raise ValueError(f"{source}: {field_name!r} must be a string or null")


def _parse_location_field(raw: object, source: str) -> LocationConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{source}: 'location' must be an object or null")
    if "latitude" not in raw or "longitude" not in raw:
        raise ValueError(f"{source}: 'location' must include 'latitude' and 'longitude'")
    try:
        lat = float(raw["latitude"])
        lon = float(raw["longitude"])
        elev = float(raw.get("elevation", 0.0))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source}: 'location' latitude/longitude/elevation must be numbers") from exc
    return LocationConfig(latitude=lat, longitude=lon, elevation=elev)


def _parse_extra_tables_field(raw: object, source: str) -> list[ExtraTableConfig]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{source}: 'extra_tables' must be a list of table objects")

    tables: list[ExtraTableConfig] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"{source}: 'extra_tables[{idx}]' must be an object")
        entry = {k: v for k, v in item.items() if not str(k).startswith("_")}

        raw_title = entry.get("title", "")
        if raw_title is None:
            raw_title = ""
        if not isinstance(raw_title, str):
            raise ValueError(f"{source}: 'extra_tables[{idx}].title' must be a string")

        raw_mode = entry.get("mode", "all")
        if raw_mode is None:
            raw_mode = "all"
        if raw_mode not in VALID_EXTRA_TABLE_MODES:
            raise ValueError(
                f"{source}: 'extra_tables[{idx}].mode' must be one of {VALID_EXTRA_TABLE_MODES}, got {raw_mode!r}"
            )

        raw_rows = entry.get("rows", [])
        if raw_rows is None:
            raw_rows = []
        if not isinstance(raw_rows, list):
            raise ValueError(f"{source}: 'extra_tables[{idx}].rows' must be a list")

        parsed_rows: list[tuple[str, str]] = []
        for r_idx, row in enumerate(raw_rows):
            if isinstance(row, (list, tuple)):
                if len(row) == 2 and isinstance(row[0], str) and isinstance(row[1], str):
                    parsed_rows.append((row[0], row[1]))
                elif len(row) == 1 and isinstance(row[0], str):
                    parsed_rows.append(("", row[0]))
                else:
                    raise ValueError(
                        f"{source}: 'extra_tables[{idx}].rows[{r_idx}]' must be a [time, desc] pair of strings"
                    )
            elif isinstance(row, dict):
                t_val = row.get("time", "")
                d_val = row.get("desc", "")
                if not isinstance(t_val, str) or not isinstance(d_val, str):
                    raise ValueError(
                        f"{source}: 'extra_tables[{idx}].rows[{r_idx}]' time/desc must be strings"
                    )
                parsed_rows.append((t_val, d_val))
            else:
                raise ValueError(
                    f"{source}: 'extra_tables[{idx}].rows[{r_idx}]' must be a [time, desc] list"
                )

        tables.append(ExtraTableConfig(title=raw_title, rows=parsed_rows, mode=raw_mode))

    return tables


def _parse_schedule_groups_field(
    raw: object,
    field_name: str,
    source: str,
) -> list[ScheduleGroupConfig]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{source}: {field_name!r} must be a list of [title, pattern] pairs")

    groups: list[ScheduleGroupConfig] = []
    for idx, item in enumerate(raw):
        if isinstance(item, ScheduleGroupConfig):
            title, pattern = item.title, item.pattern
        elif isinstance(item, (list, tuple)):
            if len(item) != 2 or not isinstance(item[0], str) or not isinstance(item[1], str):
                raise ValueError(
                    f"{source}: '{field_name}[{idx}]' must be a [group_title, match_pattern] pair of strings"
                )
            title, pattern = item[0], item[1]
        elif isinstance(item, dict):
            entry = {k: v for k, v in item.items() if not str(k).startswith("_")}
            title = entry.get("title", "")
            pattern = entry.get("pattern")
            if title is None:
                title = ""
            if not isinstance(title, str) or not isinstance(pattern, str):
                raise ValueError(
                    f"{source}: '{field_name}[{idx}]' object must have string 'title' and 'pattern'"
                )
        else:
            raise ValueError(
                f"{source}: '{field_name}[{idx}]' must be a [group_title, match_pattern] list"
            )

        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(
                f"{source}: invalid regex {pattern!r} in '{field_name}[{idx}]': {exc}"
            ) from exc

        groups.append(ScheduleGroupConfig(title=title, pattern=pattern))

    return groups


def _parse_group_unmatched_order(raw: object, source: str) -> str:
    if raw is None:
        return "chronological"
    if not isinstance(raw, str):
        raise ValueError(f"{source}: 'group_unmatched_order' must be a string")
    normalized = raw.strip().lower()
    if normalized in ("inline", "chronological"):
        return "chronological"
    if normalized in ("below", "after", "bottom"):
        return "below"
    raise ValueError(
        f"{source}: 'group_unmatched_order' must be one of {VALID_GROUP_UNMATCHED_ORDERS}, got {raw!r}"
    )


def combine_config_from_dict(
    raw_dict: dict,
    source: str = "config.json",
    base_dir: Path | None = None,
) -> CombineConfig:
    """Parses and validates a ``CombineConfig`` from a decoded JSON object,
    ignoring comment keys beginning with ``'_'``."""
    if not isinstance(raw_dict, dict):
        raise ValueError(f"{source}: top-level JSON value must be an object")

    data = {k: v for k, v in raw_dict.items() if not str(k).startswith("_")}

    # Accept convenient aliases ("patterns" / "pattern") alongside canonical "no_suffix_patterns"
    raw_patterns = data.get("no_suffix_patterns")
    if raw_patterns is None and "patterns" in data:
        raw_patterns = data["patterns"]
    elif raw_patterns is None and "pattern" in data:
        raw_patterns = data["pattern"]
    patterns = _parse_patterns_field(raw_patterns, source)

    report_title = _parse_optional_str(data.get("report_title"), "report_title", source)
    report_subtitle = _parse_optional_str(data.get("report_subtitle"), "report_subtitle", source)
    report_footer = _parse_optional_str(data.get("report_footer"), "report_footer", source)

    raw_suite = data.get("luah_suite_path")
    if raw_suite is None and "luah_root_path" in data:
        raw_suite = data["luah_root_path"]
    luah_suite_path = _parse_optional_str(raw_suite, "luah_suite_path", source)
    pyzmanim_lib_path = _parse_optional_str(data.get("pyzmanim_lib_path"), "pyzmanim_lib_path", source)
    luah_signage_path = _parse_optional_str(data.get("luah_signage_path"), "luah_signage_path", source)

    location = _parse_location_field(data.get("location"), source)
    eretz_yisroel = data.get("eretz_yisroel")
    if eretz_yisroel is not None and not isinstance(eretz_yisroel, bool):
        raise ValueError(f"{source}: 'eretz_yisroel' must be a boolean or null")
    timezone = _parse_optional_str(data.get("timezone"), "timezone", source)
    extra_tables = _parse_extra_tables_field(data.get("extra_tables"), source)

    chol_groups = _parse_schedule_groups_field(data.get("chol_groups"), "chol_groups", source)
    shabbos_groups = _parse_schedule_groups_field(data.get("shabbos_groups"), "shabbos_groups", source)
    chol_groups_exclude = _parse_patterns_field(
        data.get("chol_groups_exclude"), source, "chol_groups_exclude"
    )
    shabbos_groups_exclude = _parse_patterns_field(
        data.get("shabbos_groups_exclude"), source, "shabbos_groups_exclude"
    )
    group_unmatched_order = _parse_group_unmatched_order(data.get("group_unmatched_order"), source)
    if "group_no_suffix_label" in data:
        group_no_suffix_label = _parse_optional_str(
            data.get("group_no_suffix_label"), "group_no_suffix_label", source
        )
    else:
        group_no_suffix_label = "pattern"

    sephardi_label = data.get("sephardi_label", DEFAULT_SEPHARDI_LABEL)
    if not isinstance(sephardi_label, str):
        raise ValueError(f"{source}: 'sephardi_label' must be a string")

    ashkenazi_label = data.get("ashkenazi_label", DEFAULT_ASHKENAZI_LABEL)
    if not isinstance(ashkenazi_label, str):
        raise ValueError(f"{source}: 'ashkenazi_label' must be a string")

    suffix_desc = data.get("suffix_desc", True)
    if not isinstance(suffix_desc, bool):
        raise ValueError(f"{source}: 'suffix_desc' must be a boolean")

    order = data.get("order", "chronological")
    if order not in VALID_ORDERS:
        raise ValueError(f"{source}: 'order' must be one of {VALID_ORDERS}, got {order!r}")

    generate_html = data.get("generate_html", True)
    if not isinstance(generate_html, bool):
        raise ValueError(f"{source}: 'generate_html' must be a boolean")

    return CombineConfig(
        no_suffix_patterns=patterns,
        report_title=report_title,
        report_subtitle=report_subtitle,
        report_footer=report_footer,
        luah_suite_path=luah_suite_path,
        pyzmanim_lib_path=pyzmanim_lib_path,
        luah_signage_path=luah_signage_path,
        location=location,
        eretz_yisroel=eretz_yisroel,
        timezone=timezone,
        extra_tables=extra_tables,
        chol_groups=chol_groups,
        shabbos_groups=shabbos_groups,
        chol_groups_exclude=chol_groups_exclude,
        shabbos_groups_exclude=shabbos_groups_exclude,
        group_unmatched_order=group_unmatched_order,
        group_no_suffix_label=group_no_suffix_label,
        sephardi_label=sephardi_label,
        ashkenazi_label=ashkenazi_label,
        suffix_desc=suffix_desc,
        order=order,
        generate_html=generate_html,
        base_dir=base_dir if base_dir is not None else HERE,
    )


def load_config(config_path: Path | None = None) -> CombineConfig:
    """Loads ``CombineConfig`` from ``config_path``.

    - When ``config_path`` is ``None``, loads ``DEFAULT_CONFIG_PATH`` if it exists,
      or returns default ``CombineConfig()`` if no ``config.json`` is present yet.
    - When ``config_path`` is explicitly provided, raises ``FileNotFoundError`` if
      that file does not exist.
    """
    target = config_path if config_path is not None else DEFAULT_CONFIG_PATH
    if not target.is_file():
        if config_path is not None:
            raise FileNotFoundError(f"Config file not found: {target}")
        return CombineConfig()

    resolved = target.resolve()
    raw_text = resolved.read_text(encoding="utf-8")
    raw_obj = json.loads(raw_text)
    return combine_config_from_dict(raw_obj, source=str(target), base_dir=resolved.parent)


def apply_cli_overrides(
    config: CombineConfig,
    *,
    patterns: list[str] | None = None,
    report_title: str | None = None,
    report_subtitle: str | None = None,
    report_footer: str | None = None,
    luah_suite_path: str | None = None,
    sephardi_label: str | None = None,
    ashkenazi_label: str | None = None,
    no_desc_suffix: bool = False,
    order: str | None = None,
    no_html: bool = False,
) -> CombineConfig:
    """Applies explicitly supplied CLI flags on top of a loaded ``CombineConfig``."""
    updated = replace(config)
    if patterns is not None:
        updated.no_suffix_patterns = _parse_patterns_field(patterns, "--pattern")
    if report_title is not None:
        updated.report_title = report_title
    if report_subtitle is not None:
        updated.report_subtitle = report_subtitle
    if report_footer is not None:
        updated.report_footer = report_footer
    if luah_suite_path is not None:
        updated.luah_suite_path = luah_suite_path
    if sephardi_label is not None:
        updated.sephardi_label = sephardi_label
    if ashkenazi_label is not None:
        updated.ashkenazi_label = ashkenazi_label
    if no_desc_suffix:
        updated.suffix_desc = False
    if order is not None:
        updated.order = order
    if no_html:
        updated.generate_html = False
    return updated

