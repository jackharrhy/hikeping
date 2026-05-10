from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from hikeping import main


# Minimal HTML payload mimicking the SSR'd /upcoming-events page. The real page
# embeds the hikes array as part of a much larger TanStack Start dehydrated
# payload; for parsing purposes only the `hikes:$R[N]=[...]` literal matters.
SAMPLE_UPCOMING_HTML = (
    '<!doctype html><html><body><script>'
    'self.$_TSR.router=($R=>$R[0]={'
    'matches:[{loaderData:{'
    'hikes:$R[15]=['
    '$R[16]={'
    'id:"three-ponds-barrens-dog-day",'
    'name:"Three Ponds Barrens \\u2014 Dog Hike \\uD83D\\uDC15",'
    'dateLabel:"April 26, 2026 \\u00b7 12:00 p.m.",'
    'logisticsLine:"Three Ponds Barrens \\u00b7 Near St. John\'s \\u00b7 Variable \\u00b7 2-3 hrs",'
    'difficulty:"easy",difficultyLabel:"Easy-Moderate",'
    'description:"Bring the pups! A dog-friendly loop through the open barrens.",'
    'trailHeadUrl:"https://www.google.com/maps/search/?api=1&query=Three+Ponds+Barrens",'
    'trailEndUrl:"https://www.google.com/maps/search/?api=1&query=Three+Ponds+Barrens",'
    'allTrailsUrl:"https://www.alltrails.com/trail/canada/newfoundland-and-labrador/three-ponds-barrens",'
    'trailTypeLabel:"Loop",confirmedRegistrationCount:1,maxParticipants:null,'
    'registrationFull:!1,status:"open",eventType:"free"'
    '}'
    ']}}]}'
    '</script></body></html>'
)


def test_check_and_notify_posts_weekend_hike_details(monkeypatch):
    posted: list[tuple[str, dict]] = []

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 4, 24, 12, tzinfo=tz or ZoneInfo("America/St_Johns"))

    monkeypatch.setattr(main, "datetime", FixedDatetime)
    monkeypatch.setattr(main, "fetch_events_js", lambda: SAMPLE_UPCOMING_HTML)
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
            main.build_hike_components_payload(main.parse_events_js(SAMPLE_UPCOMING_HTML)[0]),
        )
    ]


def test_build_hike_components_payload_includes_richer_event_details():
    payload = main.build_hike_components_payload(main.parse_events_js(SAMPLE_UPCOMING_HTML)[0])

    assert payload["flags"] == 32768
    assert "content" not in payload
    assert "embeds" not in payload
    assert payload["components"][0]["type"] == 17
    assert payload["components"][0]["accent_color"] == 0x2F855A

    rendered = str(payload)
    assert "Three Ponds Barrens — Dog Hike 🐕" in rendered
    assert "Sun, Apr 26" in rendered
    assert "12:00 p.m." in rendered
    # logisticsLine is split into location/distance/duration on `·`
    assert "Three Ponds Barrens" in rendered
    assert "Easy-Moderate" in rendered
    assert "Variable" in rendered
    assert "2-3 hrs" in rendered
    assert "Bring the pups!" in rendered
    # /register URL is synthesised from the slug + eventType
    assert "/register?hike=three-ponds-barrens-dog-day" in rendered


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
    notified: list[str] = []

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 4, 24, 12, tzinfo=tz or ZoneInfo("America/St_Johns"))

    monkeypatch.setattr(main, "datetime", FixedDatetime)
    # An HTML page with no `hikes:$R[...]` array — simulates upstream change
    # or maintenance page.
    monkeypatch.setattr(main, "fetch_events_js", lambda: "<!doctype html><html><body></body></html>")
    # Stub notify_info itself so the test doesn't exercise the retry-loop HTTP
    # path (covered separately) and doesn't hit the network.
    monkeypatch.setattr(
        main,
        "notify_info",
        lambda message: notified.append(message) or True,
    )
    monkeypatch.setattr(main, "DISCORD_WEBHOOK_URL", "https://discord.example/hikes")
    monkeypatch.setattr(main, "HIKEPING_INFO_WEBHOOK_URL", "https://discord.example/info")

    main.check_and_notify()

    assert notified == [
        "⚠️ hikeping could not find a complete upcoming weekend hike on the upcoming-events page for 2026-04-25/2026-04-26. The St. John's Hike Club site may have changed: https://www.stjohnshikeclub.com/upcoming-events",
    ]


