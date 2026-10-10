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
    monkeypatch.setattr(leboncoin, "_browser_required", False)


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


def test_failed_warmups_are_paced_then_start_cooldown(monkeypatch):
    sessions = [_Session([], get_status=403) for _ in range(2)]
    sleeps = []
    monkeypatch.setattr(
        leboncoin.cffi_requests,
        "Session",
        lambda **_kwargs: sessions.pop(0),
    )
    monkeypatch.setattr(leboncoin.time, "sleep", sleeps.append)
    monkeypatch.setattr(leboncoin.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(leboncoin.random, "uniform", lambda low, _high: low)
    source = LeboncoinSource()
    monkeypatch.setattr(source, "_datadome_cookie", lambda: None)

    with pytest.raises(DataDomeBlocked, match="paced attempts"):
        source._post_curl({})
    assert sleeps == [leboncoin._BACKOFF_BASE_SECONDS]
    assert leboncoin._blocked_until == 100.0 + leboncoin._BLOCK_COOLDOWN_SECONDS

    with pytest.raises(DataDomeBlocked, match="cooldown active"):
        source._post_curl({})


def test_device_check_switches_to_persistent_browser(monkeypatch):
    from app.sources import leboncoin_browser

    source = LeboncoinSource()
    monkeypatch.setattr(
        source,
        "_post_curl",
        lambda _body: (_ for _ in ()).throw(
            DataDomeBlocked("device check", challenge=True)
        ),
    )
    calls = []
    monkeypatch.setattr(
        leboncoin_browser,
        "fetch_leboncoin_json",
        lambda base, profile, body, key: calls.append((base, profile, body, key))
        or {"ads": []},
    )

    assert source._post({"filters": {}}) == {"ads": []}
    assert leboncoin._browser_required
    assert calls[0][0] == "https://www.leboncoin.fr"
