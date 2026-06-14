from app.enums import Source
from app.sources.vinted import VintedSource

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
