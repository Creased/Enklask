import pytest

import app.sources.leboncoin as leboncoin
from app.enums import Source
from app.sources.base import SearchQuery
from app.sources.leboncoin import DataDomeBlocked, LeboncoinSource

AD = {
    "list_id": 2890001,
    "subject": "Nintendo Switch carte mère HS",
    "body": "Carte mère en panne, envoi Mondial Relay possible",
    "url": "https://www.leboncoin.fr/jeux_video/2890001.htm",
    "price": [40],
    "images": {
        "thumb_url": "https://img.leboncoin.fr/thumb.jpg",
        "urls": [
            "https://img.leboncoin.fr/1.jpg",
            "https://img.leboncoin.fr/2.jpg",
        ],
    },
    "location": {"city": "Rennes", "lat": 48.11, "lng": -1.68, "zipcode": "35000"},
    "first_publication_date": "2026-06-14 09:15:00",
}


def test_leboncoin_to_raw():
    raw = LeboncoinSource()._to_raw(AD)
    assert raw.source is Source.LEBONCOIN
    assert raw.source_id == "2890001"
    assert raw.price == 40.0
    assert raw.thumbnail == "https://img.leboncoin.fr/thumb.jpg"
    assert len(raw.photos) == 2
    assert raw.location_city == "Rennes"
    assert raw.lat == 48.11 and raw.lon == -1.68
    assert "mondial_relay" in raw.shipping_options
    assert raw.posted_at is not None and raw.posted_at.year == 2026


def test_leboncoin_handles_missing_images():
    raw = LeboncoinSource()._to_raw(
        {"list_id": 1, "subject": "x", "url": "u", "price": []}
    )
    assert raw.thumbnail is None
    assert raw.photos == []
    assert raw.price is None


def test_build_body_maps_filters_to_native_params():
    body = LeboncoinSource()._build_body(
        SearchQuery(
            query="switch",
            sort="price_asc",
            price_min=10,
            price_max=200,
            condition="for_parts",
        )
    )
    assert body["sort_by"] == "price"
    assert body["sort_order"] == "asc"
    assert body["filters"]["keywords"]["text"] == "switch"
    assert body["filters"]["ranges"]["price"] == {"min": 10, "max": 200}
    assert body["filters"]["enums"]["item_condition"] == ["5"]


def test_build_body_defaults_to_newest_no_filters():
    body = LeboncoinSource()._build_body(SearchQuery(query="velo"))
    assert (body["sort_by"], body["sort_order"]) == ("time", "desc")
    assert "ranges" not in body["filters"]
    assert "item_condition" not in body["filters"]["enums"]
    assert body["listing_source"] == "direct-search"


class _Response:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class _Cookies:
    def __init__(self):
        self.values = {}

    def set(self, name, value, domain=None):
        self.values[(domain, name)] = value

    def get(self, name):
        for (_domain, cookie_name), value in self.values.items():
            if cookie_name == name:
                return value
        return None


class _Session:
    def __init__(self, post_responses, get_status=200):
        self.headers = {}
        self.cookies = _Cookies()
        self.post_responses = list(post_responses)
        self.get_calls = 0
        self.post_calls = 0
        self.closed = False
        self.get_status = get_status

    def get(self, *_args, **_kwargs):
        self.get_calls += 1
        if self.get_status == 200:
            self.cookies.set("datadome", "warm-cookie", domain=".leboncoin.fr")
        return _Response(self.get_status)

    def post(self, *_args, **_kwargs):
        self.post_calls += 1
        return self.post_responses.pop(0)

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_leboncoin_transport(monkeypatch):
    monkeypatch.setattr(leboncoin, "_cached_session", None)
    monkeypatch.setattr(leboncoin, "_cached_profile", None)
    monkeypatch.setattr(leboncoin, "_blocked_until", 0.0)
    monkeypatch.setattr(leboncoin, "_browser_preferred_until", 0.0)


def test_datadome_challenge_requires_status_and_marker():
    assert leboncoin._is_datadome_challenge(
        _Response(403, headers={"X-DD-B": "3"})
    )
    assert leboncoin._is_datadome_challenge(
        _Response(401, headers={"X-SF-CC-X-dd-b": "1"})
    )
    assert not leboncoin._is_datadome_challenge(_Response(403))
    assert not leboncoin._is_datadome_challenge(
        _Response(500, headers={"X-DD-B": "3"})
    )


def test_successful_transport_session_is_reused(monkeypatch):
    session = _Session([_Response(200, {"ads": [1]}), _Response(200, {"ads": [2]})])
    sessions = []

    def make_session(*, impersonate):
        sessions.append((impersonate, session))
        return session

    monkeypatch.setattr(leboncoin.cffi_requests, "Session", make_session)
    source = LeboncoinSource()
    monkeypatch.setattr(source, "_datadome_cookie", lambda: "seed-cookie")

    assert source._post_curl({}) == {"ads": [1]}
    assert source._post_curl({}) == {"ads": [2]}
    assert len(sessions) == 1
    assert session.get_calls == 1
    assert session.post_calls == 2
    assert not session.closed
    assert "User-Agent" not in session.headers


