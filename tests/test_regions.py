"""근무지를 큰 지역 목록에서 고르는 것 테스트 (`app/regions.py`, 2026-09-21 결정).

모델은 부르지 않는다. 목록·근거 검사·스키마·프롬프트·검수 편집이 같은 목록을 쓰는지 본다.
"""

from __future__ import annotations

import sqlite3
from typing import get_args

from fastapi.testclient import TestClient

from app import regions
from app.classify.classifier import build_prompt
from app.classify.grounding import NOT_IN_LIST, ground
from app.classify.schema import (
    Classification,
    posting_model_of,
    validate_classification,
)
from tests.classify_fakes import response_body
from tests.test_ui_review_actions import client, conn, overrides  # noqa: F401

EXPECTED = (
    "전국",
    "서울",
    "경기",
    "인천",
    "부산",
    "대구",
    "광주",
    "대전",
    "울산",
    "세종",
    "강원",
    "경남",
    "경북",
    "전남",
    "전북",
    "충남",
    "충북",
    "제주",
    "해외",
)


def test_전국과_큰_지역_열여덟_개에서_고른다() -> None:
    """필터의 `전체` 는 선택지라 값이 아니다. 구·시·군도 아니다. `전국` 은 코드가 더한다."""
    assert regions.names() == EXPECTED


def test_여러_곳은_목록_순서로_잇고_겹친_것과_목록_밖은_버린다() -> None:
    """분류할 때마다 순서가 바뀌면 바뀐 것이 없는데 바뀐 것으로 보인다."""
    assert regions.join(["경기", "울산", "경기", "판교"]) == "경기, 울산"
    assert regions.split("경기, 울산") == ["경기", "울산"]
    assert regions.join([]) == ""


def test_전국을_고르면_전국_하나다() -> None:
    """`본사(양재동) / 전국 현장` 은 `서울, 전국` 이 아니다. 전국이 나머지를 다 포함한다."""
    assert regions.join(["서울", "전국"]) == "전국"
    assert ground({"region": "서울, 전국"}, "본문", "제목").fields["region"] == "전국"


def test_근거_검사는_목록_안_이름만_남긴다() -> None:
    kept = ground({"region": "충남, 울산, 경기"}, "본문", "제목")
    mixed = ground({"region": "울산, 분당(GRC)"}, "본문", "제목")

    assert kept.fields["region"] == "경기, 울산, 충남"
    assert kept.dropped == []
    # 하나가 틀렸다고 나머지를 버리지 않는다. 틀린 것이 있었다는 기록은 남는다
    assert mixed.fields["region"] == "울산"
    assert mixed.dropped == ["region"]
    assert mixed.reasons["region"] == NOT_IN_LIST


def test_근무지가_없는_공고는_빈_칸이다() -> None:
    assert ground({"region": ""}, "본문", "제목").fields["region"] == ""


def test_응답의_근무지는_목록으로도_옛_조각으로도_받는다() -> None:
    """예전 규칙판으로 돈 모델은 원문 조각으로 답한다. 그 글자를 읽고 근거 검사가 거른다."""
    listed = validate_classification(response_body(region=["서울", "경기"]))
    pieced = validate_classification(response_body(region=[{"line": 3, "text": "부산"}]))

    assert listed.postings[0].fields["region"] == "서울, 경기"
    assert pieced.postings[0].fields["region"] == "부산"


def test_응답_모델은_목록을_enum_으로_건다(conn: sqlite3.Connection) -> None:  # noqa: F811
    from app.classify.schema import build_classification_model

    posting = posting_model_of(build_classification_model(conn))
    (item,) = get_args(posting.model_fields["region"].annotation)

    assert get_args(item) == EXPECTED
    assert issubclass(build_classification_model(conn), Classification)


def test_프롬프트는_판정_칸_구역에_근무지_목록을_적는다() -> None:
    prompt, _ = build_prompt("근무지: 성남시 분당구", "백엔드 개발자")

    judge = prompt.split("# 판정하는 칸")[1].split("# 공고 제목")[0]
    assert "- region:" in judge
    assert "전국 / 서울 / 경기 / 인천" in judge
    assert "`근무지 : 본사(양재동) / 전국 현장` → `전국`" in judge
    assert "여러 개 고를 수 있다" in judge
    assert "`근무지: 성남시 분당구(판교)` → `경기`" in judge
    # 뽑는 칸 구역에는 없다
    extract = prompt.split("# 뽑는 칸")[1].split("# 판정하는 칸")[0]
    assert "- region:" not in extract


def test_검수에서_근무지를_목록_밖으로_고치면_막는다(
    client: TestClient,  # noqa: F811
    conn: sqlite3.Connection,  # noqa: F811
) -> None:
    html = client.post("/ui/review/jobs/1/edit", data={"region": "판교"}).text

    assert "지역 목록에 없다" in html
    assert overrides(conn) == {}


def test_옛_근무지가_남은_공고도_다른_칸은_고칠_수_있다(
    client: TestClient,  # noqa: F811
    conn: sqlite3.Connection,  # noqa: F811
) -> None:
    """2026-09-21 이전 공고는 원문 글자다. 근무지를 안 건드렸으면 막지 않는다."""
    conn.execute("UPDATE normalized_jobs SET region = '울산광역시 동구' WHERE id = 1")

    html = client.post(
        "/ui/review/jobs/1/edit",
        data={"region": "울산광역시 동구", "employment_type": "CONTRACT"},
    ).text

    assert "지역 목록에 없다" not in html
    assert overrides(conn) == {"employment_type": "CONTRACT"}
