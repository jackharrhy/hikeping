from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from hikeping import main


SAMPLE_EVENTS_JS = r'''
const EVENTS = [
  {
    id: "three-ponds-barrens-dog-hike-apr",
    title: "Three Ponds Barrens — Dog Hike \uD83D\uDC15",
    date: "2026-04-26",
    month: "Apr",
    day: 26,
    dow: "Sun",
    type: "free",
    price: null,
    difficulty: "Easy–Moderate",
    distance: "Variable",
    duration: "2–3 hrs",
    elevationGain: "Minimal",
    startTime: "12:00 PM",
    location: "Three Ponds Barrens · Near St. John’s",
    description: "Bring the pups! A dog-friendly loop through the open barrens.",
    ctaText: "Register Now",
    ctaUrl: "https://tally.so/r/68OlLO",
    mapLinks: {
      startName: "Three Ponds Barrens Trailhead"
    }
  }
];
'''


def test_check_and_notify_posts_weekend_hike_details(monkeypatch):
    posted: list[tuple[str, dict]] = []

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 4, 24, 12, tzinfo=tz or ZoneInfo("America/St_Johns"))

    monkeypatch.setattr(main, "datetime", FixedDatetime)
    monkeypatch.setattr(main, "fetch_events_js", lambda: SAMPLE_EVENTS_JS)
    monkeypatch.setattr(
        main,
        "post_discord_payload",
        lambda webhook_url, payload: posted.append((webhook_url, payload)) or True,
    )
    monkeypatch.setattr(main, "DISCORD_WEBHOOK_URL", "https://discord.example/hikes")
    monkeypatch.setattr(main, "HIKEPING_INFO_WEBHOOK_URL", "")

    main.check_and_notify()

    assert posted == [
        (
            "https://discord.example/hikes",
            main.build_hike_components_payload(main.parse_events_js(SAMPLE_EVENTS_JS)[0]),
        )
    ]


def test_build_hike_components_payload_includes_richer_event_details():
    payload = main.build_hike_components_payload(main.parse_events_js(SAMPLE_EVENTS_JS)[0])

    assert payload["flags"] == 32768
    assert "content" not in payload
    assert "embeds" not in payload
    assert payload["components"][0]["type"] == 17
    assert payload["components"][0]["accent_color"] == 0x2F855A

    rendered = str(payload)
    assert "Three Ponds Barrens — Dog Hike 🐕" in rendered
    assert "Sun, Apr 26" in rendered
    assert "12:00 PM" in rendered
    assert "Three Ponds Barrens · Near St. John’s" in rendered
    assert "Easy–Moderate" in rendered
    assert "Variable" in rendered
    assert "2–3 hrs" in rendered
    assert "Minimal" in rendered
    assert "Bring the pups!" in rendered
    assert "https://tally.so/r/68OlLO" in rendered


def test_post_discord_payload_enables_components_v2(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, webhook_url, json, params=None):
            calls.append((webhook_url, json, params))
            return FakeResponse()

    monkeypatch.setattr(main.httpx, "Client", FakeClient)

    payload = {"flags": 32768, "components": [{"type": 10, "content": "test"}]}
    assert main.post_discord_payload("https://discord.example/hook", payload)

    assert calls == [
        (
            "https://discord.example/hook",
            payload,
            {"with_components": "true"},
        )
    ]


def test_check_and_notify_sends_info_webhook_when_event_feed_breaks(monkeypatch):
    posted: list[tuple[str, str]] = []

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 4, 24, 12, tzinfo=tz or ZoneInfo("America/St_Johns"))

    monkeypatch.setattr(main, "datetime", FixedDatetime)
    monkeypatch.setattr(main, "fetch_events_js", lambda: "const EVENTS = [];")
    monkeypatch.setattr(
        main,
        "post_discord_to_webhook",
        lambda webhook_url, message: posted.append((webhook_url, message)) or True,
    )
    monkeypatch.setattr(main, "DISCORD_WEBHOOK_URL", "https://discord.example/hikes")
    monkeypatch.setattr(main, "HIKEPING_INFO_WEBHOOK_URL", "https://discord.example/info")

    main.check_and_notify()

    assert posted == [
        (
            "https://discord.example/info",
            "⚠️ hikeping could not find a complete upcoming weekend hike in events-data.js for 2026-04-25/2026-04-26. The St. John's Hike Club site may have changed: https://www.stjohnshikeclub.com/upcoming-hike.html",
        )
    ]


def test_check_and_notify_posts_weekend_hike_when_optional_details_are_missing(
    monkeypatch,
):
    posted: list[tuple[str, dict]] = []

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 4, 24, 12, tzinfo=tz or ZoneInfo("America/St_Johns"))

    events_js = r'''
const EVENTS = [
  {
    title: "Three Ponds Barrens",
    date: "2026-04-26"
  }
];
'''

    monkeypatch.setattr(main, "datetime", FixedDatetime)
    monkeypatch.setattr(main, "fetch_events_js", lambda: events_js)
    monkeypatch.setattr(
        main,
        "post_discord_payload",
        lambda webhook_url, payload: posted.append((webhook_url, payload)) or True,
    )
    monkeypatch.setattr(main, "DISCORD_WEBHOOK_URL", "https://discord.example/hikes")
    monkeypatch.setattr(main, "HIKEPING_INFO_WEBHOOK_URL", "https://discord.example/info")

    main.check_and_notify()

    assert posted == [
        (
            "https://discord.example/hikes",
            main.build_hike_components_payload(main.parse_events_js(events_js)[0]),
        )
    ]


def test_run_next_hike_posts_same_detailed_message(monkeypatch):
    posted: list[tuple[str, dict]] = []

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 4, 24, 12, tzinfo=tz or ZoneInfo("America/St_Johns"))

    monkeypatch.setattr(main, "datetime", FixedDatetime)
    monkeypatch.setattr(main, "fetch_events_js", lambda: SAMPLE_EVENTS_JS)
    monkeypatch.setattr(
        main,
        "post_discord_payload",
        lambda webhook_url, payload: posted.append((webhook_url, payload)) or True,
    )

    main.run_next_hike(post=True, webhook_url="https://discord.example/hikes")

    assert posted == [
        (
            "https://discord.example/hikes",
            main.build_hike_components_payload(main.parse_events_js(SAMPLE_EVENTS_JS)[0]),
        )
    ]
