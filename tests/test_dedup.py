import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.enums import Source
from app.models import Base, Listing, ListingTopic, SavedSearch, Topic
from app.sources.base import RawListing


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    s = factory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def topic(session):
    t = Topic(name="Test", slug="test")
    session.add(t)
    session.flush()
    return t


def _raw(**kwargs) -> RawListing:
    base = dict(
        source=Source.EBAY,
        source_id="abc",
        title="Nintendo Switch OLED carte mère",
        url="https://example.com/1",
        price=30.0,
    )
    base.update(kwargs)
    return RawListing(**base)


def test_insert_then_dedup(session, topic):
    from app.dedup import upsert_listing

    assert upsert_listing(session, _raw(), topic_id=topic.id) is not None
    session.commit()

    assert upsert_listing(session, _raw(price=25.0), topic_id=topic.id) is None
    session.commit()

    count = session.scalar(select(func.count()).select_from(Listing))
    assert count == 1

    listing = session.scalar(select(Listing))
    assert listing.price == 25.0


def test_price_history_records_changes(session, topic):
    from app.dedup import upsert_listing

    upsert_listing(session, _raw(price=60.0), topic_id=topic.id)
    session.commit()
    upsert_listing(session, _raw(price=50.0), topic_id=topic.id)  # drop
    session.commit()

    listing = session.scalar(select(Listing))
    assert listing.price == 50.0
    prices = [e["price"] for e in listing.price_history]
    assert prices == [60.0, 50.0]  # prior price seeded, then the change


def test_price_history_unchanged_price_no_growth(session, topic):
    from app.dedup import upsert_listing

    upsert_listing(session, _raw(price=60.0), topic_id=topic.id)
    session.commit()
    upsert_listing(session, _raw(price=60.0), topic_id=topic.id)  # same price
    session.commit()

    listing = session.scalar(select(Listing))
    assert [e["price"] for e in listing.price_history] == [60.0]


def test_distinct_ids_create_rows(session, topic):
    from app.dedup import upsert_listing

    upsert_listing(session, _raw(source_id="a"), topic_id=topic.id)
    upsert_listing(session, _raw(source_id="b"), topic_id=topic.id)
    session.commit()
    count = session.scalar(select(func.count()).select_from(Listing))
    assert count == 2


def test_topic_link_created(session, topic):
    from app.dedup import upsert_listing

    listing = upsert_listing(
        session, _raw(), topic_id=topic.id, search_id=None, tags=["OLED"]
    )
    session.commit()

    link = session.scalar(
        select(ListingTopic).where(
            ListingTopic.listing_id == listing.id,
            ListingTopic.topic_id == topic.id,
        )
    )
    assert link is not None
    assert "OLED" in link.tags


def test_no_duplicate_topic_link(session, topic):
    from app.dedup import upsert_listing

    upsert_listing(session, _raw(), topic_id=topic.id)
    session.commit()
    upsert_listing(session, _raw(), topic_id=topic.id)
    session.commit()

    count = session.scalar(
        select(func.count())
        .select_from(ListingTopic)
        .where(ListingTopic.topic_id == topic.id)
    )
    assert count == 1
