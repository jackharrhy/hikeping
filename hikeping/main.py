from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import polyline
from apscheduler.schedulers.blocking import BlockingScheduler
from bs4 import BeautifulSoup
from staticmap import CircleMarker, Line, StaticMap

HIKE_URL = "https://www.stjohnshikeclub.com/upcoming-hike.html"
TIMEZONE = ZoneInfo(os.getenv("HIKEPING_TIMEZONE", "America/St_Johns"))
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
EVENTS_DATA_URL = "https://www.stjohnshikeclub.com/events-data.js"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_ROUTE_URL = "https://router.project-osrm.org/route/v1/foot"
GEOCODE_CACHE_PATH = Path(__file__).resolve().parent.parent / "state" / "geocode-cache.json"


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


def _ensure_geocode_cache_parent() -> None:
    GEOCODE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)


def load_geocode_cache() -> dict[str, list[float]]:
    if not GEOCODE_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(GEOCODE_CACHE_PATH.read_text())
    except Exception:
        return {}


def save_geocode_cache(cache: dict[str, list[float]]) -> None:
    _ensure_geocode_cache_parent()
    GEOCODE_CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def query_from_maps_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    vals = qs.get("query") or qs.get("q")
    if not vals:
        return None
    return vals[0].replace("+", " ").strip()


def geocode_query(query: str, cache: dict[str, list[float]]) -> tuple[float, float] | None:
    key = query.lower().strip()
    if key in cache:
        lat, lon = cache[key]
        return lat, lon

    with httpx.Client(timeout=20, follow_redirects=True) as client:
        res = client.get(
            NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 1},
            headers={
                "User-Agent": "hikeping/0.1 (route-map)",
                "Accept-Language": "en",
            },
        )
        res.raise_for_status()
        data = res.json()

    if not data:
        return None
    lat = float(data[0]["lat"])
    lon = float(data[0]["lon"])
    cache[key] = [lat, lon]
    return lat, lon


def route_points(start: tuple[float, float], end: tuple[float, float]) -> list[tuple[float, float]]:
    start_lat, start_lon = start
    end_lat, end_lon = end
    url = f"{OSRM_ROUTE_URL}/{start_lon},{start_lat};{end_lon},{end_lat}"

    with httpx.Client(timeout=20, follow_redirects=True) as client:
        res = client.get(url, params={"overview": "full", "geometries": "polyline"})
        res.raise_for_status()
        data = res.json()

    routes = data.get("routes") or []
    if not routes:
        raise RuntimeError("No route returned by OSRM")
    geometry = routes[0].get("geometry")
    if not geometry:
        raise RuntimeError("OSRM route missing geometry")
    return polyline.decode(geometry)


def render_route_png(
    title: str,
    start: tuple[float, float],
    end: tuple[float, float],
    points: list[tuple[float, float]],
    out_path: Path,
) -> None:
    m = StaticMap(1100, 700, url_template="https://tile.openstreetmap.org/{z}/{x}/{y}.png")
    line_points = [(lon, lat) for lat, lon in points]
    m.add_line(Line(line_points, "#2D7FF9", 4))
    m.add_marker(CircleMarker((start[1], start[0]), "#0B8F3D", 8))
    m.add_marker(CircleMarker((end[1], end[0]), "#D62828", 8))
    image = m.render(zoom=None)
    image.save(out_path)


def get_next_upcoming_hike_details(now: datetime) -> dict | None:
    js = fetch_events_js()
    today = now.date()
    events: list[dict] = []

    for block in re.finditer(r"\{\s*id:\s*\".*?\n\s*\},?", js, re.DOTALL):
        b = block.group(0)
        date_m = re.search(r'date:\s*"(?P<date>\d{4}-\d{2}-\d{2})"', b)
        title_m = re.search(r'title:\s*"(?P<title>(?:\\.|[^"])*)"', b)
        start_m = re.search(r'startGoogle:\s*"(?P<url>(?:\\.|[^"])*)"', b)
        end_m = re.search(r'endGoogle:\s*"(?P<url>(?:\\.|[^"])*)"', b)
        if not date_m or not title_m:
            continue
        events.append(
            {
                "date": date_m.group("date"),
                "title": _unescape_js_string(title_m.group("title")),
                "mapLinks": {
                    "startGoogle": _unescape_js_string(start_m.group("url")) if start_m else None,
                    "endGoogle": _unescape_js_string(end_m.group("url")) if end_m else None,
                },
            }
        )

    upcoming = [e for e in events if datetime.strptime(e["date"], "%Y-%m-%d").date() >= today]
    if not upcoming:
        return None
    upcoming.sort(key=lambda e: e["date"])
    return upcoming[0]


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


def post_discord_with_attachment(webhook_url: str, message: str, file_path: Path) -> bool:
    if not webhook_url:
        print("Webhook URL is not set", file=sys.stderr)
        return False

    with httpx.Client(timeout=30) as client:
        with file_path.open("rb") as f:
            files = {"files[0]": (file_path.name, f, "image/png")}
            data = {"payload_json": json.dumps({"content": message})}
            res = client.post(webhook_url, data=data, files=files)
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


def run_next_hike_with_map(post: bool = False, webhook_url: str | None = None) -> None:
    now = datetime.now(TIMEZONE)
    event = get_next_upcoming_hike_details(now)
    if not event:
        print("No upcoming hikes found.")
        return

    date = datetime.strptime(event["date"], "%Y-%m-%d").replace(tzinfo=TIMEZONE)
    title = _unescape_js_string(event["title"])
    pretty_date = date.strftime("%a, %b %d").replace(" 0", " ")

    map_links = event.get("mapLinks") or {}
    start_q = query_from_maps_url(map_links.get("startGoogle") or map_links.get("startApple"))
    end_q = query_from_maps_url(map_links.get("endGoogle") or map_links.get("endApple"))

    msg = f"🌳 Next St. John's Hike Club hike: {title} ({pretty_date})\n{HIKE_URL}"
    if not start_q or not end_q:
        print("Could not extract map start/end queries; posting text only.")
        print(msg)
        if post:
            target = webhook_url or DISCORD_WEBHOOK_URL
            if post_discord_to_webhook(target, msg):
                print("Posted text-only message to Discord.")
        return

    cache = load_geocode_cache()
    start = geocode_query(start_q, cache)
    end = geocode_query(end_q, cache)
    save_geocode_cache(cache)

    if not start or not end:
        print("Could not geocode route points; posting text only.")
        print(msg)
        if post:
            target = webhook_url or DISCORD_WEBHOOK_URL
            if post_discord_to_webhook(target, msg):
                print("Posted text-only message to Discord.")
        return

    points = route_points(start, end)

    with tempfile.TemporaryDirectory(prefix="hikeping-") as td:
        out = Path(td) / "next-hike-route.png"
        render_route_png(title, start, end, points, out)
        print(f"Generated map image: {out}")
        print(msg)
        if post:
            target = webhook_url or DISCORD_WEBHOOK_URL
            if post_discord_with_attachment(target, msg, out):
                print("Posted message + map image to Discord.")


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
        "--with-map",
        action="store_true",
        help="When used with --next, generate and attach a route PNG for the next hike",
    )
    parser.add_argument(
        "--webhook-url",
        default="",
        help="Optional webhook override (otherwise DISCORD_WEBHOOK_URL env var)",
    )
    args = parser.parse_args()

    if args.next:
        if args.with_map:
            run_next_hike_with_map(post=args.post, webhook_url=args.webhook_url or None)
        else:
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
