#!/usr/bin/env python3
"""Render GitHub statistics into light/dark SVG cards (assets/stats-card-<theme>.svg).

Usage:
    STATS_TOKEN=... python3 scripts/generate_stats_card.py [username]
    python3 scripts/generate_stats_card.py --sample   # sample values, no API calls

Environment:
    STATS_TOKEN   token for API calls (required unless --sample)
    GH_USERNAME   GitHub login (default for the positional argument; fallback "WeOrangeSky")
    CARD_TITLE    card title (default: "Pavel Kobiak's GitHub Stats")
    PEAK_TOTAL_PRS, PEAK_MERGED_PRS, PEAK_TOTAL_COMMITS, PEAK_TOTAL_CONTRIBUTIONS
                  previously stored highs. The card shows max(live, peak) per stat so
                  numbers never drop (e.g. after losing access to a private repository).
                  New highs are written to $GITHUB_OUTPUT for the workflow to persist.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from xml.sax.saxutils import escape

ROOT: Final = Path(__file__).resolve().parent.parent
TEMPLATE_PATH: Final = ROOT / "templates" / "stats-card.svg"
OUTPUT_DIR: Final = ROOT / "assets"
DEFAULT_TITLE: Final = "Pavel Kobiak's GitHub Stats"
DEFAULT_USERNAME: Final = "WeOrangeSky"

API_VERSION: Final = "2022-11-28"
REQUEST_TIMEOUT: Final = 30
MAX_ATTEMPTS: Final = 3
RETRY_STATUSES: Final = frozenset({429, 500, 502, 503, 504})

THEMES: Final = {
    "light": {
        "BG_COLOR": "#fffefe",
        "BORDER_COLOR": "#e4e2e2",
        "TITLE_COLOR": "#2f80ed",
        "TEXT_COLOR": "#434d58",
    },
    "dark": {
        "BG_COLOR": "#0d1117",
        "BORDER_COLOR": "#30363d",
        "TITLE_COLOR": "#58a6ff",
        "TEXT_COLOR": "#c9d1d9",
    },
}

STATS_QUERY: Final = """
query ($login: String!) {
  user(login: $login) {
    createdAt
    pullRequests(first: 1) { totalCount }
    mergedPullRequests: pullRequests(states: MERGED, first: 1) { totalCount }
  }
}
"""

# contributionsCollection spans at most one year, so query one aliased window per year.
CONTRIBUTIONS_QUERY: Final = """
query ($login: String!) {{
  user(login: $login) {{
{windows}
  }}
}}
"""
CONTRIBUTIONS_WINDOW: Final = """\
    y{year}: contributionsCollection(from: "{start}", to: "{end}") {{
      contributionCalendar {{ totalContributions }}
    }}"""
TIMESTAMP_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True, slots=True)
class Stats:
    total_prs: int
    merged_prs: int
    total_commits: int
    total_contributions: int

    @property
    def merged_percent(self) -> float:
        return self.merged_prs / self.total_prs * 100 if self.total_prs else 0.0

    def max_with(self, other: Stats) -> Stats:
        return Stats(**{f.name: max(getattr(self, f.name), getattr(other, f.name)) for f in fields(self)})

    @classmethod
    def from_peak_env(cls, env: Mapping[str, str]) -> Stats:
        """Read stored highs from PEAK_* variables; missing or invalid values count as 0."""
        values = {}
        for f in fields(cls):
            raw = (env.get(peak_var(f.name)) or "").strip()
            values[f.name] = int(raw) if raw.isascii() and raw.isdigit() else 0
        return cls(**values)


SAMPLE_STATS: Final = Stats(total_prs=128, merged_prs=112, total_commits=1543, total_contributions=2876)


def peak_var(stat: str) -> str:
    """Name of the env/repository variable storing the peak of a stat, e.g. PEAK_TOTAL_PRS."""
    return f"PEAK_{stat.upper()}"


def _now() -> datetime:
    return datetime.now(UTC)


def gh_request(url: str, token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call the GitHub API and return decoded JSON, retrying transient failures."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "stats-card-generator",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        last = attempt == MAX_ATTEMPTS
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return json.load(response)
        except urllib.error.HTTPError as err:
            if err.code not in RETRY_STATUSES or last:
                raise RuntimeError(f"{url} -> HTTP {err.code}: {err.read().decode(errors='replace')}") from err
        except (urllib.error.URLError, TimeoutError) as err:
            if last:
                raise RuntimeError(f"{url} -> {err}") from err
        time.sleep(2**attempt)
    raise AssertionError("unreachable")


def graphql(query: str, login: str, token: str) -> dict[str, Any]:
    response = gh_request(
        "https://api.github.com/graphql",
        token,
        {"query": query, "variables": {"login": login}},
    )
    if response.get("errors"):
        raise RuntimeError(f"GraphQL error: {json.dumps(response['errors'])}")
    user = response["data"]["user"]
    if user is None:
        raise RuntimeError(f'User "{login}" not found')
    return user


def fetch_contributions(login: str, token: str, created_at: str) -> int:
    """Return all-time contributions, summed over one window per calendar year."""
    now = _now()
    windows = []
    for year in range(int(created_at[:4]), now.year + 1):
        start = datetime(year, 1, 1, tzinfo=UTC)
        end = min(datetime(year, 12, 31, 23, 59, 59, tzinfo=UTC), now)
        windows.append(
            CONTRIBUTIONS_WINDOW.format(
                year=year,
                start=start.strftime(TIMESTAMP_FORMAT),
                end=end.strftime(TIMESTAMP_FORMAT),
            )
        )
    user = graphql(CONTRIBUTIONS_QUERY.format(windows="\n".join(windows)), login, token)
    return sum(window["contributionCalendar"]["totalContributions"] for window in user.values())


def fetch_stats(login: str, token: str) -> Stats:
    user = graphql(STATS_QUERY, login, token)
    search = urllib.parse.quote(f"author:{login}")
    commits = gh_request(f"https://api.github.com/search/commits?q={search}&per_page=1", token)
    return Stats(
        total_prs=user["pullRequests"]["totalCount"],
        merged_prs=user["mergedPullRequests"]["totalCount"],
        total_commits=commits["total_count"],
        total_contributions=fetch_contributions(login, token, user["createdAt"]),
    )


def render_card(template: str, theme: Mapping[str, str], title: str, stats: Stats) -> str:
    values = {
        **theme,
        "TITLE": title,
        "TOTAL_PRS": f"{stats.total_prs:,}",
        "TOTAL_COMMITS": f"{stats.total_commits:,}",
        "MERGED_PRS_PERCENT": f"{stats.merged_percent:.2f}%",
        "TOTAL_CONTRIBUTIONS": f"{stats.total_contributions:,}",
    }

    def substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)
        return escape(values[key], {'"': "&quot;", "'": "&apos;"})

    return re.sub(r"\{\{(\w+)\}\}", substitute, template)


def write_github_output(stats: Stats, changed: bool) -> None:
    """Expose peaks to later workflow steps via $GITHUB_OUTPUT (no-op outside Actions)."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as output:
        for f in fields(stats):
            output.write(f"{peak_var(f.name)}={getattr(stats, f.name)}\n")
        output.write(f"changed={str(changed).lower()}\n")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate GitHub stats cards.")
    parser.add_argument(
        "username",
        nargs="?",
        default=os.environ.get("GH_USERNAME") or DEFAULT_USERNAME,
        help="GitHub login (default: $GH_USERNAME or %(default)s)",
    )
    parser.add_argument("--sample", action="store_true", help="render sample values without API calls")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.sample:
        stats = SAMPLE_STATS
    else:
        token = os.environ.get("STATS_TOKEN")
        if not token:
            print("STATS_TOKEN is required (or pass --sample)", file=sys.stderr)
            return 1
        try:
            live = fetch_stats(args.username, token)
        except RuntimeError as err:
            print(err, file=sys.stderr)
            return 1
        peaks = Stats.from_peak_env(os.environ)
        stats = live.max_with(peaks)
        print(f"live={live}\npeaks={peaks}")
        write_github_output(stats, changed=stats != peaks)

    title = os.environ.get("CARD_TITLE") or DEFAULT_TITLE
    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, theme in THEMES.items():
        output = OUTPUT_DIR / f"stats-card-{name}.svg"
        output.write_text(render_card(template, theme, title, stats), encoding="utf-8")
        print(f"Wrote {output}")
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