def test_warmup_challenge_short_circuits_to_browser(monkeypatch):
    """A DataDome 403 on the warm-up GET must escalate to the browser, not
    raise challenge=False (the headline bug the patch had)."""
    from app.sources import leboncoin_browser

    sessions = [_Session([], get_status=403)]
    monkeypatch.setattr(leboncoin.cffi_requests, "Session", lambda **_k: sessions.pop(0))
    monkeypatch.setattr(leboncoin.time, "monotonic", lambda: 100.0)
    calls = []
    monkeypatch.setattr(
        leboncoin_browser,
        "fetch_leboncoin_json",
        lambda base, profile, body, key, proxy=None: calls.append((base, proxy))
        or {"ads": [7]},
    )
    source = LeboncoinSource()
    monkeypatch.setattr(source, "_datadome_cookie", lambda: None)

    assert source._post({"filters": {}}) == {"ads": [7]}
    assert calls and calls[0][0] == "https://www.leboncoin.fr"
    assert leboncoin._blocked_until == 100.0 + leboncoin._BLOCK_COOLDOWN_SECONDS
    assert leboncoin._browser_preferred_until == 100.0 + leboncoin._BROWSER_PREFER_SECONDS
    # Only one curl identity is tried before the browser (no hammering).
    assert sessions == []


def test_cooldown_uses_browser_without_touching_curl(monkeypatch):
    """During the curl cooldown, no Leboncoin network request is made; the
    search goes straight to the browser."""
    from app.sources import leboncoin_browser

    monkeypatch.setattr(leboncoin.time, "monotonic", lambda: 50.0)
    monkeypatch.setattr(leboncoin, "_blocked_until", 100.0)  # cooldown in the future

    def _no_curl(**_k):
        raise AssertionError("curl must not run during cooldown")

    monkeypatch.setattr(leboncoin.cffi_requests, "Session", _no_curl)
    called = []
    monkeypatch.setattr(
        leboncoin_browser,
        "fetch_leboncoin_json",
        lambda *a, **k: called.append(1) or {"ads": []},
    )
    source = LeboncoinSource()
    monkeypatch.setattr(source, "_datadome_cookie", lambda: None)

    assert source._post({}) == {"ads": []}
    assert called


def test_200_html_body_is_not_cached_and_escalates(monkeypatch):
    """A 200 whose body is not JSON (DataDome soft challenge) must not poison
    the cache and must route to the browser."""
    from app.sources import leboncoin_browser

    class _HtmlResp:
        status_code = 200
        headers: dict = {}

        def json(self):
            raise ValueError("not json")

    session = _Session([_HtmlResp()])
    monkeypatch.setattr(leboncoin.cffi_requests, "Session", lambda **_k: session)
    monkeypatch.setattr(leboncoin.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(
        leboncoin_browser,
        "fetch_leboncoin_json",
        lambda *a, **k: {"ads": [1]},
    )
    source = LeboncoinSource()
    monkeypatch.setattr(source, "_datadome_cookie", lambda: None)

    assert source._post({}) == {"ads": [1]}
    assert leboncoin._cached_session is None  # never cached the bad identity


def test_cached_session_invalidated_on_markerless_error(monkeypatch):
    """A previously-good cached session that starts returning a markerless
    non-200 must be dropped, not reused forever."""
    cached = _Session([_Response(200, {"ads": [1]})])
    fresh = _Session([], get_status=500)
    factory = [cached, fresh, fresh]
    monkeypatch.setattr(leboncoin.cffi_requests, "Session", lambda **_k: factory.pop(0))
    monkeypatch.setattr(leboncoin.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(leboncoin.time, "monotonic", lambda: 0.0)
    source = LeboncoinSource()
    monkeypatch.setattr(source, "_datadome_cookie", lambda: None)

    assert source._post_curl({}) == {"ads": [1]}
    assert leboncoin._cached_session is cached

    cached.post_responses = [_Response(500, headers={})]  # now the identity dies
    with pytest.raises(DataDomeBlocked):
        source._post_curl({})
    assert leboncoin._cached_session is None


def test_browser_failure_does_not_pin_and_surfaces_block(monkeypatch):
    """If the browser is unavailable, a curl challenge surfaces the original
    block (so other sources run) instead of latching to a dead transport."""
    from app.sources import leboncoin_browser

    source = LeboncoinSource()
    monkeypatch.setattr(
        source,
        "_post_curl",
        lambda _b: (_ for _ in ()).throw(DataDomeBlocked("device check", challenge=True)),
    )

    def _browser_unavailable(*_a, **_k):
        raise leboncoin_browser.LeboncoinBrowserError("Camoufox is unavailable")

    monkeypatch.setattr(leboncoin_browser, "fetch_leboncoin_json", _browser_unavailable)
    monkeypatch.setattr(leboncoin.time, "monotonic", lambda: 0.0)

    with pytest.raises(DataDomeBlocked):
        source._post({"filters": {}})
    assert leboncoin._browser_preferred_until == 0.0  # never pinned to the browser


def test_proxy_is_passed_to_curl_and_browser(monkeypatch):
    from app.sources import leboncoin_browser

    monkeypatch.setenv("LEBONCOIN_PROXY", "http://proxy.example:8080")
    captured = {}

    def _capture_session(**kwargs):
        captured["proxies"] = kwargs.get("proxies")
        return _Session([_Response(200, {"ads": []})])

    monkeypatch.setattr(leboncoin.cffi_requests, "Session", _capture_session)
    monkeypatch.setattr(leboncoin.time, "monotonic", lambda: 0.0)
    source = LeboncoinSource()
    monkeypatch.setattr(source, "_datadome_cookie", lambda: None)

    source._post_curl({})
    assert captured["proxies"] == {
        "https": "http://proxy.example:8080",
        "http": "http://proxy.example:8080",
    }

    browser_proxy = []
    monkeypatch.setattr(
        leboncoin_browser,
        "fetch_leboncoin_json",
        lambda base, profile, body, key, proxy=None: browser_proxy.append(proxy)
        or {"ads": []},
    )
    source._post_browser({})
    assert browser_proxy == ["http://proxy.example:8080"]
