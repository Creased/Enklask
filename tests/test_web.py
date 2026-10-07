from app.web.routes import templates


def test_listing_images_open_the_preview():
    for template_name in ("_card.html", "_search_results.html", "_watch_row.html"):
        source, _, _ = templates.env.loader.get_source(
            templates.env, template_name
        )
        assert 'onclick="openPreview(this)"' in source
        assert "cursor-zoom-in" in source
