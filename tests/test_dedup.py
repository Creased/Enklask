import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.enums import Source
from app.models import Base, Listing
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


def test_insert_then_dedup(session):
    from app.dedup import upsert_listing

    assert upsert_listing(session, _raw()) is not None
    session.commit()

    # Same source/source_id should not create a second row.
    assert upsert_listing(session, _raw(price=25.0)) is None
    session.commit()

    count = session.scalar(select(func.count()).select_from(Listing))
    assert count == 1

    # Price update is reflected on re-seen.
    listing = session.scalar(select(Listing))
    assert listing.price == 25.0
    # Classification was applied on insert.
    assert listing.model_guess == "oled"
    assert listing.part_guess == "motherboard"


def test_distinct_ids_create_rows(session):
    from app.dedup import upsert_listing

    upsert_listing(session, _raw(source_id="a"))
    upsert_listing(session, _raw(source_id="b"))
    session.commit()
    count = session.scalar(select(func.count()).select_from(Listing))
    assert count == 2
