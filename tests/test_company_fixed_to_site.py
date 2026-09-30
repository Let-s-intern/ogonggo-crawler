"""공고의 회사명은 사이트마다 한 가지 방법으로 정한다 (2026-09-30 결정, 0048).

공고에서 읽은 회사 이름은 수집할 때마다 조금씩 달랐다 — 채널톡 사이트의 공고가 `채널톡` 과
`채널코퍼레이션` 으로 갈려 오공고에 같은 회사가 두 이름으로 쌓였다. 회사가 하나인 사이트(기본)는
사이트 추가에 넣은 이름으로 고정하고, 그룹 채용 사이트만 공고에서 읽은 계열사를 쓴다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import companies, db
from app.deliver import spring
from app.normalize.engine import insert_normalized


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (id, name, list_url, status, default_company)"
        " VALUES (1, '채널톡 채용', 'https://x', 'promoted', '채널톡')"
    )
    connection.execute(
        "INSERT INTO crawlers (id, name, list_url, status, default_company, has_affiliates)"
        " VALUES (2, '삼성 채용', 'https://y', 'promoted', '삼성', 1)"
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '채널톡')")
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (2, 2, '삼성')")
    try:
        yield connection
    finally:
        connection.close()


def add_job(conn: sqlite3.Connection, workflow_id: int, company_name: str) -> sqlite3.Row:
    """공고 하나를 넣고 정규화한 행을 돌려준다."""
    count = conn.execute("SELECT count(*) FROM raw_jobs").fetchone()[0]
    url = f"https://x/{count + 1}"
    raw = {"source_url": url, "title": "백엔드", "body": "본문", "company_name": company_name}
    cursor = conn.execute(
        "INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash)"
        " VALUES (?, ?, ?, ?)",
        (workflow_id, url, json.dumps(raw, ensure_ascii=False), f"hash{count}"),
    )
    insert_normalized(conn, int(cursor.lastrowid or 0), [])
    return conn.execute("SELECT * FROM normalized_jobs WHERE source_url = ?", (url,)).fetchone()


def test_회사가_하나인_사이트는_공고에_뭐라고_적혀_있든_사이트_이름으로_나간다(
    conn: sqlite3.Connection,
) -> None:
    sent = [spring.payload(add_job(conn, 1, name)) for name in ("채널톡", "채널코퍼레이션", "")]

    assert [(body["companyName"], body["parentCompanyName"]) for body in sent] == [
        ("채널톡", None)
    ] * 3
    # 공고에서 읽은 이름으로 회사 행이 늘지 않는다
    assert [company.name for company in companies.list_all(conn)] == ["채널톡"]


def test_그룹_채용_사이트는_계열사가_회사명이고_표기만_다른_이름은_하나로_모은다(
    conn: sqlite3.Connection,
) -> None:
    names = ("삼성전기", "삼성전기(주)", "주식회사 삼성전기", "삼성 전기", "삼성SDS")

    sent = [spring.payload(add_job(conn, 2, name)) for name in names]

    assert [body["companyName"] for body in sent] == ["삼성전기"] * 4 + ["삼성SDS"]
    assert {body["parentCompanyName"] for body in sent} == {"삼성"}
    assert sorted(company.name for company in companies.list_all(conn)) == [
        "삼성",
        "삼성SDS",
        "삼성전기",
    ]


def test_법인_표기가_붙은_이름이_먼저_있어도_표기_없는_이름으로_모은다(
    conn: sqlite3.Connection,
) -> None:
    first = add_job(conn, 2, "삼성전기(주)")
    second = add_job(conn, 2, "삼성전기")
    third = add_job(conn, 2, "㈜삼성전기")

    assert [row["company_name"] for row in (first, second, third)] == [
        "삼성전기(주)",
        "삼성전기",
        "삼성전기",
    ]


def test_그룹_사이트에서_계열사를_못_읽으면_그룹_이름이_회사명이다(
    conn: sqlite3.Connection,
) -> None:
    body = spring.payload(add_job(conn, 2, ""))

    assert (body["companyName"], body["parentCompanyName"]) == ("삼성", None)


def test_다른_사이트의_비슷한_이름과는_섞지_않는다(conn: sqlite3.Connection) -> None:
    companies.ensure(conn, "삼성전기(주)", "다른 그룹")

    assert companies.canonical(conn, "삼성전기", "삼성") == "삼성전기"