def test_check_and_notify_posts_weekend_hike_when_optional_details_are_missing(
    monkeypatch,
):
    posted: list[tuple[str, dict]] = []

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 4, 24, 12, tzinfo=tz or ZoneInfo("America/St_Johns"))

    minimal_html = (
        '<!doctype html><html><body><script>'
        'hikes:$R[1]=['
        '$R[2]={id:"three-ponds-barrens",name:"Three Ponds Barrens",'
        'dateLabel:"April 26, 2026 \\u00b7 12:00 p.m."}'
        ']'
        '</script></body></html>'
    )

    monkeypatch.setattr(main, "datetime", FixedDatetime)
    monkeypatch.setattr(main, "fetch_events_js", lambda: minimal_html)
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
            main.build_hike_components_payload(main.parse_events_js(minimal_html)[0]),
        )
    ]


def test_run_next_hike_posts_same_detailed_message(monkeypatch):
    posted: list[tuple[str, dict]] = []

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 4, 24, 12, tzinfo=tz or ZoneInfo("America/St_Johns"))

    monkeypatch.setattr(main, "datetime", FixedDatetime)
    monkeypatch.setattr(main, "fetch_events_js", lambda: SAMPLE_UPCOMING_HTML)
    monkeypatch.setattr(
        main,
        "post_discord_payload",
        lambda webhook_url, payload: posted.append((webhook_url, payload)) or True,
    )

    main.run_next_hike(post=True, webhook_url="https://discord.example/hikes")

    assert posted == [
        (
            "https://discord.example/hikes",
            main.build_hike_components_payload(main.parse_events_js(SAMPLE_UPCOMING_HTML)[0]),
        )
    ]


class _FakeResponse:
    """Minimal stand-in for httpx.Response covering only what notify_info uses."""

    def __init__(self, status_code: int, headers: dict | None = None, body: dict | None = None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _install_fake_httpx(monkeypatch, responses: list):
    """Patch main.httpx.Client so each .post() returns the next response in `responses`."""

    posts: list[tuple[str, dict]] = []
    queue = list(responses)

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json):
            posts.append((url, json))
            if not queue:
                raise AssertionError("FakeClient.post called more times than expected")
            result = queue.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(main.httpx, "Client", FakeClient)
    return posts


def test_notify_info_retries_on_429_and_eventually_succeeds(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(main.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(main, "HIKEPING_INFO_WEBHOOK_URL", "https://discord.example/info")

    posts = _install_fake_httpx(
        monkeypatch,
        [
            _FakeResponse(429, headers={"retry-after": "1"}, body={"retry_after": 0.5}),
            _FakeResponse(204),
        ],
    )

    assert main.notify_info("hello") is True
    assert len(posts) == 2
    # JSON body's retry_after (0.5s) takes precedence over the header.
    assert sleeps == [0.5]


def test_notify_info_retries_on_5xx_then_gives_up(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(main.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(main, "HIKEPING_INFO_WEBHOOK_URL", "https://discord.example/info")
    monkeypatch.setattr(main, "INFO_WEBHOOK_MAX_ATTEMPTS", 3)

    posts = _install_fake_httpx(
        monkeypatch,
        [
            _FakeResponse(503),
            _FakeResponse(503),
            _FakeResponse(503),
        ],
    )

    assert main.notify_info("alarm") is False
    assert len(posts) == 3
    # Two sleeps (between attempts 1->2 and 2->3); no sleep after the last attempt.
    assert len(sleeps) == 2


def test_notify_info_does_not_retry_on_4xx_other_than_429(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(main.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(main, "HIKEPING_INFO_WEBHOOK_URL", "https://discord.example/info")

    posts = _install_fake_httpx(
        monkeypatch,
        [_FakeResponse(404)],
    )

    assert main.notify_info("alarm") is False
    assert len(posts) == 1
    assert sleeps == []


def test_notify_info_returns_false_when_webhook_unset(monkeypatch):
    monkeypatch.setattr(main, "HIKEPING_INFO_WEBHOOK_URL", "")
    # Should not even try to POST.
    monkeypatch.setattr(main.httpx, "Client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not POST")))
    assert main.notify_info("alarm") is False
