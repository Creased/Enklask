from unittest.mock import patch

from app.notifier import Notifier, NotifyItem, discord_webhook


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
        description="Console pour pièces, ne s'allume plus.",
        thumbnail="https://img/thumb.jpg",
    )
    base.update(kw)
    return NotifyItem(**base)


# --- Apprise text path (non-Discord services) ------------------------------

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
    assert send.call_count == 3  # one Apprise text send per item (no Discord configured)


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


# --- Discord rich-embed path -----------------------------------------------

def test_discord_webhook_parsing():
    assert discord_webhook("discord://123/abc-DEF_9") == \
        "https://discord.com/api/webhooks/123/abc-DEF_9"
    assert discord_webhook("discord://MyBot@123/abc") == \
        "https://discord.com/api/webhooks/123/abc"
    assert discord_webhook("discord://123/abc?format=markdown") == \
        "https://discord.com/api/webhooks/123/abc"
    assert discord_webhook("https://discordapp.com/api/webhooks/123/abc") == \
        "https://discord.com/api/webhooks/123/abc"
    assert discord_webhook("ntfy://ntfy.sh/topic") is None
    assert discord_webhook("json://example") is None


def test_build_embed_full():
    n = Notifier(urls=["discord://1/tok"])
    e = n.build_embed(_item(source="vinted"))
    assert e["title"].startswith("Nintendo Switch")
    assert e["url"] == "https://ebay.fr/itm/0"
    assert e["color"] == 0x09B1BA  # vinted teal
    assert e["thumbnail"] == {"url": "https://img/thumb.jpg"}
    assert e["description"].startswith("Console pour pièces")
    names = [f["name"] for f in e["fields"]]
    assert names == ["Prix", "Source", "Lieu"]
    assert e["fields"][0]["value"] == "45 EUR"
    assert e["fields"][2]["value"] == "Rennes (3 km)"
    assert all(f["inline"] for f in e["fields"])
    assert "timestamp" in e and e["footer"]["text"] == "Nintendo Switch"


def test_build_embed_omits_missing():
    n = Notifier(urls=["discord://1/tok"])
    e = n.build_embed(_item(description="", thumbnail=None, location_city=None, distance_km=None))
    assert "description" not in e
    assert "thumbnail" not in e
    assert [f["name"] for f in e["fields"]] == ["Prix", "Source"]


def test_discord_target_gets_embed_not_text():
    n = Notifier(urls=["discord://123/tok"])
    assert n._discord == ["https://discord.com/api/webhooks/123/tok"]
    assert n._other == []
    with patch.object(Notifier, "_post_discord", return_value=True) as post, \
            patch.object(Notifier, "_send") as send:
        assert n.notify_new([_item()]) == 1
        post.assert_called_once()
        send.assert_not_called()  # no non-Discord services, so no Apprise text


def test_mixed_targets_get_both():
    n = Notifier(urls=["discord://123/tok", "ntfy://ntfy.sh/topic"])
    with patch.object(Notifier, "_post_discord", return_value=True) as post, \
            patch.object(Notifier, "_send", return_value=True) as send:
        assert n.notify_new([_item()]) == 1
        post.assert_called_once()
        send.assert_called_once()
        assert send.call_args.kwargs["urls"] == ["ntfy://ntfy.sh/topic"]
