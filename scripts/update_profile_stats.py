"""Generate README SVGs from public GitHub data, without an image service."""

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
IMAGE_NAMES = ("stats.svg", "languages.svg", "activity.svg")
QUERY = """
query($login: String!, $after: String) {
  user(login: $login) {
    login
    followers { totalCount }
    repositories(first: 100, after: $after, privacy: PUBLIC,
                 ownerAffiliations: [OWNER], isFork: false) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        stargazerCount
        forkCount
        languages(first: 100) {
          pageInfo { hasNextPage }
          edges { size node { name color } }
        }
      }
    }
  }
}
"""


def fetch(url, payload=None, token=None):
    headers = {"User-Agent": "github-profile-stats", "Accept-Language": "en-US"}
    if token:
        headers.update(Authorization=f"Bearer {token}", Accept="application/vnd.github+json")
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=payload, headers=headers)
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def public_repository_stats(username, token):
    repositories = []
    cursor = None
    while True:
        payload = json.dumps({"query": QUERY, "variables": {"login": username, "after": cursor}}).encode()
        response = json.loads(fetch("https://api.github.com/graphql", payload, token))
        if response.get("errors"):
            raise RuntimeError("GitHub GraphQL request failed: " + json.dumps(response["errors"]))
        user = response["data"]["user"]
        if user is None:
            raise RuntimeError(f"GitHub user not found: {username}")
        page = user["repositories"]
        repositories.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    languages = Counter()
    colors = {}
    for repository in repositories:
        if repository["languages"]["pageInfo"]["hasNextPage"]:
            raise RuntimeError("A repository has more than 100 languages; refusing incomplete statistics")
        for edge in repository["languages"]["edges"]:
            name = edge["node"]["name"]
            languages[name] += edge["size"]
            colors[name] = edge["node"]["color"] or "#7dcfff"
    return {
        "repositories": len(repositories),
        "stars": sum(repository["stargazerCount"] for repository in repositories),
        "forks": sum(repository["forkCount"] for repository in repositories),
        "followers": user["followers"]["totalCount"],
        "languages": languages,
        "colors": colors,
    }


class ContributionCalendar(HTMLParser):
    """Read the unauthenticated calendar so only public profile data is used."""

    def __init__(self):
        super().__init__()
        self.dates = {}
        self.counts = {}
        self.target = None
        self.text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "data-date" in attrs and "id" in attrs:
            self.dates[attrs["id"]] = date.fromisoformat(attrs["data-date"])
        if tag == "tool-tip":
            self.target = attrs.get("for")
            self.text = []

    def handle_data(self, data):
        if self.target:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "tool-tip" and self.target:
            match = re.match(r"\s*(No|[\d,]+) contributions?\b", "".join(self.text))
            if match:
                self.counts[self.target] = 0 if match[1] == "No" else int(match[1].replace(",", ""))
            self.target = None


def public_activity(username, today):
    start = today - timedelta(days=30)
    params = urlencode({"from": start.isoformat(), "to": today.isoformat()})
    calendar = ContributionCalendar()
    calendar.feed(fetch(f"https://github.com/users/{username}/contributions?{params}"))
    days = {day: calendar.counts[cell] for cell, day in calendar.dates.items() if cell in calendar.counts}
    expected = [start + timedelta(days=offset) for offset in range(31)]
    if any(day not in days for day in expected):
        raise RuntimeError("GitHub calendar is incomplete or its markup changed; keeping existing images")
    return [(day, days[day]) for day in expected]


def text(x, y, value, size=14, color="#a9b1d6", anchor="start", weight="400"):
    return (f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" '
            f'text-anchor="{anchor}" font-weight="{weight}">{escape(str(value))}</text>')


def card(width, height, title, description, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">\n'
            f'<title id="title">{escape(title)}</title>\n'
            f'<desc id="desc">{escape(description)}</desc>\n'
            f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" '
            'rx="12" fill="#1a1b27" stroke="#2a2f45"/>\n'
            '<g font-family="-apple-system, BlinkMacSystemFont, Segoe UI, sans-serif">\n'
            + "\n".join(body) + '\n</g>\n</svg>\n')


def stats_card(username, stats, updated_at):
    rows = [("Public repositories", stats["repositories"]), ("Stars earned", stats["stars"]),
            ("Forks received", stats["forks"]), ("Followers", stats["followers"])]
    body = [text(26, 36, "GitHub Stats", 22, "#70a5fd", weight="600")]
    for index, (label, value) in enumerate(rows):
        y = 76 + index * 34
        body.extend([text(26, y, label), text(414, y, f"{value:,}", 18, "#7dcfff", "end", "600")])
    body.extend([text(26, 222, "Repositories, stars and forks exclude forks and private repos.", 11),
                 text(26, 241, f"@{username} · Updated {updated_at}", 11)])
    return card(440, 258, f"{username}'s GitHub stats", "; ".join(f"{label}: {value}" for label, value in rows), body)


