import json

from app.enums import Source
from app.sources.rakuten import RakutenSource, _extract_products_from_html

PRODUCT = {
    "@type": "Product",
    "name": "Nintendo Switch OLED pour pièces",
    "url": "https://fr.shopping.rakuten.com/mfp/9876543/nintendo-switch-oled",
    "image": ["https://fr.shopping.rakuten.com/photo/abc.jpg"],
    "offers": {
        "@type": "AggregateOffer",
        "lowPrice": "39.90",
        "priceCurrency": "EUR",
    },
}


def _html_with(*ld_objects) -> str:
    scripts = "".join(
        f'<script type="application/ld+json">{json.dumps(o)}</script>'
        for o in ld_objects
    )
    return f"<html><head>{scripts}</head><body></body></html>"


def test_extract_products_direct_and_in_itemlist():
    item_list = {
        "@type": "ItemList",
        "itemListElement": [
            {"@type": "ListItem", "item": PRODUCT},
        ],
    }
    # A non-product (Organization) must be ignored; duplicate name deduped.
    html = _html_with(PRODUCT, item_list, {"@type": "Organization", "name": "Rakuten"})
    products = _extract_products_from_html(html)
    assert len(products) == 1
    assert products[0]["name"].startswith("Nintendo Switch OLED")


def test_rakuten_to_raw():
    raw = RakutenSource()._to_raw(PRODUCT)
    assert raw.source is Source.RAKUTEN
    assert raw.source_id == "9876543"
    assert raw.price == 39.9
    assert raw.currency == "EUR"
    assert raw.thumbnail == "https://fr.shopping.rakuten.com/photo/abc.jpg"
    assert raw.url.endswith("nintendo-switch-oled")


def test_rakuten_to_raw_simple_offer_and_no_name():
    p = {
        "@type": "Product",
        "name": "Carte mère Switch",
        "url": "https://fr.shopping.rakuten.com/offer/123456/x",
        "offers": {"price": "25", "priceCurrency": "EUR"},
    }
    raw = RakutenSource()._to_raw(p)
    assert raw.price == 25.0
    assert raw.photos == []
    # Missing name -> skipped.
    assert RakutenSource()._to_raw({"@type": "Product"}) is None
