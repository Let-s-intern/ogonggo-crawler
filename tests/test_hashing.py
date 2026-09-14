"""중복 감지 해시 테스트. 픽스처 한 건을 바꿔가며 해시가 언제 같고 언제 달라야 하는지 단언한다."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from app.crawler.hashing import HASH_FIELDS, content_hash

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "raw-job-sample.json"


def sample() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_hash_fields_match_the_data_model() -> None:
    """`.claude/docs/data-model.md` 가 정한 네 필드다. 늘리거나 줄이면 여기서 걸린다."""
    assert HASH_FIELDS == ("source_url", "title", "recruitment_end_at", "body")


def test_same_posting_hashes_the_same() -> None:
    assert content_hash(sample()) == content_hash(sample())


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("view_count", "9999"),
        ("posted_relative", "방금 전"),
        ("ad_text", "마감 임박"),
        ("list_index", 7),
        ("crawled_at", "2026-08-22T18:30:00+09:00"),
        ("company_name", "다른회사"),
    ],
)
def test_noisy_fields_do_not_change_the_hash(field: str, changed: Any) -> None:
    """매 크롤마다 달라지는 값이 섞이면 같은 공고가 매번 신규가 된다."""
    other = sample()
    other[field] = changed

    assert content_hash(other) == content_hash(sample())


def test_the_source_text_stays_out_of_the_hash() -> None:
    """원문에는 조회수·배너·옆에 붙은 안내가 섞인다. 해시에 들어가면 같은 공고가 매 크롤마다
    신규로 쌓인다. `HASH_FIELDS` 는 넷 그대로다."""
    assert "source_text" not in HASH_FIELDS

    first = sample()
    first["source_text"] = "백엔드 개발자\n조회수 1,204\n지원하기"
    second = sample()
    second["source_text"] = "백엔드 개발자\n조회수 1,881\n지원하기\n신규 배너"

    assert content_hash(first) == content_hash(sample())
    assert content_hash(second) == content_hash(first)


def test_unknown_field_does_not_change_the_hash() -> None:
    other = sample()
    other["새로_생긴_필드"] = "무엇이든"

    assert content_hash(other) == content_hash(sample())


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("title", "백엔드 개발자 (Go)"),
        ("recruitment_end_at", "2026-10-31"),
        ("source_url", "https://example.test/recruit/1043"),
        ("body", "모집 분야: 서버 개발\n자격 요건: Python 5년 이상\n근무지: 서울 강남구"),
    ],
)
def test_content_change_changes_the_hash(field: str, changed: str) -> None:
    other = sample()
    other[field] = changed

    assert content_hash(other) != content_hash(sample())


def test_missing_field_is_the_same_as_empty() -> None:
    """마감일이 없는 공고를 두 번 크롤해도 신규가 되지 않는다."""
    absent = sample()
    del absent["recruitment_end_at"]
    empty = sample()
    empty["recruitment_end_at"] = None

    assert content_hash(absent) == content_hash(empty)


def test_whitespace_difference_does_not_change_the_hash() -> None:
    """앞뒤 공백과 줄바꿈 차이는 흡수한다."""
    padded = sample()
    padded["title"] = f"  {padded['title']}\n"

    assert content_hash(padded) == content_hash(sample())


def test_field_boundary_is_not_forgeable() -> None:
    """값을 이어붙인 것과 필드가 나뉜 것이 같은 해시가 되지 않는다."""
    split = {"source_url": "", "title": "백엔드", "recruitment_end_at": "개발자", "body": ""}
    merged = {"source_url": "", "title": "백엔드개발자", "recruitment_end_at": "", "body": ""}

    assert content_hash(split) != content_hash(merged)


def test_hash_is_a_sha256_hex_digest() -> None:
    digest = content_hash(sample())

    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")
