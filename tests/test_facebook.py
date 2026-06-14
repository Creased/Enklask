from app.enums import Source
from app.sources.facebook import FacebookSource

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
