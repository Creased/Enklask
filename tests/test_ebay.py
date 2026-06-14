from app.enums import Source
from app.sources.ebay import EbaySource, _parse_search_html

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


def test_ebay_api_to_raw_maps_fields():
    source = EbaySource()
    raw = source._api_to_raw(SAMPLE_ITEM)

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


def test_ebay_api_handles_missing_optional_fields():
    source = EbaySource()
    raw = source._api_to_raw({"itemId": "x", "title": "t", "itemWebUrl": "u"})
    assert raw.price is None
    assert raw.photos == []
    assert raw.posted_at is None


def _card(listing_id: str, title: str, price: str = "48,00 EUR") -> str:
    return (
        f'<li class="s-card s-card--horizontal" data-listingid="{listing_id}">'
        f'<a class="s-card__link image-treatment" '
        f'href="https://www.ebay.fr/itm/{listing_id}?_skw=x&amp;hash=y">'
        f'<img class="s-card__image" src="https://i.ebayimg.com/images/g/Z/s-l500.webp" '
        f'alt="{title}"></a>'
        f'<div role="heading" aria-level="3" class="s-card__title">'
        f'<span class="su-styled-text primary default">{title}</span></div>'
        f'<span class="su-styled-text primary bold s-card__price">{price}</span></li>'
    )


def test_scrape_parser_extracts_fields():
    html = "<ul>" + _card("147366270841", "Nintendo Switch HS Pour Pièces") + "</ul>"
    results = _parse_search_html(html)

    assert len(results) == 1
    r = results[0]
    assert r.source is Source.EBAY
    assert r.source_id == "147366270841"
    assert "Nintendo Switch HS" in r.title
    assert r.url == "https://www.ebay.fr/itm/147366270841"  # query stripped
    assert r.price == 48.0
    assert r.currency == "EUR"
    assert r.thumbnail.endswith("s-l500.webp")


def test_scrape_parser_dedupes_and_skips_placeholder():
    html = (
        _card("111", "Real Listing")
        + _card("111", "Real Listing")  # clipped duplicate
        + _card("222", "Shop on eBay")  # placeholder card
    )
    results = _parse_search_html(html)

    assert [r.source_id for r in results] == ["111"]
