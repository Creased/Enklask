from unittest.mock import patch

from app.notifier import Notifier, NotifyItem


def _item(i: int = 0, **kw) -> NotifyItem:
    base = dict(
        source="ebay",
        title=f"Nintendo Switch OLED pour pièces {i}",
        url=f"https://ebay.fr/itm/{i}",
        price=45.0,
        currency="EUR",
        location_city="Rennes",
        distance_km=3.0,
        topic_name="Nintendo Switch",
    )
    base.update(kw)
    return NotifyItem(**base)


def test_disabled_without_urls():
    n = Notifier(urls=[])
    assert n.enabled is False
    with patch.object(Notifier, "_send") as send:
        assert n.notify_new([_item()]) == 0
        send.assert_not_called()


def test_format_listing_has_price_and_url():
    n = Notifier(urls=["json://example"])
    title, body = n.format_listing(_item(price=45.0))
    assert "45EUR" in title
    assert "https://ebay.fr/itm/0" in body
    assert "Rennes" in body


def test_format_listing_includes_topic():
    n = Notifier(urls=["json://example"])
    title, _ = n.format_listing(_item(topic_name="Laptop"))
    assert "[Laptop]" in title


def test_individual_sends_below_cap():
    n = Notifier(urls=["json://example"], max_per_poll=15)
    items = [_item(i) for i in range(3)]
    with patch.object(Notifier, "_send", return_value=True) as send:
        sent = n.notify_new(items)
    assert sent == 3
    assert send.call_count == 3
    # Only (title, body) — no image attachment (the link preview shows the photo).
    assert len(send.call_args.args) == 2


def test_digest_above_cap():
    n = Notifier(urls=["json://example"], max_per_poll=2)
    items = [_item(i) for i in range(10)]
    with patch.object(Notifier, "_send", return_value=True) as send:
        sent = n.notify_new(items)
    assert sent == 1
    assert send.call_count == 1
    title, body = send.call_args.args[0], send.call_args.args[1]
    assert "10 nouvelles annonces" in title
    assert "et 8 de plus" in body


def test_cold_start_skips():
    n = Notifier(urls=["json://example"], notify_on_first_run=False)
    with patch.object(Notifier, "_send") as send:
        assert n.notify_new([_item()], is_cold_start=True) == 0
        send.assert_not_called()


def test_cold_start_sends_when_enabled():
    n = Notifier(urls=["json://example"], notify_on_first_run=True)
    with patch.object(Notifier, "_send", return_value=True) as send:
        assert n.notify_new([_item()], is_cold_start=True) == 1
        send.assert_called_once()


def test_send_swallows_errors():
    n = Notifier(urls=["totally-invalid-url"])
    assert n._send("t", "b") is False


def test_send_swallows_base_exception():
    import apprise

    with patch.object(apprise, "Apprise", side_effect=BaseException("boom")):
        assert Notifier(urls=["json://x"])._send("t", "b") is False
