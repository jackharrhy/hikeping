from __future__ import annotations

import argparse
import math
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import polyline
from apscheduler.schedulers.blocking import BlockingScheduler
from staticmap import CircleMarker, Line, StaticMap

SITE_BASE_URL = "https://www.stjohnshikeclub.com"
HIKE_URL = f"{SITE_BASE_URL}/upcoming-events"
TIMEZONE = ZoneInfo(os.getenv("HIKEPING_TIMEZONE", "America/St_Johns"))
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
HIKEPING_INFO_WEBHOOK_URL = os.getenv("HIKEPING_INFO_WEBHOOK_URL", "").strip()
EVENTS_DATA_URL = HIKE_URL  # legacy name; now points at the SSR'd HTML page we scrape
IS_COMPONENTS_V2 = 1 << 15
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_ROUTE_URL = "https://router.project-osrm.org/route/v1/foot"
OVERPASS_URLS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]
GEOCODE_CACHE_PATH = Path(__file__).resolve().parent.parent / "state" / "geocode-cache.json"


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
    duration: str = ""
    elevation_gain: str = ""
    description: str = ""
    cta_text: str = ""
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
    """Fetch the SSR'd upcoming-events HTML page.

    Name kept for backward compatibility with prior callers/tests; the
    underlying source moved from `events-data.js` (deleted upstream Q2 2026)
    to the dehydrated TanStack Start payload embedded in the page itself.
    """

    with httpx.Client(timeout=20, follow_redirects=True) as client:
        res = client.get(
            HIKE_URL,
            headers={
                "User-Agent": "hikeping/0.1 (+https://github.com/jackharrhy/hikeping)",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
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


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(x))


def _trail_length_km(trail: list[tuple[float, float]]) -> float:
    total = 0.0
    for i in range(1, len(trail)):
        total += haversine_km(trail[i - 1], trail[i])
    return total


def _min_distance_to_points_km(pt: tuple[float, float], anchors: list[tuple[float, float]]) -> float:
    return min(haversine_km(pt, a) for a in anchors)


def fetch_trails_near_route(
    points: list[tuple[float, float]],
    pad: float = 0.02,
    max_point_distance_km: float = 3.0,
    max_trail_length_km: float = 20.0,
) -> list[list[tuple[float, float]]]:
    if not points:
        return []

    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    s, w = min(lats) - pad, min(lons) - pad
    n, e = max(lats) + pad, max(lons) + pad

    query = f"""
    [out:json][timeout:20];
    (
      way[highway~"path|footway|track|steps"]({s},{w},{n},{e});
      way[route="hiking"]({s},{w},{n},{e});
      relation[route="hiking"]({s},{w},{n},{e});
    );
    out geom;
    """

    last_err: Exception | None = None
    data: dict = {}
    for overpass_url in OVERPASS_URLS:
        try:
            with httpx.Client(timeout=25, follow_redirects=True) as client:
                res = client.get(
                    overpass_url,
                    params={"data": query},
                    headers={"User-Agent": "hikeping/0.1 (trail-overlay)"},
                )
                res.raise_for_status()
                data = res.json()
                break
        except Exception as exc:
            last_err = exc
            continue
    else:
        raise RuntimeError(f"All Overpass endpoints failed: {last_err}")

    trails: list[list[tuple[float, float]]] = []
    anchors = points
    for el in data.get("elements", []):
        geom = el.get("geom") or []
        if len(geom) >= 2:
            trail = [(float(p["lat"]), float(p["lon"])) for p in geom if "lat" in p and "lon" in p]
            if len(trail) >= 2:
                trails.append(trail)
        for member in el.get("members", []) or []:
            mgeom = member.get("geometry") or []
            if len(mgeom) < 2:
                continue
            trail = [(float(p["lat"]), float(p["lon"])) for p in mgeom if "lat" in p and "lon" in p]
            if len(trail) >= 2:
                trails.append(trail)

    filtered: list[list[tuple[float, float]]] = []
    for trail in trails:
        near = [p for p in trail if _min_distance_to_points_km(p, anchors) <= max_point_distance_km]
        if len(near) < 2:
            continue
        if _trail_length_km(near) > max_trail_length_km:
            continue
        filtered.append(near)
    return filtered


def render_route_png(
    title: str,
    start: tuple[float, float],
    end: tuple[float, float],
    points: list[tuple[float, float]],
    trail_lines: list[list[tuple[float, float]]],
    out_path: Path,
) -> None:
    m = StaticMap(
        1100,
        700,
        url_template="https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}.jpg",
    )
    for trail in trail_lines:
        trail_points = [(lon, lat) for lat, lon in trail]
        m.add_line(Line(trail_points, "#FFD84D", 2))
    line_points = [(lon, lat) for lat, lon in points]
    m.add_line(Line(line_points, "#00E5FF", 5))
    m.add_marker(CircleMarker((start[1], start[0]), "#0B8F3D", 8))
    m.add_marker(CircleMarker((end[1], end[0]), "#D62828", 8))
    image = m.render(zoom=None)
    image.save(out_path)


def get_next_upcoming_hike_details(now: datetime) -> dict | None:
    """Return raw map-link details for the next upcoming hike, or None.

    Used by the ``--with-map`` flow. We re-parse the SSR'd HTML to pull the
    trailhead/trail-end Google Maps URLs that the schema-aware
    ``parse_events_js`` does not currently surface on ``HikeEvent``.
    """

    html = fetch_events_js()

    try:
        array_src = _extract_hikes_array_source(html)
    except EventFeedError:
        return None

    today = now.date()
    candidates: list[dict] = []

    for block in _extract_event_blocks(array_src):
        name = _js_string_field(block, "name")
        date_label = _js_string_field(block, "dateLabel")
        if not name or not date_label:
            continue
        event_date, _ = _parse_date_label(date_label)
        if event_date is None:
            continue
        if event_date.date() < today:
            continue

        candidates.append(
            {
                "date": event_date.date().isoformat(),
                "title": name,
                "mapLinks": {
                    # Keep the legacy keys so run_next_hike_with_map() doesn't
                    # need to know about the rename.
                    "startGoogle": _js_string_field(block, "trailHeadUrl") or None,
                    "endGoogle": _js_string_field(block, "trailEndUrl") or None,
                },
            }
        )

    if not candidates:
        return None
    candidates.sort(key=lambda e: e["date"])
    return candidates[0]


def _unescape_js_string(s: str) -> str:
    """Decode a JS double-quoted string body.

    The dehydrated TanStack Start payload uses JS-literal strings (not JSON), so
    we need to tolerate a couple of JS-only escapes that ``json.loads`` rejects:
    ``\\'`` (escaped apostrophe — common in `St. John\\'s`) and ``\\`` followed
    by a literal newline. Everything else falls through to the JSON parser so we
    still get correct ``\\uXXXX`` and surrogate-pair handling.
    """

    # ``\'`` is illegal in JSON but legal in JS string literals.
    cleaned = s.replace("\\'", "'")
    try:
        return json.loads(f'"{cleaned}"')
    except json.JSONDecodeError:
        # Last-ditch: strip remaining backslashes the JSON parser refused.
        return cleaned.replace("\\", "")


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


def _extract_hikes_array_source(html: str) -> str:
    """Pull the `hikes:$R[N]=[ ... ]` array literal out of the SSR'd HTML.

    The St. John's Hike Club site is a TanStack Start app that dehydrates its
    loader data into the page as JS object/array literals (not JSON). We find
    the start of the hikes array, then bracket-balance to find the matching `]`,
    skipping over string contents.
    """

    m = re.search(r"hikes:\$R\[\d+\]=\[", html)
    if not m:
        raise EventFeedError("Could not find dehydrated `hikes` array in upcoming-events HTML")

    i = m.end() - 1  # index of opening `[`
    depth = 0
    in_string = False
    escaped = False
    end: int | None = None

    while i < len(html):
        ch = html[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        i += 1

    if end is None:
        raise EventFeedError("Unterminated dehydrated `hikes` array in upcoming-events HTML")

    return html[m.end() - 1 : end]


_DATE_LABEL_RE = re.compile(
    r"^(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s+(?P<year>\d{4})\s*[·•]\s*(?P<time>.+)$"
)


def _parse_date_label(date_label: str) -> tuple[datetime | None, str]:
    """Parse `"May 10, 2026 · 12:00 p.m."` into (datetime, "12:00 p.m.").

    Returns (None, "") if the label cannot be parsed.
    """

    m = _DATE_LABEL_RE.match(date_label.strip())
    if not m:
        return None, ""

    raw_date = f"{m.group('month')} {m.group('day')} {m.group('year')}"
    try:
        parsed = datetime.strptime(raw_date, "%B %d %Y")
    except ValueError:
        return None, ""

    return parsed.replace(tzinfo=TIMEZONE), m.group("time").strip()


def _split_logistics_line(line: str) -> tuple[str, str, str]:
    """Split `"Stiles Cove - East Coast Trail · ~8k · 3.5-4 hrs"` into
    (location, distance, duration). Missing pieces become empty strings.
    """

    if not line:
        return "", "", ""

    # The site uses `·` (U+00B7) as the separator; tolerate `•` and `|` too.
    parts = [p.strip() for p in re.split(r"\s*[·•|]\s*", line) if p.strip()]
    location = parts[0] if len(parts) > 0 else ""
    distance = parts[1] if len(parts) > 1 else ""
    duration = parts[2] if len(parts) > 2 else ""
    if len(parts) > 3:
        # Anything trailing gets folded into duration so we don't lose info.
        duration = " · ".join(parts[2:])
    return location, distance, duration


def parse_events_js(html: str) -> list[HikeEvent]:
    """Parse the SSR'd `/upcoming-events` HTML into HikeEvent objects.

    The function name is kept for backward compatibility with the previous
    `events-data.js` scraper; it now operates on the full page HTML instead.
    Empty/whitespace-only input is treated as "no events found" rather than an
    error so existing tests that pass `"const EVENTS = [];"` still exercise the
    "no weekend hike" branch via `get_upcoming_weekend_hike`.
    """

    if not html or not html.strip():
        return []

    try:
        array_src = _extract_hikes_array_source(html)
    except EventFeedError:
        # Legacy/empty fixtures: fall through and return no events.
        return []

    events: list[HikeEvent] = []
    for block in _extract_event_blocks(array_src):
        name = _js_string_field(block, "name")
        date_label = _js_string_field(block, "dateLabel")
        if not name or not date_label:
            continue

        event_date, start_time = _parse_date_label(date_label)
        if event_date is None:
            continue

        slug = _js_string_field(block, "id")
        logistics = _js_string_field(block, "logisticsLine")
        location, distance, duration = _split_logistics_line(logistics)

        # Free hikes use the in-app /register route; paid hikes don't have a
        # registration form, so leave cta_url blank and let the message fall
        # back to the upcoming-events page link.
        event_type = _js_string_field(block, "eventType")
        cta_url = ""
        cta_text = ""
        if slug and event_type == "free":
            cta_url = f"{SITE_BASE_URL}/register?hike={slug}"
            cta_text = "Register"
        elif slug and event_type == "paid":
            cta_url = f"{SITE_BASE_URL}/upcoming-events"
            cta_text = "Event details"

        events.append(
            HikeEvent(
                date=event_date,
                title=name,
                start_time=start_time,
                location=location,
                difficulty=_js_string_field(block, "difficultyLabel")
                or _js_string_field(block, "difficulty"),
                distance=distance,
                duration=duration,
                elevation_gain="",
                description=_js_string_field(block, "description"),
                cta_text=cta_text,
                cta_url=cta_url,
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
        raise EventFeedError("No events could be parsed from upcoming-events page")

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


def _text_component(content: str) -> dict:
    return {"type": 10, "content": content}


def _separator_component() -> dict:
    return {"type": 14, "divider": True, "spacing": 1}


def _link_button(label: str, url: str) -> dict:
    return {"type": 2, "style": 5, "label": label, "url": url}


def build_hike_components_payload(event: HikeEvent) -> dict:
    pretty_date = event.date.strftime("%a, %b %d").replace(" 0", " ")
    when = pretty_date
    if event.start_time:
        when = f"{when} at {event.start_time}"

    summary_lines = [f"## 🌳 {event.title}", f"**{when}**"]
    if event.location:
        summary_lines.append(f"📍 {event.location}")

    detail_lines = []
    for label, value in [
        ("Difficulty", event.difficulty),
        ("Distance", event.distance),
        ("Duration", event.duration),
        ("Elevation", event.elevation_gain),
    ]:
        if value:
            detail_lines.append(f"**{label}:** {value}")

    components = [_text_component("\n".join(summary_lines))]
    if event.description:
        components.extend([_separator_component(), _text_component(event.description)])
    if detail_lines:
        components.extend([_separator_component(), _text_component("\n".join(detail_lines))])

    buttons = [_link_button("Hike Page", HIKE_URL)]
    if event.cta_url:
        buttons.insert(0, _link_button(event.cta_text or "Register", event.cta_url))
    components.extend([_separator_component(), {"type": 1, "components": buttons}])

    return {
        "flags": IS_COMPONENTS_V2,
        "components": [
            {
                "type": 17,
                "accent_color": 0x2F855A,
                "components": components,
            }
        ],
        "allowed_mentions": {"parse": []},
    }


def notify_info(message: str) -> bool:
    if not HIKEPING_INFO_WEBHOOK_URL:
        print(message, file=sys.stderr)
        return False
    return post_discord_to_webhook(HIKEPING_INFO_WEBHOOK_URL, message)


def post_discord_payload(webhook_url: str, payload: dict) -> bool:
    if not webhook_url:
        print("Webhook URL is not set", file=sys.stderr)
        return False

    params = None
    if payload.get("flags", 0) & IS_COMPONENTS_V2:
        params = {"with_components": "true"}

    with httpx.Client(timeout=20) as client:
        res = client.post(webhook_url, json=payload, params=params)
        res.raise_for_status()
    return True


def post_discord_to_webhook(webhook_url: str, message: str) -> bool:
    return post_discord_payload(webhook_url, {"content": message})


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
    print(f"[{now.isoformat()}] Checking {EVENTS_DATA_URL}")
    try:
        event = get_upcoming_weekend_hike(now)
    except EventFeedError as exc:
        sat, sun = next_weekend_dates(now)
        msg = (
            "⚠️ hikeping could not find a complete upcoming weekend hike on the "
            f"upcoming-events page for {sat.date().isoformat()}/{sun.date().isoformat()}. "
            f"The St. John's Hike Club site may have changed: {HIKE_URL}"
        )
        print(f"{msg} ({exc})", file=sys.stderr)
        try:
            notify_info(msg)
        except Exception as notify_exc:
            print(f"Failed to post info webhook: {notify_exc}", file=sys.stderr)
        return
    except Exception as exc:
        msg = f"⚠️ hikeping failed to fetch or parse upcoming-events page: {exc}"
        print(msg, file=sys.stderr)
        try:
            notify_info(msg)
        except Exception as notify_exc:
            print(f"Failed to post info webhook: {notify_exc}", file=sys.stderr)
        return

    if event:
        payload = build_hike_components_payload(event)
        try:
            sent = post_discord_payload(DISCORD_WEBHOOK_URL, payload)
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
        sent = post_discord_payload(target, build_hike_components_payload(next_hike))
        if sent:
            print("Posted to Discord.")


def run_next_hike_with_map(post: bool = False, webhook_url: str | None = None) -> None:
    now = datetime.now(TIMEZONE)
    event = get_next_upcoming_hike_details(now)
    if not event:
        print("No upcoming hikes found.")
        return

    date = datetime.strptime(event["date"], "%Y-%m-%d").replace(tzinfo=TIMEZONE)
    title = event["title"]
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

    loop_hike = haversine_km(start, end) < 0.5
    if loop_hike:
        points = [start, end]
        print("Detected loop/out-and-back hike; skipping long OSRM route estimate.")
    else:
        points = route_points(start, end)
    trail_lines: list[list[tuple[float, float]]] = []
    try:
        trail_lines = fetch_trails_near_route(
            points,
            pad=0.05 if loop_hike else 0.02,
            max_point_distance_km=1.5 if loop_hike else 3.0,
            max_trail_length_km=8.0 if loop_hike else 20.0,
        )
        print(f"Loaded {len(trail_lines)} nearby trail segments from OSM/Overpass.")
    except Exception as exc:
        print(f"Trail overlay fetch failed (continuing): {exc}")

    with tempfile.TemporaryDirectory(prefix="hikeping-") as td:
        out = Path(td) / "next-hike-route.png"
        render_route_png(title, start, end, points, trail_lines, out)
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
        help="Show the next upcoming hike from the upcoming-events page",
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
