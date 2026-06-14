import json

from app.enums import Source
from app.sources.leboncoin import (
    LeboncoinSource,
    _extract_ads_from_html,
    _find_ads,
)

AD = {
    "list_id": 2890001,
    "subject": "Nintendo Switch carte mère HS",
    "body": "Carte mère en panne, envoi Mondial Relay possible",
    "url": "https://www.leboncoin.fr/jeux_video/2890001.htm",
    "price": [40],
    "images": {
        "thumb_url": "https://img.leboncoin.fr/thumb.jpg",
        "urls": [
            "https://img.leboncoin.fr/1.jpg",
            "https://img.leboncoin.fr/2.jpg",
        ],
    },
    "location": {"city": "Rennes", "lat": 48.11, "lng": -1.68, "zipcode": "35000"},
    "first_publication_date": "2026-06-14 09:15:00",
}


def test_leboncoin_to_raw():
    raw = LeboncoinSource()._to_raw(AD)
    assert raw.source is Source.LEBONCOIN
    assert raw.source_id == "2890001"
    assert raw.price == 40.0
    assert raw.thumbnail == "https://img.leboncoin.fr/thumb.jpg"
    assert len(raw.photos) == 2
    assert raw.location_city == "Rennes"
    assert raw.lat == 48.11 and raw.lon == -1.68
    assert "mondial_relay" in raw.shipping_options
    assert raw.posted_at is not None and raw.posted_at.year == 2026


def test_leboncoin_handles_missing_images():
    raw = LeboncoinSource()._to_raw(
        {"list_id": 1, "subject": "x", "url": "u", "price": []}
    )
    assert raw.thumbnail is None
    assert raw.photos == []
    assert raw.price is None


def test_find_ads_recurses_and_dedups():
    blob = {
        "props": {
            "pageProps": {
                "searchData": {
                    "ads": [AD, {"list_id": 2890002, "subject": "Switch Lite"}],
                }
            }
        },
        # A duplicate of the same ad nested elsewhere should be collapsed.
        "other": [AD],
    }
    ads = _find_ads(blob)
    ids = sorted(a["list_id"] for a in ads)
    assert ids == [2890001, 2890002]


def test_extract_ads_from_next_data_html():
    next_data = {"props": {"pageProps": {"ads": [AD]}}}
    html = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(next_data)
        + "</script></body></html>"
    )
    ads = _extract_ads_from_html(html)
    assert len(ads) == 1
    # The same _to_raw parser handles browser-sourced ads.
    raw = LeboncoinSource()._to_raw(ads[0])
    assert raw.source is Source.LEBONCOIN
    assert raw.source_id == "2890001"
    assert raw.price == 40.0


def test_extract_ads_from_html_without_next_data():
    assert _extract_ads_from_html("<html>no data here</html>") == []
