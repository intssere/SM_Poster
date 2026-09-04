from app.db.base import Base
from app.models.domain import PinPublication, PublicationStatus
from app.services.publication_duplicates import (
    ALREADY_PUBLISHED,
    DUPLICATE_PUBLICATION,
    POSSIBLE_DUPLICATE_PIN,
    SAFE_TO_CONTINUE,
    UNKNOWN_OUTCOME_BLOCKS_RETRY,
    evaluate_publication_duplicates,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _publication(**overrides):
    values = {
        "draft_id": "draft",
        "creative_id": "creative",
        "publication_fingerprint": "p" * 64,
        "status": PublicationStatus.SCHEDULED,
        "pinterest_board_id_snapshot": "board-1",
        "utm_url": "https://diamondshelf.us/products/item?utm_source=pinterest",
        "creative_fingerprint": "c" * 64,
        "text_fingerprint": "t" * 64,
    }
    values.update(overrides)
    return PinPublication(**values)


def _result(current, *others):
    db = _db()
    db.add(current)
    db.add_all(others)
    db.commit()
    return evaluate_publication_duplicates(db, current)


def test_current_published_or_known_provider_pin_blocks():
    assert _result(_publication(status=PublicationStatus.PUBLISHED))["status"] == ALREADY_PUBLISHED
    assert _result(_publication(pinterest_pin_id="pin-123"))["status"] == ALREADY_PUBLISHED


def test_current_unknown_blocks_retry():
    result = _result(_publication(status=PublicationStatus.PUBLISH_UNKNOWN))
    assert result["status"] == UNKNOWN_OUTCOME_BLOCKS_RETRY
    assert result["blocking"] is True


def test_other_matching_unknown_blocks_retry():
    current = _publication(id="current", publication_fingerprint="a" * 64)
    other = _publication(id="other", publication_fingerprint="b" * 64, status=PublicationStatus.PUBLISH_UNKNOWN)
    result = _result(current, other)
    assert result["status"] == UNKNOWN_OUTCOME_BLOCKS_RETRY
    assert result["matches"] == [{"publication_id": "other", "status": "PUBLISH_UNKNOWN", "reason_code": UNKNOWN_OUTCOME_BLOCKS_RETRY}]


def test_exact_published_operational_duplicate_blocks():
    current = _publication(id="current", publication_fingerprint="a" * 64)
    other = _publication(id="other", publication_fingerprint="b" * 64, status=PublicationStatus.PUBLISHED)
    result = _result(current, other)
    assert result["status"] == DUPLICATE_PUBLICATION
    assert result["blocking"] is True


def test_same_creative_link_board_with_different_text_blocks_as_possible_duplicate():
    current = _publication(id="current", publication_fingerprint="a" * 64, text_fingerprint="t" * 64)
    other = _publication(id="other", publication_fingerprint="b" * 64, status=PublicationStatus.PUBLISHED, text_fingerprint="u" * 64)
    result = _result(current, other)
    assert result["status"] == POSSIBLE_DUPLICATE_PIN
    assert result["blocking"] is True


def test_different_legitimate_creative_or_destination_is_safe():
    assert _result(
        _publication(id="current", publication_fingerprint="a" * 64),
        _publication(id="other", publication_fingerprint="b" * 64, status=PublicationStatus.PUBLISHED, creative_fingerprint="x" * 64),
    )["status"] == SAFE_TO_CONTINUE
    assert _result(
        _publication(id="current", publication_fingerprint="c" * 64),
        _publication(id="other", publication_fingerprint="d" * 64, status=PublicationStatus.PUBLISHED, utm_url="https://diamondshelf.us/products/other?utm_source=pinterest"),
    )["status"] == SAFE_TO_CONTINUE


def test_duplicate_result_exposes_safe_match_metadata_only():
    result = _result(
        _publication(id="current", publication_fingerprint="a" * 64),
        _publication(id="other", publication_fingerprint="b" * 64, status=PublicationStatus.PUBLISH_UNKNOWN),
    )
    rendered = repr(result).lower()
    assert "request_fingerprint" not in rendered
    assert "provider_response" not in rendered
    assert "access_token" not in rendered
    assert "refresh_token" not in rendered
    assert "traceback" not in rendered
