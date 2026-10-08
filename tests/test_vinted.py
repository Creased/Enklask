import json

from app.enums import Source
from app.sources.base import SearchQuery
from app.sources.vinted import VintedSource, _extract_catalog_items, _extract_photos

# Newer Vinted catalog item shape.
ITEM_NEW = {
    "id": 987654,
    "title": "Nintendo Switch Lite HS pour pièces",
    "url": "https://www.vinted.fr/items/987654",
    "price": {"amount": "35.0", "currency_code": "EUR"},
    "photo": {
        "url": "https://images.vinted.net/thumb.jpg",
        "full_size_url": "https://images.vinted.net/full.jpg",
    },
    "city": "Rennes",
}

# Older shape: price as a bare string, relative URL.
ITEM_OLD = {
    "id": 111,
    "title": "Coque Switch",
    "url": "/items/111",
    "price": "8.5",
    "currency": "EUR",
    "photo": {"url": "https://images.vinted.net/t.jpg"},
}

ITEM_SSR = {
    "id": 10284931886,
    "productItem": {
        "id": 10284931886,
        "title": "Nintendo Switch 2 + 4 jeux",
        "url": "/items/10284931886-nintendo-switch-2-4-jeux",
        "price": {"amount": "690.00", "currencyCode": "EUR"},
        "thumbnailUrl": "https://images1.vinted.net/310x430/cover.webp",
        "photos": [{"url": "https://images1.vinted.net/f800/full.webp"}],
    },
}


def test_vinted_new_shape():
    raw = VintedSource()._to_raw(ITEM_NEW)
    assert raw.source is Source.VINTED
    assert raw.source_id == "987654"
    assert raw.price == 35.0
    assert raw.currency == "EUR"
    assert raw.thumbnail == "https://images.vinted.net/thumb.jpg"
    assert raw.photos == ["https://images.vinted.net/full.jpg"]
    assert raw.location_city == "Rennes"


def test_vinted_old_shape_and_relative_url():
    raw = VintedSource()._to_raw(ITEM_OLD)
    assert raw.price == 8.5
    assert raw.url == "https://www.vinted.fr/items/111"
    # Falls back to thumbnail when no full-size url is given.
    assert raw.photos == ["https://images.vinted.net/t.jpg"]


def test_vinted_current_ssr_shape():
    raw = VintedSource()._to_raw(ITEM_SSR)
    assert raw.source_id == "10284931886"
    assert raw.title == "Nintendo Switch 2 + 4 jeux"
    assert raw.price == 690.0
    assert raw.currency == "EUR"
    assert raw.thumbnail.endswith("/310x430/cover.webp")
    assert raw.photos == ["https://images1.vinted.net/f800/full.webp"]
    assert raw.url.startswith("https://www.vinted.fr/items/")


def test_extract_catalog_items_from_next_rsc_payload():
    state = (
        '99:{"items":{"items":'
        + json.dumps([ITEM_SSR], separators=(",", ":"))
        + ',"pagination":{"current_page":1,"per_page":96}}}'
    )
    script = (
        "self.__next_f.push(" + json.dumps([1, state], separators=(",", ":")) + ")"
    )
    html = f"<html><script src='asset.js'></script><script>{script}</script></html>"

    assert _extract_catalog_items(html) == [ITEM_SSR]


def test_extract_catalog_items_ignores_unrelated_next_payloads():
    html = "<script>self.__next_f.push([1,\"1:unrelated\"])</script>"
    assert _extract_catalog_items(html) is None


def test_extract_catalog_items_accepts_empty_results():
    state = '99:{"items":{"items":[],"pagination":{"current_page":1}}}'
    script = "self.__next_f.push(" + json.dumps([1, state]) + ")"
    assert _extract_catalog_items(f"<script>{script}</script>") == []


def test_extract_photos_uses_current_top_level_urls():
    html = r'''\"photos\":[
        {\"thumbnails\":[{\"url\":\"https://images.vinted.net/thumb.jpg\"}],
         \"url\":\"https://images.vinted.net/first.jpg\"},
        {\"url\":\"https://images.vinted.net/second.jpg\"}
    ]'''

    assert _extract_photos(html) == [
        "https://images.vinted.net/first.jpg",
        "https://images.vinted.net/second.jpg",
    ]


def test_search_warms_and_scrapes_catalog_page(monkeypatch):
    state = (
        '99:{"items":{"items":'
        + json.dumps([ITEM_SSR], separators=(",", ":"))
        + "}}"
    )
    html = (
        "<script>self.__next_f.push("
        + json.dumps([1, state], separators=(",", ":"))
        + ")</script>"
    )
    calls = []
    source = VintedSource()
    monkeypatch.setattr(
        source,
        "_get_html",
        lambda path, **kwargs: calls.append((path, kwargs)) or html,
    )

    rows = source.search(
        SearchQuery(query="switch", price_max=200, condition="for_parts")
    )

    assert len(rows) == 1
    assert calls[0][0] == "/catalog"
    assert calls[0][1]["params"] == {
        "search_text": "switch",
        "order": "newest_first",
        "price_to": 200,
        "status_ids[]": "7",
    }


def test_html_transport_uses_persistent_browser(monkeypatch):
    source = VintedSource()
    calls = []
    monkeypatch.setattr(
        "app.sources.vinted.fetch_vinted_html",
        lambda base, profile, path, params: calls.append(
            (base, profile, path, params)
        )
        or "<html>browser</html>",
    )

    html = source._get_html("/catalog", params={"search_text": "switch"})

    assert html == "<html>browser</html>"
    assert calls[0][2:] == ("/catalog", {"search_text": "switch"})
