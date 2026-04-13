from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.blocking import BlockingScheduler
from bs4 import BeautifulSoup

HIKE_URL = "https://www.stjohnshikeclub.com/upcoming-hike.html"
TIMEZONE = ZoneInfo(os.getenv("HIKEPING_TIMEZONE", "America/St_Johns"))
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
EVENTS_DATA_URL = "https://www.stjohnshikeclub.com/events-data.js"


def next_weekend_dates(now: datetime) -> tuple[datetime, datetime]:
    days_to_sat = (5 - now.weekday()) % 7
    if days_to_sat == 0:
        days_to_sat = 7
    saturday = (now + timedelta(days=days_to_sat)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    sunday = saturday + timedelta(days=1)
    return saturday, sunday


def date_variants(d: datetime) -> list[str]:
    month_full = d.strftime("%B")
    month_abbr = d.strftime("%b")
    month_abbr_dot = f"{month_abbr}."
    day = str(d.day)
    year = d.strftime("%Y")
    weekday_full = d.strftime("%A")
    weekday_abbr = d.strftime("%a")

    return [
        f"{month_full} {day}",
        f"{month_full} {day}, {year}",
        f"{month_abbr} {day}",
        f"{month_abbr} {day}, {year}",
        f"{month_abbr_dot} {day}",
        f"{month_abbr_dot} {day}, {year}",
        f"{weekday_full}, {month_full} {day}",
        f"{weekday_full}, {month_abbr} {day}",
        f"{weekday_abbr}, {month_abbr} {day}",
    ]


def page_mentions_weekend_hike(page_text: str, now: datetime) -> bool:
    sat, sun = next_weekend_dates(now)
    haystack = re.sub(r"\s+", " ", page_text).lower()

    for needle in [*date_variants(sat), *date_variants(sun)]:
        if needle.lower() in haystack:
            return True

    # fallback: if page names this coming Sat/Sun with no explicit month
    sat_words = ["saturday", "sat"]
    sun_words = ["sunday", "sun"]
    if any(w in haystack for w in sat_words + sun_words) and (
        "upcoming" in haystack or "this weekend" in haystack
    ):
        return True

    return False


def fetch_page_text() -> str:
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        res = client.get(HIKE_URL, headers={"User-Agent": "hikeping/0.1"})
        res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")
    return soup.get_text(" ", strip=True)


def fetch_events_js() -> str:
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        res = client.get(EVENTS_DATA_URL, headers={"User-Agent": "hikeping/0.1"})
        res.raise_for_status()
    return res.text


def _unescape_js_string(s: str) -> str:
    return bytes(s, "utf-8").decode("unicode_escape")


def get_next_upcoming_hike(now: datetime) -> tuple[datetime, str] | None:
    js = fetch_events_js()
    # event objects consistently have title then date near the top
    pattern = re.compile(
        r'title:\s*"(?P<title>(?:\\.|[^"])*)".*?date:\s*"(?P<date>\d{4}-\d{2}-\d{2})"',
        re.DOTALL,
    )
    upcoming: list[tuple[datetime, str]] = []
    today = now.date()
    for m in pattern.finditer(js):
        d = datetime.strptime(m.group("date"), "%Y-%m-%d").replace(tzinfo=TIMEZONE)
        if d.date() >= today:
            upcoming.append((d, _unescape_js_string(m.group("title"))))

    if not upcoming:
        return None

    upcoming.sort(key=lambda x: x[0])
    return upcoming[0]


def post_discord(message: str) -> bool:
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL is not set", file=sys.stderr)
        return False

    with httpx.Client(timeout=20) as client:
        res = client.post(DISCORD_WEBHOOK_URL, json={"content": message})
        res.raise_for_status()
    return True


def post_discord_to_webhook(webhook_url: str, message: str) -> bool:
    if not webhook_url:
        print("Webhook URL is not set", file=sys.stderr)
        return False

    with httpx.Client(timeout=20) as client:
        res = client.post(webhook_url, json={"content": message})
        res.raise_for_status()
    return True


def check_and_notify() -> None:
    now = datetime.now(TIMEZONE)
    print(f"[{now.isoformat()}] Checking {HIKE_URL}")
    try:
        text = fetch_page_text()
    except Exception as exc:
        print(f"Failed to fetch hike page: {exc}", file=sys.stderr)
        return

    if page_mentions_weekend_hike(text, now):
        msg = f"🌳 Weekend hike looks posted: {HIKE_URL}"
        try:
            sent = post_discord(msg)
            if sent:
                print("Posted to Discord.")
        except Exception as exc:
            print(f"Failed to post webhook: {exc}", file=sys.stderr)
    else:
        print("No upcoming weekend hike detected.")


def run_next_hike(post: bool = False, webhook_url: str | None = None) -> None:
    now = datetime.now(TIMEZONE)
    next_hike = get_next_upcoming_hike(now)
    if not next_hike:
        print("No upcoming hikes found.")
        return

    date, title = next_hike
    pretty_date = date.strftime("%a, %b %d").replace(" 0", " ")
    msg = f"🌳 Next St. John's Hike Club hike: {title} ({pretty_date})\n{HIKE_URL}"
    print(msg)

    if post:
        target = webhook_url or DISCORD_WEBHOOK_URL
        sent = post_discord_to_webhook(target, msg)
        if sent:
            print("Posted to Discord.")


def main() -> None:
    parser = argparse.ArgumentParser(description="St. John's Hike Club pinger")
    parser.add_argument("--once", action="store_true", help="Run weekend check once")
    parser.add_argument(
        "--next",
        action="store_true",
        help="Show the next upcoming hike from events-data.js",
    )
    parser.add_argument(
        "--post",
        action="store_true",
        help="When used with --next, post the message to Discord webhook",
    )
    parser.add_argument(
        "--webhook-url",
        default="",
        help="Optional webhook override (otherwise DISCORD_WEBHOOK_URL env var)",
    )
    args = parser.parse_args()

    if args.next:
        run_next_hike(post=args.post, webhook_url=args.webhook_url or None)
        return

    if args.once:
        check_and_notify()
        return

    scheduler = BlockingScheduler(timezone=TIMEZONE)
    scheduler.add_job(check_and_notify, "cron", day_of_week="fri", hour=18, minute=0)
    print(
        "hikeping started. Runs every Friday at 6:00 PM",
        f"({TIMEZONE.key}). Use --once for immediate run.",
    )
    scheduler.start()


if __name__ == "__main__":
    main()
