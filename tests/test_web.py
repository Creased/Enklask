from starlette.requests import Request

from app.enums import Source
from app.sources.base import RawListing
from app.web import routes as web_routes

templates = web_routes.templates


def test_listing_images_open_the_preview():
    for template_name in ("_card.html", "_search_results.html", "_watch_row.html"):
        source, _, _ = templates.env.loader.get_source(
            templates.env, template_name
        )
        assert 'onclick="openPreview(this)"' in source
        assert "cursor-zoom-in" in source


def test_preview_history_is_short_and_rejects_polluted_trails():
    history = [{"price": price} for price in range(10)]
    assert web_routes._preview_history(history) == history[-6:]
    assert web_routes._preview_history(history * 6) == []


class _Source:
    def __init__(self, name, *, results=None, error=None):
        self.name = name
        self._results = results or []
        self._error = error

    def search(self, query):
        if self._error:
            raise self._error
        return self._results


def test_live_search_displays_source_errors_without_hiding_results(monkeypatch):
    listing = RawListing(
        source=Source.EBAY,
        source_id="123",
        title="Nintendo Switch",
        url="https://example.com/123",
    )
    sources = [
        _Source(Source.EBAY, results=[listing]),
        _Source(Source.VINTED, error=RuntimeError("HTTP 403 challenge")),
    ]
    monkeypatch.setattr(web_routes, "get_enabled_sources", lambda: sources)
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/search/results",
        "query_string": b"q=switch",
        "headers": [],
    })

    response = web_routes.search_results_partial(request)
    html = response.body.decode()

    assert "Nintendo Switch" in html
    assert "Certaines sources" in html
    assert "pas pu être interrogées" in html
    assert "Vinted" in html
    assert "HTTP 403 challenge" in html
