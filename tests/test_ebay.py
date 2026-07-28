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


def _card(listing_id: str, title: str, price: str = "48,00 EUR", extra: str = "") -> str:
    return (
        f'<li class="s-card s-card--horizontal" data-listingid="{listing_id}">'
        f'<a class="s-card__link image-treatment" '
        f'href="https://www.ebay.fr/itm/{listing_id}?_skw=x&amp;hash=y">'
        f'<img class="s-card__image" src="https://i.ebayimg.com/images/g/Z/s-l500.webp" '
        f'alt="{title}"></a>'
        f'<div role="heading" aria-level="3" class="s-card__title">'
        f'<span class="su-styled-text primary default">{title}</span></div>'
        f'<span class="su-styled-text primary bold s-card__price">{price}</span>'
        f'{extra}</li>'
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


# --- shipping cost + auction/buy-it-now -------------------------------------

def test_ebay_shipping_and_format_helpers():
    from app.sources.ebay import _ebay_buying_format, _parse_ebay_shipping

    # English (the www.ebay.com fallback)
    assert _parse_ebay_shipping("+$10.43 delivery") == 10.43
    assert _parse_ebay_shipping("Free delivery") == 0.0
    assert _ebay_buying_format("Buy It Now") == "buy_it_now"
    assert _ebay_buying_format("12 bids") == "auction"
    assert _ebay_buying_format("0 bids or Best Offer") == "auction"
    assert _ebay_buying_format("or Best Offer") == "buy_it_now"
    # French (ebay.fr)
    assert _parse_ebay_shipping("+4,99 EUR livraison") == 4.99
    assert _parse_ebay_shipping("Livraison gratuite") == 0.0
    assert _ebay_buying_format("3 enchères") == "auction"
    # no shipping info in the text
    assert _parse_ebay_shipping("Située en France") is None


def test_ebay_api_shipping_and_format():
    src = EbaySource()
    raw = src._api_to_raw(dict(
        SAMPLE_ITEM,
        buyingOptions=["AUCTION", "FIXED_PRICE"],
        shippingOptions=[{"shippingCost": {"value": "4.99", "currency": "EUR"}}],
    ))
    assert raw.buying_format == "auction"
    assert raw.shipping_cost == 4.99

    raw2 = src._api_to_raw(dict(
        SAMPLE_ITEM,
        buyingOptions=["FIXED_PRICE"],
        shippingOptions=[{"shippingCost": {"value": "0.0"}}],
    ))
    assert raw2.buying_format == "buy_it_now"
    assert raw2.shipping_cost == 0.0

    # No buying/shipping options -> both None.
    raw3 = src._api_to_raw(SAMPLE_ITEM)
    assert raw3.buying_format is None
    assert raw3.shipping_cost is None


def test_scrape_parser_shipping_and_auction():
    html = (
        _card("111", "Switch BIN", extra=(
            '<div class="s-card__attribute-row">Buy It Now</div>'
            '<div class="s-card__attribute-row">+$10.43 delivery</div>'
        ))
        + _card("222", "Switch Auction", extra=(
            '<div class="s-card__attribute-row">5 bids</div>'
            '<div class="s-card__attribute-row">Free delivery</div>'
        ))
    )
    by = {r.source_id: r for r in _parse_search_html(html)}
    assert by["111"].buying_format == "buy_it_now" and by["111"].shipping_cost == 10.43
    assert by["222"].buying_format == "auction" and by["222"].shipping_cost == 0.0
