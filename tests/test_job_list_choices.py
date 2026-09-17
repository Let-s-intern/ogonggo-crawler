"""직군·직무·산업은 설정에 넣어 둔 목록 안에서만 고른다 (2026-09-17 결정).

| 확인 | 깨지면 |
|---|---|
| 공고 수정 화면에서 세 칸이 목록에서 고르는 칸이다 | 아무 글자나 적어 목록 밖 값이 된다 |
| 목록 밖 직군·산업, 직군 아래에 없는 직무는 저장하지 않는다 | 목록 밖 값이 보정으로 굳는다 |
| AI 가 다른 직군의 직무를 고르면 버린다 | 직군과 직무가 어긋난 채 오공고로 간다 |
"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app import industries, taxonomy
from app.classify.classifier import _taxonomy_choices
from app.classify.grounding import NOT_IN_LIST, ground
from tests.test_ui_review_actions import client, conn, overrides  # noqa: F401

TREE = (("IT·개발", ("서버·백엔드", "프론트엔드")), ("영업", ("B2B영업",)))


def seed(conn: sqlite3.Connection) -> None:  # noqa: F811
    for order, (major, minors) in enumerate(TREE):
        node = taxonomy.create(conn, parent_id=None, name=major, sort_order=order)
        for minor in minors:
            taxonomy.create(conn, parent_id=node.id, name=minor)
    industries.create(conn, name="IT·정보통신업")


def test_수정_화면에서_세_칸은_목록에서_고른다(
    client: TestClient,  # noqa: F811
    conn: sqlite3.Connection,  # noqa: F811
) -> None:
    seed(conn)

    html = client.get("/ui/review/jobs/1/edit").text

    assert '<select name="job_field"' in html and '<select name="job_role"' in html
    assert '<select name="industry"' in html
    assert '<optgroup label="IT·개발">' in html
    assert '<option value="B2B영업"' in html
    assert '<input type="text" name="job_field"' not in html


def test_목록_밖_값과_직군_아래에_없는_직무는_저장하지_않는다(
    client: TestClient,  # noqa: F811
    conn: sqlite3.Connection,  # noqa: F811
) -> None:
    seed(conn)

    outside = client.post("/ui/review/jobs/1/edit", data={"job_field": "우주항공"}).text
    mismatch = client.post(
        "/ui/review/jobs/1/edit", data={"job_field": "영업", "job_role": "서버·백엔드"}
    ).text
    industry = client.post("/ui/review/jobs/1/edit", data={"industry": "없는산업"}).text
    good = client.post(
        "/ui/review/jobs/1/edit",
        data={"job_field": "IT·개발", "job_role": "서버·백엔드", "industry": "IT·정보통신업"},
    ).text

    assert "직무 분류에 없다" in outside
    assert "아래에 없다" in mismatch
    assert "산업 분류에 없다" in industry
    assert "3칸을 고쳤다" in good
    assert overrides(conn) == {
        "job_field": "IT·개발",
        "job_role": "서버·백엔드",
        "industry": "IT·정보통신업",
    }


def test_AI_가_다른_직군의_직무를_고르면_버린다() -> None:
    choices = _taxonomy_choices(TREE, ("IT·정보통신업",))
    assert choices is not None

    grounded = ground(
        {"job_field": "영업", "job_role": "서버·백엔드", "industry": "IT·정보통신업"},
        "본문",
        "제목",
        taxonomy_choices=choices,
    )
    kept = ground(
        {"job_field": "IT·개발", "job_role": "서버·백엔드", "industry": "IT·정보통신업"},
        "본문",
        "제목",
        taxonomy_choices=choices,
    )

    assert grounded.fields["job_field"] == "영업"
    assert grounded.fields["job_role"] == ""
    assert grounded.reasons["job_role"] == NOT_IN_LIST
    assert kept.fields["job_role"] == "서버·백엔드"
