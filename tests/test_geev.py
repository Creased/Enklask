from app.enums import Source
from app.sources.geev import GeevSource, _extract_articles

ARTICLE = {
    "id": "abc123",
    "title": "Nintendo Switch en panne pour pièces",
    "description": "Ne s'allume plus",
    "latitude": 48.10,
    "longitude": -1.70,
    "city": "Rennes",
    "pictures": [
        {"url": "https://cdn.geev.fr/1.jpg"},
        {"url": "https://cdn.geev.fr/2.jpg"},
    ],
}


def test_extract_articles_various_shapes():
    assert _extract_articles([ARTICLE]) == [ARTICLE]
    assert _extract_articles({"articles": [ARTICLE]}) == [ARTICLE]
    assert _extract_articles({"data": [ARTICLE]}) == [ARTICLE]
    assert _extract_articles({"nope": 1}) == []


def test_geev_to_raw():
    raw = GeevSource()._to_raw(ARTICLE)
    assert raw.source is Source.GEEV
    assert raw.source_id == "abc123"
    assert raw.price == 0.0  # donations are free
    assert raw.location_city == "Rennes"
    assert raw.lat == 48.10 and raw.lon == -1.70
    assert raw.photos == ["https://cdn.geev.fr/1.jpg", "https://cdn.geev.fr/2.jpg"]
    assert raw.thumbnail == "https://cdn.geev.fr/1.jpg"
    assert raw.url.endswith("/fr/ad/abc123")


def test_geev_nested_location_and_string_images():
    article = {
        "_id": "xyz",
        "title": "Coque Switch",
        "location": {"city": "Cesson", "latitude": 48.12, "longitude": -1.6},
        "images": ["https://cdn.geev.fr/a.jpg"],
    }
    raw = GeevSource()._to_raw(article)
    assert raw.source_id == "xyz"
    assert raw.location_city == "Cesson"
    assert raw.lat == 48.12 and raw.lon == -1.6
    assert raw.photos == ["https://cdn.geev.fr/a.jpg"]
