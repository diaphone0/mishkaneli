#!/usr/bin/env python3
"""Unit tests for combine_prayer_times.py (stdlib unittest, no network needed)."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import combine_prayer_times as cpt
import config as mg_config
import fetch_prayer_times as fpt
import get_prayer_times as gpt


class CombinePrayerTimesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sephardi_sample = {
            "fetched_at": "2026-11-16T15:00:00+00:00",
            "page_date": "שני , ו' כסלו",
            "shul_name": "בית הכנסת הספרדי- משכן עלי",
            "weekday": [
                {"desc": "שחרית", "time": "06:30"},
                {"desc": "שחרית", "time": "07:30"},
                {"desc": "מנחה - פתח אליהו", "time": "16:25"},
                {"desc": "אשרי", "time": "16:35"},
                {"desc": "ערבית - לאחר מנחה", "time": ""},
            ],
            "shabbos": [
                {"desc": "מנחה ערב שבת", "time": "16:25"},
                {"desc": "קבלת שבת", "time": ""},
                {"desc": "שחרית", "time": "07:45"},
                {"desc": "מנחה", "time": "16:00"},
                {"desc": "ערבית צאת שבת", "time": "17:28"},
            ],
        }
        self.ashkenazi_sample = {
            "fetched_at": "2026-11-16T15:00:01+00:00",
            "page_date": "שני , ו' כסלו",
            "shul_name": "בית הכנסת  משכן עלי",
            "weekday": [
                {"desc": "שחרית", "time": "06:00"},
                {"desc": "מנחה (יום שישי)", "time": "13:00"},
                {"desc": "מנחה", "time": "16:25"},
                {"desc": "ערבית", "time": "17:05"},
            ],
            "shabbos": [
                {"desc": "מנחה ערב שבת וקבלת שבת", "time": "16:21"},
                {"desc": "מנין ראשון", "time": "06:15"},
                {"desc": "מנחה קטנה שניה", "time": "16:00"},
                {"desc": "ערבית מאוחרת", "time": "17:35"},
                {"desc": "", "time": ""},
            ],
        }

    def test_strips_empty_rows_and_adds_hebrew_suffix_and_minyan(self) -> None:
        combined, chol, shabbos = cpt.combine_boards(self.sephardi_sample, self.ashkenazi_sample)

        # Blank row {"desc": "", "time": ""} must be dropped
        self.assertEqual(len(shabbos["shabbos"]), 9)
        self.assertTrue(all(r["desc"] or r["time"] for r in shabbos["shabbos"]))

        # Check weekday order & attached timeless row
        self.assertEqual(
            [(r["time"], r["desc"], r["minyan"]) for r in chol["weekday"]],
            [
                ("06:00", "שחרית (אשכנזי)", "ashkenazi"),
                ("06:30", "שחרית (ספרדי)", "sephardi"),
                ("07:30", "שחרית (ספרדי)", "sephardi"),
                ("13:00", "מנחה (יום שישי) (אשכנזי)", "ashkenazi"),
                ("16:25", "מנחה - פתח אליהו (ספרדי)", "sephardi"),
                ("16:25", "מנחה (אשכנזי)", "ashkenazi"),
                ("16:35", "אשרי (ספרדי)", "sephardi"),
                ("", "ערבית - לאחר מנחה (ספרדי)", "sephardi"),
                ("17:05", "ערבית (אשכנזי)", "ashkenazi"),
            ],
        )

        # Check Shabbos multi-day wrap & attached Kabbalat Shabbat
        self.assertEqual(
            [(r["time"], r["desc"], r["minyan"]) for r in shabbos["shabbos"]],
            [
                ("16:21", "מנחה ערב שבת וקבלת שבת (אשכנזי)", "ashkenazi"),
                ("16:25", "מנחה ערב שבת (ספרדי)", "sephardi"),
                ("", "קבלת שבת (ספרדי)", "sephardi"),
                ("06:15", "מנין ראשון (אשכנזי)", "ashkenazi"),
                ("07:45", "שחרית (ספרדי)", "sephardi"),
                ("16:00", "מנחה (ספרדי)", "sephardi"),
                ("16:00", "מנחה קטנה שניה (אשכנזי)", "ashkenazi"),
                ("17:28", "ערבית צאת שבת (ספרדי)", "sephardi"),
                ("17:35", "ערבית מאוחרת (אשכנזי)", "ashkenazi"),
            ],
        )

        # Combined payload has both keys and works with get_prayer_times.format_times
        self.assertEqual(combined["weekday"], chol["weekday"])
        self.assertEqual(combined["shabbos"], shabbos["shabbos"])
        rendered = gpt.format_times(combined["shabbos"], sep=" - ", hide_empty_sep=True)
        self.assertIn("16:21 - מנחה ערב שבת וקבלת שבת (אשכנזי)", rendered)
        self.assertIn("קבלת שבת (ספרדי)", rendered)

    def test_shabbos_alignment_when_one_board_omits_erev_shabbos(self) -> None:
        board_a = cpt.prepare_board_rows(
            [
                {"desc": "מנחה ערב שבת", "time": "16:20"},
                {"desc": "שחרית", "time": "08:00"},
            ],
            minyan="sephardi",
            hebrew_label="ספרדי",
        )
        board_b = cpt.prepare_board_rows(
            [
                {"desc": "שחרית מוקדמת", "time": "06:30"},
                {"desc": "מנחה", "time": "16:00"},
            ],
            minyan="ashkenazi",
            hebrew_label="אשכנזי",
        )
        merged = cpt.merge_schedule_rows([board_a, board_b], is_shabbos=True)
        self.assertEqual(
            [r["time"] for r in merged],
            ["16:20", "06:30", "08:00", "16:00"],
        )

    def test_weekday_late_night_selichot_stays_after_evening(self) -> None:
        board_a = cpt.prepare_board_rows(
            [
                {"desc": "שחרית", "time": "06:30"},
                {"desc": "ערבית", "time": "21:00"},
                {"desc": "סליחות", "time": "00:30"},
            ],
            minyan="sephardi",
            hebrew_label="ספרדי",
        )
        board_b = cpt.prepare_board_rows(
            [
                {"desc": "שחרית", "time": "06:00"},
                {"desc": "ערבית", "time": "22:15"},
                {"desc": "מנחה גדולה", "time": "13:00"},
            ],
            minyan="ashkenazi",
            hebrew_label="אשכנזי",
        )
        merged = cpt.merge_schedule_rows([board_a, board_b], is_shabbos=False)
        self.assertEqual(
            [r["time"] for r in merged],
            ["06:00", "06:30", "13:00", "21:00", "22:15", "00:30"],
        )

    def test_no_desc_suffix_option(self) -> None:
        combined, _, _ = cpt.combine_boards(
            self.sephardi_sample,
            self.ashkenazi_sample,
            suffix_desc=False,
        )
        self.assertEqual(combined["weekday"][0], {"desc": "שחרית", "time": "06:00", "minyan": "ashkenazi"})

    def test_pattern_skips_desc_suffix_only_for_matching_rows(self) -> None:
        sephardi = {
            "weekday": [
                {"desc": "שחרית", "time": "06:30"},
                {"desc": "שיעור בבית המדרש", "time": "20:00"},
            ],
            "shabbos": [],
        }
        ashkenazi = {
            "weekday": [
                {"desc": "שחרית", "time": "06:00"},
                {"desc": "ערבית בבית המדרש", "time": "20:15"},
            ],
            "shabbos": [],
        }
        combined, _, _ = cpt.combine_boards(
            sephardi,
            ashkenazi,
            no_suffix_patterns=[r".*המדרש.*"],
        )
        self.assertEqual(
            combined["weekday"],
            [
                {"desc": "שחרית (אשכנזי)", "time": "06:00", "minyan": "ashkenazi"},
                {"desc": "שחרית (ספרדי)", "time": "06:30", "minyan": "sephardi"},
                {"desc": "שיעור בבית המדרש", "time": "20:00", "minyan": "sephardi"},
                {"desc": "ערבית בבית המדרש", "time": "20:15", "minyan": "ashkenazi"},
            ],
        )

    def test_render_html_report_contains_both_schedules_and_escapes_html(self) -> None:
        combined, chol, shabbos = cpt.combine_boards(
            self.sephardi_sample,
            self.ashkenazi_sample,
            no_suffix_patterns=[r".*המדרש.*"],
        )
        combined["page_date"] = 'שלישי , כ"ג חשוון <בדיקה>'
        doc = cpt.render_html_report(combined, mode="both")
        self.assertIn('<html lang="he" dir="rtl">', doc)
        self.assertIn("זמני תפילות לימי חול", doc)
        self.assertIn("זמני תפילות לשבת ומועד", doc)
        self.assertIn("שחרית (אשכנזי)", doc)
        self.assertIn("קבלת שבת (ספרדי)", doc)
        self.assertIn("&lt;בדיקה&gt;", doc)

        chol_doc = cpt.render_html_report(chol, mode="chol")
        self.assertIn("זמני תפילות לימי חול", chol_doc)
        self.assertNotIn("זמני תפילות לשבת ומועד", chol_doc)
        self.assertIn('class="container single"', chol_doc)

        shabbos_doc = cpt.render_html_report(shabbos, mode="shabbos")
        self.assertIn("זמני תפילות לשבת ומועד", shabbos_doc)
        self.assertNotIn("זמני תפילות לימי חול", shabbos_doc)
        self.assertIn("קבלת שבת (ספרדי)", shabbos_doc)

    def test_html_report_title_subtitle_and_footer_overrides(self) -> None:
        combined, _, _ = cpt.combine_boards(self.sephardi_sample, self.ashkenazi_sample)
        custom_doc = cpt.render_html_report(
            combined,
            mode="both",
            report_title="קהילת משכן עלי",
            report_subtitle="לוח זמנים מאוחד",
            report_footer="ט.ל.ח. • עודכן: {updated_at}",
        )
        self.assertIn("<h1>קהילת משכן עלי</h1>", custom_doc)
        self.assertIn('<div class="subtitle">לוח זמנים מאוחד</div>', custom_doc)
        self.assertNotIn("בית הכנסת הספרדי", custom_doc)
        self.assertIn('<footer class="footer">ט.ל.ח. • עודכן: <span dir="ltr">', custom_doc)

        hidden_sub_doc = cpt.render_html_report(
            combined,
            mode="chol",
            report_title="משכן עלי",
            report_subtitle="",
            report_footer="",
        )
        self.assertIn("<h1>משכן עלי</h1>", hidden_sub_doc)
        self.assertNotIn('<div class="subtitle">', hidden_sub_doc)
        self.assertNotIn("<footer", hidden_sub_doc)

    def test_config_loading_and_cli_override_precedence(self) -> None:
        cfg = mg_config.combine_config_from_dict(
            {
                "_comment": "ignored documentation key",
                "no_suffix_patterns": [".*המדרש.*"],
                "report_title": "משכן עלי",
                "report_subtitle": None,
                "report_footer": "שבת שלום",
            }
        )
        self.assertEqual(cfg.no_suffix_patterns, [".*המדרש.*"])
        self.assertEqual(cfg.report_title, "משכן עלי")
        self.assertIsNone(cfg.report_subtitle)
        self.assertEqual(cfg.report_footer, "שבת שלום")

        overridden = mg_config.apply_cli_overrides(
            cfg,
            report_subtitle="שבוע טוב",
            report_footer="חודש טוב",
            no_html=True,
        )
        self.assertEqual(overridden.no_suffix_patterns, [".*המדרש.*"])
        self.assertEqual(overridden.report_title, "משכן עלי")
        self.assertEqual(overridden.report_subtitle, "שבוע טוב")
        self.assertEqual(overridden.report_footer, "חודש טוב")
        self.assertFalse(overridden.generate_html)

        example_cfg = mg_config.load_config(Path(__file__).resolve().parent / "config.example.json")
        self.assertEqual(example_cfg.no_suffix_patterns, [".*המדרש.*"])
        self.assertEqual(example_cfg.luah_suite_path, "..")
        self.assertIsNotNone(example_cfg.location)
        assert example_cfg.location is not None
        self.assertAlmostEqual(example_cfg.location.latitude, 32.06)
        self.assertAlmostEqual(example_cfg.location.longitude, 35.26)
        self.assertAlmostEqual(example_cfg.location.elevation, 720.0)
        self.assertTrue(example_cfg.eretz_yisroel)
        self.assertEqual(example_cfg.timezone, "Asia/Jerusalem")

    def test_luah_tag_renderer_expands_tags_when_configured_and_leaves_untouched_when_missing(self) -> None:
        combined, _, _ = cpt.combine_boards(self.sephardi_sample, self.ashkenazi_sample)
        fixed_now = datetime(2026, 11, 16, 10, 0, 0)

        # 1. Configured via luah_suite_path=".." -> renders #HEBDATE / #PARSHA
        cfg_ok = mg_config.CombineConfig(luah_suite_path="..")
        renderer = cpt.build_luah_tag_renderer(cfg_ok, now=fixed_now)
        self.assertIsNotNone(renderer)
        rendered_doc = cpt.render_html_report(
            combined,
            mode="both",
            report_title="משכן עלי — #PARSHA",
            report_subtitle="#HEBDATE",
            report_footer="תאריך עברי: #HEBDATE",
            tag_renderer=renderer,
        )
        self.assertNotIn("#HEBDATE", rendered_doc)
        self.assertNotIn("#PARSHA", rendered_doc)
        self.assertIn("כסלו", rendered_doc)

        # 2. Unconfigured (luah_suite_path=None) -> leaves #HEBDATE literal
        cfg_none = mg_config.CombineConfig(luah_suite_path=None)
        self.assertIsNone(cpt.build_luah_tag_renderer(cfg_none, now=fixed_now))
        unrendered_doc = cpt.render_html_report(
            combined,
            mode="chol",
            report_footer="#HEBDATE",
            tag_renderer=cpt.build_luah_tag_renderer(cfg_none, now=fixed_now),
        )
        self.assertIn('<footer class="footer">#HEBDATE</footer>', unrendered_doc)

        # 3. Configured path does not exist -> leaves #HEBDATE literal without error
        cfg_missing = mg_config.CombineConfig(luah_suite_path="./nonexistent_luah_dir")
        self.assertIsNone(cpt.build_luah_tag_renderer(cfg_missing, now=fixed_now))
        missing_doc = cpt.render_html_report(
            combined,
            mode="shabbos",
            report_subtitle="#HEBDATE",
            tag_renderer=cpt.build_luah_tag_renderer(cfg_missing, now=fixed_now),
        )
        self.assertIn('<div class="subtitle">#HEBDATE</div>', missing_doc)


        # 4. Explicit location / timezone in mg_sync config overrides zmanim calculation
        cfg_nyc = mg_config.CombineConfig(
            luah_suite_path="..",
            location=mg_config.LocationConfig(latitude=40.7128, longitude=-74.0060, elevation=10.0),
            eretz_yisroel=False,
            timezone="America/New_York",
        )
        cfg_eli = mg_config.CombineConfig(
            luah_suite_path="..",
            location=mg_config.LocationConfig(latitude=32.06, longitude=35.26, elevation=720.0),
            eretz_yisroel=True,
            timezone="Asia/Jerusalem",
        )
        r_nyc = cpt.build_luah_tag_renderer(cfg_nyc, now=fixed_now)
        r_eli = cpt.build_luah_tag_renderer(cfg_eli, now=fixed_now)
        assert r_nyc is not None and r_eli is not None
        self.assertNotEqual(r_nyc("#SUNSET"), r_eli("#SUNSET"))

    def test_extra_tables_config_parsing_and_tag_rendering(self) -> None:
        combined, chol, shabbos = cpt.combine_boards(self.sephardi_sample, self.ashkenazi_sample)
        fixed_now = datetime(2026, 11, 16, 10, 0, 0)

        cfg = mg_config.combine_config_from_dict(
            {
                "luah_suite_path": "..",
                "extra_tables": [
                    {
                        "title": "זמני היום — #PARSHA",
                        "rows": [
                            ["#SUNRISE", "הנץ החמה"],
                            ["#SUNSET", "שקיעה <בדיקה>"],
                            ["", "שיעור דף היומי: #DAFYOMIBV"],
                            ["", "#DAYINFO"],  # empty on plain 6 Kislev -> skipped automatically
                        ],
                    },
                    {
                        "title": "זמני שבת בלבד",
                        "mode": "shabbos",
                        "rows": [
                            ["#SHABBOS-30", "הדלקת נרות"],
                        ],
                    },
                ],
            }
        )
        self.assertEqual(len(cfg.extra_tables), 2)
        self.assertEqual(cfg.extra_tables[0].rows[0], ("#SUNRISE", "הנץ החמה"))
        self.assertEqual(cfg.extra_tables[1].mode, "shabbos")

        renderer = cpt.build_luah_tag_renderer(cfg, now=fixed_now)
        assert renderer is not None
        expected_sunrise = renderer("#SUNRISE")
        expected_parsha = renderer("#PARSHA")
        expected_daf = renderer("#DAFYOMIBV")
        expected_candles = renderer("#SHABBOS-30")
        self.assertEqual(renderer("#DAYINFO"), "")

        # "both" HTML includes both extra tables with all tags expanded & HTML escaped
        both_doc = cpt.render_html_report(
            combined,
            mode="both",
            extra_tables=cfg.extra_tables,
            tag_renderer=renderer,
        )
        self.assertIn(f'<h2 class="card-header">זמני היום — {expected_parsha}</h2>', both_doc)
        self.assertIn(
            f'<tr class="row"><td class="desc">הנץ החמה</td><td class="time" dir="ltr">{expected_sunrise}</td></tr>',
            both_doc,
        )
        self.assertIn("שקיעה &lt;בדיקה&gt;", both_doc)
        self.assertIn(f'<td class="desc" colspan="2">שיעור דף היומי: {expected_daf}</td>', both_doc)
        self.assertNotIn("#DAYINFO", both_doc)
        self.assertNotIn('<td class="desc" colspan="2"></td>', both_doc)
        self.assertIn("זמני שבת בלבד", both_doc)
        self.assertIn(expected_candles, both_doc)

        # "chol" HTML includes the default ("all") table, omits the "shabbos"-only table
        chol_doc = cpt.render_html_report(
            chol,
            mode="chol",
            extra_tables=cfg.extra_tables,
            tag_renderer=renderer,
        )
        self.assertIn(f"זמני היום — {expected_parsha}", chol_doc)
        self.assertNotIn("זמני שבת בלבד", chol_doc)

        # "shabbos" HTML includes both
        shabbos_doc = cpt.render_html_report(
            shabbos,
            mode="shabbos",
            extra_tables=cfg.extra_tables,
            tag_renderer=renderer,
        )
        self.assertIn(f"זמני היום — {expected_parsha}", shabbos_doc)
        self.assertIn("זמני שבת בלבד", shabbos_doc)

        # Without tag_renderer, #TAGs remain literal in title, time, and desc
        raw_doc = cpt.render_html_report(
            combined,
            mode="both",
            extra_tables=[{"title": "#PARSHA", "rows": [["#SUNRISE", "#HEBDATE"]]}],
            tag_renderer=None,
        )
        self.assertIn('<h2 class="card-header">#PARSHA</h2>', raw_doc)
        self.assertIn('<td class="desc">#HEBDATE</td><td class="time" dir="ltr">#SUNRISE</td>', raw_doc)

    def test_schedule_groups_merging_unmatched_order_and_no_suffix_label_modes(self) -> None:
        sephardi = {
            "weekday": [
                {"desc": "שחרית", "time": "06:30"},
                {"desc": "מנחה - פתח אליהו", "time": "16:15"},
                {"desc": "אשרי", "time": "16:25"},
                {"desc": "ערבית - לאחר מנחה", "time": ""},
            ],
            "shabbos": [],
        }
        ashkenazi = {
            "weekday": [
                {"desc": "שחרית", "time": "06:00"},
                {"desc": "מנחה (יום שישי)", "time": "13:00"},
                {"desc": "מנחה", "time": "16:25"},
                {"desc": "ערבית", "time": "17:05"},
                {"desc": "ערבית בבית המדרש", "time": "20:15"},
            ],
            "shabbos": [],
        }
        _, chol, _ = cpt.combine_boards(
            sephardi,
            ashkenazi,
            no_suffix_patterns=[r".*המדרש.*"],
        )

        chol_groups = [
            ["שחרית:", r".*שחרית.*"],
            ["מנחה:", r"^(?!.*(?:ערבית|שישי)).*(?:מנחה|אשרי).*"],
            ["ערבית:", r".*ערבית.*"],
        ]

        # 1. Chronological order + "pattern" trimmed label ("20:15 (המדרש)")
        doc_chrono = cpt.render_html_report(
            chol,
            mode="chol",
            chol_groups=chol_groups,
            group_unmatched_order="chronological",
            group_no_suffix_label="pattern",
            no_suffix_patterns=[r".*המדרש.*"],
        )
        self.assertIn('<div class="group-title">שחרית:</div>', doc_chrono)
        self.assertIn(
            '<span class="group-time" dir="ltr">06:00</span> <span class="group-label">(אשכנזי)</span>,',
            doc_chrono,
        )
        self.assertIn(
            '<span class="group-time" dir="ltr">06:30</span> <span class="group-label">(ספרדי)</span>',
            doc_chrono,
        )
        # 16:25 merges Sephardi ("אשרי") + Ashkenazi ("מנחה") into "(ספרדי ואשכנזי)"
        self.assertIn(
            '<span class="group-time" dir="ltr">16:15</span> <span class="group-label">(ספרדי)</span>,</span> '
            '<span class="group-item"><span class="group-time" dir="ltr">16:25</span> '
            '<span class="group-label">(ספרדי ואשכנזי)</span></span>',
            doc_chrono,
        )
        # ".*המדרש.*" trims to "(המדרש)"
        self.assertIn(
            '<span class="group-time" dir="ltr">20:15</span> <span class="group-label">(המדרש)</span>',
            doc_chrono,
        )
        # Ungrouped timeless row uses .group-title + non-bold .group-label WITHOUT trailing ':'
        self.assertIn(
            '<tr class="row group-row minyan-sephardi"><td class="group-cell" colspan="2">'
            '<div class="group-title">ערבית - לאחר מנחה <span class="group-label">(ספרדי)</span></div>'
            "</td></tr>",
            doc_chrono,
        )
        # Chronological order: שחרית -> מנחה (יום שישי) -> מנחה -> ערבית - לאחר מנחה -> ערבית
        idx_shacharit = doc_chrono.index("שחרית:")
        idx_friday_mincha = doc_chrono.index("מנחה (יום שישי)")
        idx_mincha = doc_chrono.index("מנחה:</div>")
        idx_after_mincha = doc_chrono.index("ערבית - לאחר מנחה")
        idx_arvit = doc_chrono.index("ערבית:</div>")
        self.assertTrue(idx_shacharit < idx_friday_mincha < idx_mincha < idx_after_mincha < idx_arvit)

        # 2. "below" order + no label (None) -> "20:15" without parens, unmatched rows after all groups
        doc_below_nolabel = cpt.render_html_report(
            chol,
            mode="chol",
            chol_groups=chol_groups,
            group_unmatched_order="below",
            group_no_suffix_label=None,
            no_suffix_patterns=[r".*המדרש.*"],
        )
        self.assertIn(
            '<span class="group-item"><span class="group-time" dir="ltr">20:15</span></span>',
            doc_below_nolabel,
        )
        self.assertNotIn("(המדרש)", doc_below_nolabel)
        self.assertTrue(
            doc_below_nolabel.index("ערבית:</div>")
            < doc_below_nolabel.index("מנחה (יום שישי)")
            < doc_below_nolabel.index("ערבית - לאחר מנחה")
        )

        # 3. Fixed custom label -> "20:15 (בית המדרש)"
        doc_fixed = cpt.render_html_report(
            chol,
            mode="chol",
            chol_groups=chol_groups,
            group_no_suffix_label="בית המדרש",
            no_suffix_patterns=[r".*המדרש.*"],
        )
        self.assertIn(
            '<span class="group-time" dir="ltr">20:15</span> <span class="group-label">(בית המדרש)</span>',
            doc_fixed,
        )

        # 4. *_groups_exclude hides unmatched rows (e.g. "קבלת שבת", "אילן") while keeping
        #    grouped rows like "מנחה ערב שבת וקבלת שבת (אשכנזי)" inside "מנחה ערב שבת:"
        _, _, shabbos = cpt.combine_boards(
            {
                "weekday": [],
                "shabbos": [
                    {"desc": "מנחה ערב שבת", "time": "16:15"},
                    {"desc": "קבלת שבת", "time": ""},
                    {"desc": "שיעור - הרב אילן", "time": "10:20"},
                    {"desc": "מנחה גדולה", "time": "12:30"},
                ],
            },
            {
                "weekday": [],
                "shabbos": [
                    {"desc": "מנחה ערב שבת וקבלת שבת", "time": "16:21"},
                ],
            },
        )
        doc_shabbos_ex = cpt.render_html_report(
            shabbos,
            mode="shabbos",
            shabbos_groups=[["מנחה ערב שבת:", r".*ערב שבת.*"]],
            shabbos_groups_exclude=[r".*קבלת שבת.*", r".*אילן.*"],
            extra_tables=[{"title": "זמני היום", "rows": [["06:09", "זריחה"]]}],
        )
        self.assertIn("מנחה ערב שבת:</div>", doc_shabbos_ex)
        self.assertIn("16:15", doc_shabbos_ex)
        self.assertIn("16:21", doc_shabbos_ex)
        self.assertNotIn("קבלת שבת (ספרדי)", doc_shabbos_ex)
        self.assertNotIn("אילן", doc_shabbos_ex)
        # Ungrouped board row stacks time on a newline with non-bold label and trailing ':' when grouping is enabled:
        self.assertIn(
            '<div class="group-title">מנחה גדולה <span class="group-label">(ספרדי)</span>:</div>'
            '<div class="group-times"><span class="group-item">'
            '<span class="group-time" dir="ltr">12:30</span></span></div>',
            doc_shabbos_ex,
        )
        # Extra tables remain 2-column (unaffected by board grouping):
        self.assertIn(
            '<tr class="row"><td class="desc">זריחה</td><td class="time" dir="ltr">06:09</td></tr>',
            doc_shabbos_ex,
        )

    def test_board_url_resolution_precedence_and_secret_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            secrets_file = tmp_dir / "secrets.json"
            dotenv_file = tmp_dir / ".env"

            # Missing everywhere -> raises ValueError without leaking anything
            with self.assertRaises(ValueError):
                fpt.resolve_board_url(
                    "sephardi",
                    env={},
                    secrets_path=secrets_file,
                    dotenv_path=dotenv_file,
                )

            # .env fallback
            dotenv_file.write_text(
                'MYGABAY_SEPHARDI_URL="https://example.test/preview?app=DOTENV_TOK"\n'
                "export MYGABAY_ASHKENAZI_URL='https://example.test/preview?app=DOTENV_ASH'\n",
                encoding="utf-8",
            )
            self.assertEqual(
                fpt.resolve_board_url(
                    "sephardi",
                    env={},
                    secrets_path=secrets_file,
                    dotenv_path=dotenv_file,
                ),
                "https://example.test/preview?app=DOTENV_TOK",
            )
            self.assertEqual(
                fpt.resolve_board_url(
                    "ashkenazi",
                    env={},
                    secrets_path=secrets_file,
                    dotenv_path=dotenv_file,
                ),
                "https://example.test/preview?app=DOTENV_ASH",
            )

            # secrets.json overrides .env
            secrets_file.write_text(
                json.dumps({"sephardi_url": "https://example.test/preview?app=JSON_TOK"}),
                encoding="utf-8",
            )
            self.assertEqual(
                fpt.resolve_board_url(
                    "sephardi",
                    env={},
                    secrets_path=secrets_file,
                    dotenv_path=dotenv_file,
                ),
                "https://example.test/preview?app=JSON_TOK",
            )

            # Environment variable overrides secrets.json
            self.assertEqual(
                fpt.resolve_board_url(
                    "sephardi",
                    env={"MYGABAY_SEPHARDI_URL": "https://example.test/preview?app=ENV_TOK"},
                    secrets_path=secrets_file,
                    dotenv_path=dotenv_file,
                ),
                "https://example.test/preview?app=ENV_TOK",
            )

            # Explicit --url overrides environment variable
            self.assertEqual(
                fpt.resolve_board_url(
                    "sephardi",
                    url_override="https://example.test/preview?app=CLI_TOK",
                    env={"MYGABAY_SEPHARDI_URL": "https://example.test/preview?app=ENV_TOK"},
                    secrets_path=secrets_file,
                    dotenv_path=dotenv_file,
                ),
                "https://example.test/preview?app=CLI_TOK",
            )

        # Redaction scrubs both full URLs and urllib3 relative paths
        raw_err = (
            "500 Server Error for url: https://mygabay.com/board/tfilot/preview?app=SECRET_123 "
            "(Max retries exceeded with url: /board/tfilot/preview?app=SECRET_456)"
        )
        scrubbed = fpt.redact_url_secrets(raw_err)
        self.assertNotIn("SECRET_123", scrubbed)
        self.assertNotIn("SECRET_456", scrubbed)
        self.assertIn("<redacted>", scrubbed)

        # Regression check: no hardcoded CfDJ board tokens in tracked sources or config.json
        mg_dir = Path(__file__).resolve().parent
        for tracked_name in (
            "fetch_prayer_times.py",
            "combine_prayer_times.py",
            "config.py",
            "get_prayer_times.py",
            "config.json",
        ):
            tracked_path = mg_dir / tracked_name
            if tracked_path.is_file():
                src = tracked_path.read_text(encoding="utf-8")
                self.assertNotIn("CfDJ8Fy7VZW", src, f"Hardcoded board token found in {tracked_name}")

    def test_skip_if_unchanged_ignores_footer_diff_only(self) -> None:
        combined, _, _ = cpt.combine_boards(self.sephardi_sample, self.ashkenazi_sample)
        html_t1 = cpt.render_html_report(combined, report_footer="עודכן: 10:00")
        html_t2 = cpt.render_html_report(combined, report_footer="עודכן: 10:15")
        html_changed = cpt.render_html_report(
            combined,
            report_subtitle="כותרת משנה חדשה",
            report_footer="עודכן: 10:15",
        )

        self.assertFalse(cpt.html_has_meaningful_changes(html_t1, html_t2))
        self.assertTrue(cpt.html_has_meaningful_changes(html_t1, html_changed))

        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "index.html"

            # First write creates index.html
            self.assertTrue(cpt.write_html_output(out_path, html_t1, skip_if_unchanged=True))
            self.assertEqual(out_path.read_text(encoding="utf-8"), html_t1)

            # Footer-only change is skipped; original timestamp stays on disk
            self.assertFalse(cpt.write_html_output(out_path, html_t2, skip_if_unchanged=True))
            self.assertIn("עודכן: 10:00", out_path.read_text(encoding="utf-8"))

            # Real content change overwrites index.html (including updated footer)
            self.assertTrue(cpt.write_html_output(out_path, html_changed, skip_if_unchanged=True))
            disk_text = out_path.read_text(encoding="utf-8")
            self.assertIn("כותרת משנה חדשה", disk_text)
            self.assertIn("עודכן: 10:15", disk_text)


if __name__ == "__main__":
    unittest.main()

