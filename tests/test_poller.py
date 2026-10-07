from types import SimpleNamespace

from app.enums import Source
from app.poller import _poll_one


class _Source:
    name = Source.EBAY

    def __init__(self):
        self.query = None

    def search(self, query):
        self.query = query
        return []


def test_saved_search_minimum_price_reaches_source(monkeypatch):
    monkeypatch.setattr("app.poller.time.sleep", lambda _: None)
    source = _Source()
    topic = SimpleNamespace(id=1, name="Switch")
    search = SimpleNamespace(
        id=2,
        query="Nintendo Switch",
        price_min=25.0,
        price_max=150.0,
        max_distance_km=None,
        condition=None,
        sources=[],
        tags=[],
    )

    _poll_one(topic, search, [source])

    assert source.query.price_min == 25.0
    assert source.query.price_max == 150.0
