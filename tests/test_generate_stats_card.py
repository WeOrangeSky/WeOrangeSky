"""Tests for scripts/generate_stats_card.py.

Run from the repository root: python3 -m unittest -v
Standard library only; all GitHub API calls are mocked.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import tempfile
import unittest
import urllib.error
import xml.dom.minidom
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from scripts import generate_stats_card as card

TEMPLATE = card.TEMPLATE_PATH.read_text(encoding="utf-8")
STATS = card.Stats(total_prs=1234, merged_prs=1000, total_commits=56789, total_contributions=4321)
NOW = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)


def fake_api(created_at="2023-05-01T00:00:00Z", per_year=100, commits=42, prs=10, merged=7):
    """Build a gh_request replacement that answers like the GitHub API and records calls."""
    calls = []

    def fake(url, token, payload=None):
        calls.append({"url": url, "token": token, "payload": payload})
        if "search/commits" in url:
            return {"total_count": commits}
        query = payload["query"]
        if "createdAt" in query:
            return {
                "data": {
                    "user": {
                        "createdAt": created_at,
                        "pullRequests": {"totalCount": prs},
                        "mergedPullRequests": {"totalCount": merged},
                    }
                }
            }
        years = re.findall(r"y(\d{4}):", query)
        return {"data": {"user": {f"y{y}": {"contributionCalendar": {"totalContributions": per_year}} for y in years}}}

    return fake, calls


class FrozenClockMixin:
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(card, "_now", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)


class RenderCardTests(unittest.TestCase):
    def render(self, theme="light", title="Test's Stats", **overrides):
        return card.render_card(TEMPLATE, card.THEMES[theme], title, replace(STATS, **overrides))

    def test_all_placeholders_replaced_for_every_theme(self):
        for theme in card.THEMES:
            with self.subTest(theme=theme):
                self.assertNotIn("{{", self.render(theme))

    def test_output_is_valid_xml_for_every_theme(self):
        for theme in card.THEMES:
            with self.subTest(theme=theme):
                xml.dom.minidom.parseString(self.render(theme))

    def test_theme_colours_applied(self):
        for theme, colours in card.THEMES.items():
            svg = self.render(theme)
            for key, colour in colours.items():
                with self.subTest(theme=theme, key=key):
                    self.assertIn(colour, svg)

    def test_numbers_formatted_with_thousands_separator(self):
        svg = self.render()
        for expected in (">1,234<", ">56,789<", ">4,321<"):
            self.assertIn(expected, svg)

    def test_merged_percentage(self):
        self.assertIn(">81.04%<", self.render())  # 1000 / 1234

    def test_merged_percentage_zero_prs_does_not_divide_by_zero(self):
        self.assertIn(">0.00%<", self.render(total_prs=0, merged_prs=0))

    def test_merged_percentage_all_merged(self):
        self.assertIn(">100.00%<", self.render(total_prs=5, merged_prs=5))

    def test_title_is_xml_escaped(self):
        svg = self.render(title='<script>&"x"</script>')
        self.assertNotIn("<script>", svg)
        self.assertIn("&lt;script&gt;&amp;&quot;x&quot;&lt;/script&gt;", svg)
        xml.dom.minidom.parseString(svg)

    def test_unknown_placeholder_left_untouched(self):
        out = card.render_card("{{TITLE}} {{UNKNOWN}}", {}, "T", STATS)
        self.assertEqual(out, "T {{UNKNOWN}}")

    def test_template_has_no_images(self):
        self.assertNotRegex(TEMPLATE, r"<(image|img)\b")


class FetchContributionsTests(FrozenClockMixin, unittest.TestCase):
    def test_one_window_per_year_and_sum(self):
        fake, calls = fake_api(per_year=100)
        with mock.patch.object(card, "gh_request", fake):
            total = card.fetch_contributions("user", "tok", "2023-05-01T00:00:00Z")

        self.assertEqual(total, 300)
        self.assertEqual(re.findall(r"y(\d{4}):", calls[0]["payload"]["query"]), ["2023", "2024", "2025"])

    def test_windows_are_within_one_year_and_not_in_future(self):
        fake, calls = fake_api()
        with mock.patch.object(card, "gh_request", fake):
            card.fetch_contributions("user", "tok", "2020-01-01T00:00:00Z")

        pairs = re.findall(r'from: "([^"]+)", to: "([^"]+)"', calls[0]["payload"]["query"])
        self.assertEqual(len(pairs), 6)
        for start, end in pairs:
            s = datetime.strptime(start, card.TIMESTAMP_FORMAT).replace(tzinfo=UTC)
            e = datetime.strptime(end, card.TIMESTAMP_FORMAT).replace(tzinfo=UTC)
            with self.subTest(start=start):
                self.assertLess(s, e)
                self.assertEqual(s.year, e.year)  # GitHub limits a window to 1 year
                self.assertLessEqual(e, NOW)

    def test_single_request(self):
        fake, calls = fake_api()
        with mock.patch.object(card, "gh_request", fake):
            card.fetch_contributions("user", "tok", "2015-01-01T00:00:00Z")
        self.assertEqual(len(calls), 1)

    def test_account_created_this_year_has_one_window(self):
        fake, calls = fake_api(per_year=5)
        with mock.patch.object(card, "gh_request", fake):
            total = card.fetch_contributions("user", "tok", "2025-03-01T00:00:00Z")
        self.assertEqual(total, 5)
        self.assertEqual(re.findall(r"y(\d{4}):", calls[0]["payload"]["query"]), ["2025"])


class FetchStatsTests(FrozenClockMixin, unittest.TestCase):
    def test_combines_all_sources(self):
        fake, calls = fake_api(created_at="2025-01-01T00:00:00Z", per_year=55, commits=42, prs=10, merged=7)
        with mock.patch.object(card, "gh_request", fake):
            stats = card.fetch_stats("octo-cat", "tok")

        self.assertEqual(stats, card.Stats(total_prs=10, merged_prs=7, total_commits=42, total_contributions=55))
        self.assertTrue(all(c["token"] == "tok" for c in calls))

    def test_commit_search_query_is_url_encoded(self):
        fake, calls = fake_api()
        with mock.patch.object(card, "gh_request", fake):
            card.fetch_stats("octo-cat", "tok")
        search = next(c["url"] for c in calls if "search/commits" in c["url"])
        self.assertIn("q=author%3Aocto-cat", search)

    def test_login_passed_as_graphql_variable(self):
        fake, calls = fake_api()
        with mock.patch.object(card, "gh_request", fake):
            card.fetch_stats("octo-cat", "tok")
        for c in calls:
            if c["payload"]:
                self.assertEqual(c["payload"]["variables"], {"login": "octo-cat"})

    def test_unknown_user_raises(self):
        with mock.patch.object(card, "gh_request", return_value={"data": {"user": None}}):
            with self.assertRaisesRegex(RuntimeError, "not found"):
                card.fetch_stats("ghost", "tok")


class GraphqlErrorTests(unittest.TestCase):
    def test_graphql_errors_raise(self):
        with mock.patch.object(card, "gh_request", return_value={"errors": [{"message": "boom"}]}):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                card.graphql("query", "user", "tok")

    def test_unknown_user_raises(self):
        with mock.patch.object(card, "gh_request", return_value={"data": {"user": None}}):
            with self.assertRaisesRegex(RuntimeError, "not found"):
                card.graphql("query", "ghost", "tok")


class GhRequestTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(card.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def http_error(code, body=b""):
        return urllib.error.HTTPError("https://api.example/x", code, "err", {}, io.BytesIO(body))

    def test_get_sends_headers_and_decodes_json(self):
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"ok": true}')) as urlopen:
            self.assertEqual(card.gh_request("https://api.example/x", "tok"), {"ok": True})
        req = urlopen.call_args.args[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.get_header("Authorization"), "Bearer tok")
        self.assertEqual(req.get_header("X-github-api-version"), card.API_VERSION)
        self.assertIsNone(req.get_header("Content-type"))
        self.assertEqual(urlopen.call_args.kwargs["timeout"], card.REQUEST_TIMEOUT)

    def test_post_when_payload_given(self):
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(b"{}")) as urlopen:
            card.gh_request("https://api.example/x", "tok", {"a": 1})
        req = urlopen.call_args.args[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(json.loads(req.data), {"a": 1})
        self.assertEqual(req.get_header("Content-type"), "application/json")

    def test_client_error_raises_without_retry(self):
        with mock.patch("urllib.request.urlopen", side_effect=self.http_error(401, b"Bad credentials")) as urlopen:
            with self.assertRaisesRegex(RuntimeError, r"HTTP 401: Bad credentials"):
                card.gh_request("https://api.example/x", "tok")
        self.assertEqual(urlopen.call_count, 1)
        self.sleep.assert_not_called()

    def test_transient_http_error_is_retried(self):
        responses = [self.http_error(502), self.http_error(503), io.BytesIO(b'{"ok": 1}')]
        with mock.patch("urllib.request.urlopen", side_effect=responses) as urlopen:
            self.assertEqual(card.gh_request("https://api.example/x", "tok"), {"ok": 1})
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_persistent_server_error_gives_up(self):
        errors = [self.http_error(500, b"down") for _ in range(card.MAX_ATTEMPTS)]
        with mock.patch("urllib.request.urlopen", side_effect=errors) as urlopen:
            with self.assertRaisesRegex(RuntimeError, r"HTTP 500: down"):
                card.gh_request("https://api.example/x", "tok")
        self.assertEqual(urlopen.call_count, card.MAX_ATTEMPTS)

    def test_network_error_is_retried_then_wrapped(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")) as urlopen:
            with self.assertRaisesRegex(RuntimeError, "no route"):
                card.gh_request("https://api.example/x", "tok")
        self.assertEqual(urlopen.call_count, card.MAX_ATTEMPTS)

    def test_timeout_is_retried_then_wrapped(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                card.gh_request("https://api.example/x", "tok")


class StatsTests(unittest.TestCase):
    def test_from_peak_env_ignores_missing_empty_and_invalid(self):
        env = {
            "PEAK_TOTAL_PRS": "12",
            "PEAK_MERGED_PRS": "",
            "PEAK_TOTAL_COMMITS": "abc",
            "PEAK_TOTAL_CONTRIBUTIONS": " 1500 ",
        }
        self.assertEqual(card.Stats.from_peak_env(env), card.Stats(12, 0, 0, 1500))

    def test_from_peak_env_rejects_unicode_digits_and_separators(self):
        env = {
            "PEAK_TOTAL_PRS": "²",
            "PEAK_MERGED_PRS": "１２",
            "PEAK_TOTAL_COMMITS": "1,500",
            "PEAK_TOTAL_CONTRIBUTIONS": "-5",
        }
        self.assertEqual(card.Stats.from_peak_env(env), card.Stats(0, 0, 0, 0))

    def test_max_with_keeps_max_per_stat(self):
        live = card.Stats(total_prs=5, merged_prs=9, total_commits=1, total_contributions=7)
        peaks = card.Stats(total_prs=8, merged_prs=3, total_commits=1, total_contributions=0)
        self.assertEqual(live.max_with(peaks), card.Stats(8, 9, 1, 7))

    def test_peak_var_names(self):
        self.assertEqual(card.peak_var("total_prs"), "PEAK_TOTAL_PRS")

    def test_write_github_output_noop_without_env(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            card.write_github_output(STATS, changed=True)  # must not raise


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "assets"
        patcher = mock.patch.object(card, "OUTPUT_DIR", self.out)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, argv, env):
        """Run main() and return (exit code, stderr)."""
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, env, clear=True),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
        ):
            code = card.main(argv)
        return code, stderr.getvalue()

    def read_card(self, theme="light"):
        return (self.out / f"stats-card-{theme}.svg").read_text(encoding="utf-8")

    def test_sample_writes_one_file_per_theme(self):
        code, _ = self.run_main(["--sample"], {})
        self.assertEqual(code, 0)
        for theme in card.THEMES:
            with self.subTest(theme=theme):
                svg = self.read_card(theme)
                xml.dom.minidom.parseString(svg)
                self.assertIn("Pavel Kobiak&apos;s GitHub Stats", svg)

    def test_card_title_env_overrides_default(self):
        self.run_main(["--sample"], {"CARD_TITLE": "Custom Title"})
        self.assertIn(">Custom Title<", self.read_card())

    def test_missing_token_fails_with_message(self):
        code, stderr = self.run_main([], {})
        self.assertEqual(code, 1)
        self.assertIn("STATS_TOKEN is required", stderr)

    def test_api_error_fails_with_message(self):
        with mock.patch.object(card, "fetch_stats", side_effect=RuntimeError("boom")):
            code, stderr = self.run_main([], {"STATS_TOKEN": "tok"})
        self.assertEqual(code, 1)
        self.assertIn("boom", stderr)
        self.assertFalse(self.out.exists())

    def test_username_from_env(self):
        with mock.patch.object(card, "fetch_stats", return_value=STATS) as fetch:
            self.run_main([], {"STATS_TOKEN": "tok", "GH_USERNAME": "env-user"})
        fetch.assert_called_once_with("env-user", "tok")

    def test_positional_username_overrides_env(self):
        with mock.patch.object(card, "fetch_stats", return_value=STATS) as fetch:
            self.run_main(["cli-user"], {"STATS_TOKEN": "tok", "GH_USERNAME": "env-user"})
        fetch.assert_called_once_with("cli-user", "tok")

    def test_default_username(self):
        with mock.patch.object(card, "fetch_stats", return_value=STATS) as fetch:
            self.run_main([], {"STATS_TOKEN": "tok"})
        fetch.assert_called_once_with(card.DEFAULT_USERNAME, "tok")

    def run_live(self, live, peaks):
        """Run main() with mocked live stats and PEAK_* env; return GITHUB_OUTPUT as a dict."""
        gh_output = Path(self.tmp.name) / "gh_output"
        gh_output.touch()
        env = {
            "STATS_TOKEN": "tok",
            "GITHUB_OUTPUT": str(gh_output),
            **{card.peak_var(k): str(v) for k, v in peaks.items()},
        }
        with mock.patch.object(card, "fetch_stats", return_value=live):
            code, _ = self.run_main([], env)
        self.assertEqual(code, 0)
        return dict(line.split("=", 1) for line in gh_output.read_text(encoding="utf-8").splitlines())

    def test_card_uses_peak_when_live_value_dropped(self):
        outputs = self.run_live(
            STATS,
            {
                "total_prs": 2000,
                "merged_prs": 1000,
                "total_commits": 60000,
                "total_contributions": 4321,
            },
        )
        svg = self.read_card()
        self.assertIn(">2,000<", svg)
        self.assertIn(">60,000<", svg)
        self.assertIn(">50.00%<", svg)  # 1000 merged / 2000 peak total
        self.assertEqual(outputs["changed"], "false")
        self.assertEqual(outputs["PEAK_TOTAL_PRS"], "2000")

    def test_new_highs_are_written_to_github_output(self):
        outputs = self.run_live(
            STATS, {"total_prs": 1234, "merged_prs": 1000, "total_commits": 56789, "total_contributions": 100}
        )
        self.assertIn(">4,321<", self.read_card())
        self.assertEqual(outputs["changed"], "true")
        self.assertEqual(outputs["PEAK_TOTAL_CONTRIBUTIONS"], "4321")
        self.assertEqual(outputs["PEAK_TOTAL_PRS"], "1234")

    def test_no_stored_peaks_uses_live_and_marks_changed(self):
        outputs = self.run_live(STATS, {})
        self.assertIn(">1,234<", self.read_card())
        self.assertEqual(outputs["changed"], "true")


if __name__ == "__main__":
    unittest.main()
