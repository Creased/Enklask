from app.enums import Source
from app.sources.base import SearchQuery
from app.sources.facebook import FacebookSource, _apply_price_filter

# --- Logged-in Playwright fallback parser (legacy card scraping) -----------

CARDS = [
    {
        "href": "https://www.facebook.com/marketplace/item/1234567890/?ref=x",
        "text": "45 €\nNintendo Switch OLED pour pièces\nRennes",
        "image": "https://scontent.fb.com/img1.jpg",
    },
    # Duplicate item id — should be collapsed.
    {
        "href": "https://www.facebook.com/marketplace/item/1234567890/",
        "text": "45 €\nNintendo Switch OLED\nRennes",
        "image": "https://scontent.fb.com/img1.jpg",
    },
    # Not a marketplace item link — should be ignored.
    {"href": "https://www.facebook.com/groups/123", "text": "x", "image": None},
]


def test_facebook_parse_cards_dedups_and_filters():
    raws = FacebookSource()._parse_cards(CARDS)
    assert len(raws) == 1
    raw = raws[0]
    assert raw.source is Source.FACEBOOK
    assert raw.source_id == "1234567890"
    assert raw.price == 45.0
    assert raw.title == "Nintendo Switch OLED pour pièces"
    assert raw.location_city == "Rennes"
    assert raw.url == "https://www.facebook.com/marketplace/item/1234567890/"
    assert raw.thumbnail == "https://scontent.fb.com/img1.jpg"


def test_facebook_price_with_thousands_separator():
    raw = FacebookSource()._parse_card(
        {
            "href": "https://www.facebook.com/marketplace/item/55/",
            "text": "1 200 €\nLot de consoles Switch\nNantes",
            "image": None,
        }
    )
    assert raw.price == 1200.0
    assert raw.location_city == "Nantes"


# --- Anonymous GraphQL feed parser (the no-account path) -------------------

def _node(item_id, title, amount, formatted="120 €", city="Bruz", **flags):
    listing = {
        "id": item_id,
        "marketplace_listing_title": title,
        "listing_price": {"amount": amount, "formatted_amount": formatted},
        "primary_listing_photo": {"image": {"uri": f"https://cdn/{item_id}.jpg"}},
        "location": {"reverse_geocode": {"city": city}},
        "is_live": True,
        "is_sold": False,
        "is_hidden": False,
        "is_pending": False,
    }
    listing.update(flags)
    return {"node": {"__typename": "MarketplaceFeedListingStoryObject", "listing": listing}}


def _payload(*nodes):
    return {"data": {"marketplace_search": {"feed_units": {"edges": list(nodes)}}}}


def test_facebook_parse_feed_maps_fields():
    payload = _payload(_node("111", "Nintendo Switch OLED", "120.00"))
    raws = FacebookSource()._parse_feed(payload)
    assert len(raws) == 1
    raw = raws[0]
    assert raw.source is Source.FACEBOOK
    assert raw.source_id == "111"
    assert raw.title == "Nintendo Switch OLED"
    assert raw.price == 120.0
    assert raw.currency == "EUR"
    assert raw.url == "https://www.facebook.com/marketplace/item/111/"
    assert raw.thumbnail == "https://cdn/111.jpg"
    assert raw.photos == ["https://cdn/111.jpg"]
    assert raw.location_city == "Bruz"


def test_facebook_parse_feed_dedups_and_skips_sold():
    payload = _payload(
        _node("111", "A", "120.00"),
        _node("111", "A dup", "120.00"),  # duplicate id -> collapsed
        _node("222", "Sold one", "80.00", is_sold=True),  # sold -> dropped
        {"node": {"listing": {}}},  # no id -> dropped
    )
    raws = FacebookSource()._parse_feed(payload)
    assert [r.source_id for r in raws] == ["111"]


def test_facebook_currency_from_formatted_amount():
    payload = _payload(_node("9", "US item", "50.00", formatted="$50"))
    raw = FacebookSource()._parse_feed(payload)[0]
    assert raw.currency == "USD"


def test_facebook_price_filter_uses_decimal_amount():
    payload = _payload(
        _node("1", "cheap", "30.00"),
        _node("2", "mid", "120.00"),
        _node("3", "pricey", "300.00"),
    )
    raws = FacebookSource()._parse_feed(payload)
    filtered = _apply_price_filter(raws, SearchQuery("switch", price_max=150))
    assert [r.source_id for r in filtered] == ["1", "2"]
