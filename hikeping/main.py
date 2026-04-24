from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.blocking import BlockingScheduler

HIKE_URL = "https://www.stjohnshikeclub.com/upcoming-hike.html"
TIMEZONE = ZoneInfo(os.getenv("HIKEPING_TIMEZONE", "America/St_Johns"))
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
HIKEPING_INFO_WEBHOOK_URL = os.getenv("HIKEPING_INFO_WEBHOOK_URL", "").strip()
EVENTS_DATA_URL = "https://www.stjohnshikeclub.com/events-data.js"


class EventFeedError(Exception):
    pass


@dataclass(frozen=True)
class HikeEvent:
    date: datetime
    title: str
    start_time: str = ""
    location: str = ""
    difficulty: str = ""
    distance: str = ""
    cta_url: str = ""


def next_weekend_dates(now: datetime) -> tuple[datetime, datetime]:
    days_to_sat = (5 - now.weekday()) % 7
    if days_to_sat == 0:
        days_to_sat = 7
    saturday = (now + timedelta(days=days_to_sat)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    sunday = saturday + timedelta(days=1)
    return saturday, sunday


def fetch_events_js() -> str:
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        res = client.get(EVENTS_DATA_URL, headers={"User-Agent": "hikeping/0.1"})
        res.raise_for_status()
    return res.text


def _unescape_js_string(s: str) -> str:
    return json.loads(f'"{s}"')


def _extract_event_blocks(js: str) -> list[str]:
    blocks: list[str] = []
    depth = 0
    start: int | None = None
    in_string = False
    escaped = False

    for i, char in enumerate(js):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = i
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start is not None:
                blocks.append(js[start : i + 1])
                start = None

    return blocks


def _js_string_field(block: str, field: str) -> str:
    m = re.search(rf'\b{re.escape(field)}:\s*"(?P<value>(?:\\.|[^"\\])*)"', block)
    if not m:
        return ""
    return _unescape_js_string(m.group("value"))


def parse_events_js(js: str) -> list[HikeEvent]:
    events: list[HikeEvent] = []
    for block in _extract_event_blocks(js):
        title = _js_string_field(block, "title")
        date_value = _js_string_field(block, "date")
        if not title or not date_value:
            continue

        try:
            event_date = datetime.strptime(date_value, "%Y-%m-%d").replace(tzinfo=TIMEZONE)
        except ValueError:
            continue

        events.append(
            HikeEvent(
                date=event_date,
                title=title,
                start_time=_js_string_field(block, "startTime"),
                location=_js_string_field(block, "location"),
                difficulty=_js_string_field(block, "difficulty"),
                distance=_js_string_field(block, "distance"),
                cta_url=_js_string_field(block, "ctaUrl"),
            )
        )

    return events


def get_next_upcoming_event(now: datetime) -> HikeEvent | None:
    events = parse_events_js(fetch_events_js())
    upcoming: list[HikeEvent] = []
    today = now.date()
    for event in events:
        if event.date.date() >= today:
            upcoming.append(event)

    if not upcoming:
        return None

    upcoming.sort(key=lambda x: x.date)
    return upcoming[0]


def get_next_upcoming_hike(now: datetime) -> tuple[datetime, str] | None:
    event = get_next_upcoming_event(now)
    if event is None:
        return None
    return event.date, event.title


def get_upcoming_weekend_hike(now: datetime) -> HikeEvent | None:
    events = parse_events_js(fetch_events_js())
    if not events:
        raise EventFeedError("No events could be parsed from events-data.js")

    sat, sun = next_weekend_dates(now)
    weekend_dates = {sat.date(), sun.date()}
    weekend_events = [event for event in events if event.date.date() in weekend_dates]
    if not weekend_events:
        return None

    weekend_events.sort(key=lambda event: event.date)
    event = weekend_events[0]
    return event


def format_hike_message(event: HikeEvent) -> str:
    pretty_date = event.date.strftime("%a, %b %d").replace(" 0", " ")
    lines = [f"🌳 Next St. John's Hike Club hike: {event.title} ({pretty_date})"]
    if event.start_time:
        lines.append(f"Time: {event.start_time}")
    if event.location:
        lines.append(f"Location: {event.location}")
    if event.difficulty:
        lines.append(f"Difficulty: {event.difficulty}")
    if event.distance:
        lines.append(f"Distance: {event.distance}")
    if event.cta_url:
        lines.append(f"Register: {event.cta_url}")
    lines.append(HIKE_URL)
    return "\n".join(lines)


def notify_info(message: str) -> bool:
    if not HIKEPING_INFO_WEBHOOK_URL:
        print(message, file=sys.stderr)
        return False
    return post_discord_to_webhook(HIKEPING_INFO_WEBHOOK_URL, message)


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
    print(f"[{now.isoformat()}] Checking {EVENTS_DATA_URL}")
    try:
        event = get_upcoming_weekend_hike(now)
    except EventFeedError as exc:
        sat, sun = next_weekend_dates(now)
        msg = (
            "⚠️ hikeping could not find a complete upcoming weekend hike in "
            f"events-data.js for {sat.date().isoformat()}/{sun.date().isoformat()}. "
            f"The St. John's Hike Club site may have changed: {HIKE_URL}"
        )
        print(f"{msg} ({exc})", file=sys.stderr)
        try:
            notify_info(msg)
        except Exception as notify_exc:
            print(f"Failed to post info webhook: {notify_exc}", file=sys.stderr)
        return
    except Exception as exc:
        msg = f"⚠️ hikeping failed to fetch or parse events-data.js: {exc}"
        print(msg, file=sys.stderr)
        try:
            notify_info(msg)
        except Exception as notify_exc:
            print(f"Failed to post info webhook: {notify_exc}", file=sys.stderr)
        return

    if event:
        msg = format_hike_message(event)
        try:
            sent = post_discord_to_webhook(DISCORD_WEBHOOK_URL, msg)
            if sent:
                print("Posted to Discord.")
        except Exception as exc:
            print(f"Failed to post webhook: {exc}", file=sys.stderr)
    else:
        print("No upcoming weekend hike detected.")


def run_next_hike(post: bool = False, webhook_url: str | None = None) -> None:
    now = datetime.now(TIMEZONE)
    next_hike = get_next_upcoming_event(now)
    if not next_hike:
        print("No upcoming hikes found.")
        return

    msg = format_hike_message(next_hike)
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