def languages_card(stats, updated_at):
    languages = stats["languages"]
    total = sum(languages.values())
    entries = [(name, count, stats["colors"][name]) for name, count in languages.most_common(5)]
    remainder = total - sum(count for _, count, _ in entries)
    if remainder:
        entries.append(("Other", remainder, "#6b7394"))
    body = [text(26, 36, "Most Used Languages", 22, "#70a5fd", weight="600")]
    for index, (name, count, color) in enumerate(entries):
        y = 68 + index * 27
        percentage = 100 * count / total
        body.extend([text(26, y, name, 13), text(414, y, f"{percentage:.1f}%", 13, anchor="end"),
                     f'<rect x="26" y="{y + 6}" width="388" height="5" rx="2.5" fill="#2a2f45"/>',
                     f'<rect x="26" y="{y + 6}" width="{388 * count / total:.2f}" height="5" rx="2.5" fill="{color}"/>'])
    if not total:
        body.append(text(26, 100, "No public language data available."))
    body.extend([text(26, 231, "Share of code bytes in owned public, non-fork repositories.", 11),
                 text(26, 249, f"Updated {updated_at}", 11)])
    description = "; ".join(f"{name}: {100 * count / total:.1f}%" for name, count, _ in entries)
    return card(440, 258, "Most used languages", description, body)


def activity_card(username, activity, today, updated_at):
    left, right, top, bottom = 54, 834, 90, 236
    maximum = max(4, math.ceil(max(count for _, count in activity) / 4) * 4)
    points = [(left + index * (right - left) / 30, bottom - count * (bottom - top) / maximum)
              for index, (_, count) in enumerate(activity)]
    body = [text(26, 36, "Contribution Activity", 22, "#70a5fd", weight="600"),
            text(26, 59, f"{sum(count for _, count in activity):,} contributions · {activity[0][0]} to {today}", 12)]
    for index in range(5):
        y = bottom - index * (bottom - top) / 4
        body.extend([f'<path d="M {left} {y} H {right}" stroke="#2a2f45"/>',
                     text(left - 12, y + 4, maximum * index // 4, 11, anchor="end")])
    coordinates = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    body.append(f'<polygon points="{left},{bottom} {coordinates} {right},{bottom}" fill="#70a5fd" opacity="0.12"/>')
    body.append(f'<polyline points="{coordinates}" fill="none" stroke="#7dcfff" stroke-width="2.5" stroke-linejoin="round"/>')
    for (day, count), (x, y) in zip(activity, points):
        body.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3" fill="#7dcfff"><title>{day}: {count} contributions</title></circle>')
    for index in (0, 7, 14, 21, 30):
        body.append(text(points[index][0], 258, activity[index][0].strftime("%m-%d"), 11, anchor="middle"))
    body.append(text(26, 285, f"Public GitHub contribution calendar · Updated {updated_at}", 11))
    return card(860, 302, f"{username}'s contribution activity", "Last 31 days of the public GitHub contribution calendar.", body)


def snapshot_digest(username, stats, activity):
    # Include the generator so presentation changes also rebuild existing cards.
    snapshot = {
        "username": username,
        "stats": stats,
        "activity": [(day.isoformat(), count) for day, count in activity],
        "generator": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def snapshot_unchanged(output_dir, digest):
    for name in IMAGE_NAMES:
        try:
            root = ET.parse(output_dir / name).getroot()
        except (OSError, ET.ParseError):
            return False
        if root.get("data-source-hash") != digest:
            return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default="xcy960815")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "assets" / "profile")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9-]+", args.username):
        parser.error("Invalid GitHub username")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        # GitHub CLI is only needed for local runs; Actions supplies GITHUB_TOKEN.
        token = subprocess.run(["gh", "auth", "token"], check=True, capture_output=True, text=True).stdout.strip()
    now = datetime.now(timezone(timedelta(hours=8)))
    today = now.date()
    stats = public_repository_stats(args.username, token)
    activity = public_activity(args.username, today)
    digest = snapshot_digest(args.username, stats, activity)
    if snapshot_unchanged(args.output_dir, digest):
        print("No data changes; keeping existing images and update timestamps.")
        return
    updated_at = now.strftime("%Y-%m-%d %H:%M UTC+08:00")
    images = {
        "stats.svg": stats_card(args.username, stats, updated_at),
        "languages.svg": languages_card(stats, updated_at),
        "activity.svg": activity_card(args.username, activity, today, updated_at),
    }
    images = {name: content.replace('<svg ', f'<svg data-source-hash="{digest}" ', 1)
              for name, content in images.items()}
    # Fetch and validate everything before replacing any existing image.
    for content in images.values():
        ET.fromstring(content)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in images.items():
        target = args.output_dir / name
        temporary = target.with_suffix(".svg.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(target)
        print(f"Updated {target}")


if __name__ == "__main__":
    main()
