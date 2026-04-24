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
    posted: list[tuple[str, str]] = []

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 4, 24, 12, tzinfo=tz or ZoneInfo("America/St_Johns"))

    monkeypatch.setattr(main, "datetime", FixedDatetime)
    monkeypatch.setattr(main, "fetch_events_js", lambda: SAMPLE_EVENTS_JS)
    monkeypatch.setattr(
        main,
        "post_discord_to_webhook",
        lambda webhook_url, message: posted.append((webhook_url, message)) or True,
    )
    monkeypatch.setattr(main, "DISCORD_WEBHOOK_URL", "https://discord.example/hikes")
    monkeypatch.setattr(main, "HIKEPING_INFO_WEBHOOK_URL", "")

    main.check_and_notify()

    assert posted == [
        (
            "https://discord.example/hikes",
            "🌳 Next St. John's Hike Club hike: Three Ponds Barrens — Dog Hike 🐕 (Sun, Apr 26)\n"
            "Time: 12:00 PM\n"
            "Location: Three Ponds Barrens · Near St. John’s\n"
            "Difficulty: Easy–Moderate\n"
            "Distance: Variable\n"
            "Register: https://tally.so/r/68OlLO\n"
            "https://www.stjohnshikeclub.com/upcoming-hike.html",
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
    posted: list[tuple[str, str]] = []

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
        "post_discord_to_webhook",
        lambda webhook_url, message: posted.append((webhook_url, message)) or True,
    )
    monkeypatch.setattr(main, "DISCORD_WEBHOOK_URL", "https://discord.example/hikes")
    monkeypatch.setattr(main, "HIKEPING_INFO_WEBHOOK_URL", "https://discord.example/info")

    main.check_and_notify()

    assert posted == [
        (
            "https://discord.example/hikes",
            "🌳 Next St. John's Hike Club hike: Three Ponds Barrens (Sun, Apr 26)\n"
            "https://www.stjohnshikeclub.com/upcoming-hike.html",
        )
    ]


def test_run_next_hike_posts_same_detailed_message(monkeypatch):
    posted: list[tuple[str, str]] = []

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 4, 24, 12, tzinfo=tz or ZoneInfo("America/St_Johns"))

    monkeypatch.setattr(main, "datetime", FixedDatetime)
    monkeypatch.setattr(main, "fetch_events_js", lambda: SAMPLE_EVENTS_JS)
    monkeypatch.setattr(
        main,
        "post_discord_to_webhook",
        lambda webhook_url, message: posted.append((webhook_url, message)) or True,
    )

    main.run_next_hike(post=True, webhook_url="https://discord.example/hikes")

    assert posted == [
        (
            "https://discord.example/hikes",
            "🌳 Next St. John's Hike Club hike: Three Ponds Barrens — Dog Hike 🐕 (Sun, Apr 26)\n"
            "Time: 12:00 PM\n"
            "Location: Three Ponds Barrens · Near St. John’s\n"
            "Difficulty: Easy–Moderate\n"
            "Distance: Variable\n"
            "Register: https://tally.so/r/68OlLO\n"
            "https://www.stjohnshikeclub.com/upcoming-hike.html",
        )
    ]
