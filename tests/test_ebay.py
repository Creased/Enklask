from app.enums import Source
from app.sources.ebay import EbaySource

# A trimmed-down sample of a Browse API item_summary response item.
SAMPLE_ITEM = {
    "itemId": "v1|123456789|0",
    "title": "Nintendo Switch OLED pour pièces HS",
    "itemWebUrl": "https://www.ebay.fr/itm/123456789",
    "price": {"value": "45.00", "currency": "EUR"},
    "image": {"imageUrl": "https://i.ebayimg.com/images/g/abc/s-l500.jpg"},
    "additionalImages": [
        {"imageUrl": "https://i.ebayimg.com/images/g/def/s-l500.jpg"}
    ],
    "itemLocation": {"city": "Rennes", "country": "FR"},
    "itemCreationDate": "2026-06-14T08:30:00.000Z",
}


def test_ebay_to_raw_maps_fields():
    source = EbaySource()
    raw = source._to_raw(SAMPLE_ITEM)

    assert raw.source is Source.EBAY
    assert raw.source_id == "v1|123456789|0"
    assert raw.title.startswith("Nintendo Switch OLED")
    assert raw.url == "https://www.ebay.fr/itm/123456789"
    assert raw.price == 45.0
    assert raw.currency == "EUR"
    assert raw.thumbnail.endswith("s-l500.jpg")
    assert len(raw.photos) == 2
    assert raw.location_city == "Rennes"
    assert raw.posted_at is not None
    assert raw.posted_at.year == 2026


def test_ebay_handles_missing_optional_fields():
    source = EbaySource()
    raw = source._to_raw({"itemId": "x", "title": "t", "itemWebUrl": "u"})
    assert raw.price is None
    assert raw.photos == []
    assert raw.posted_at is None
