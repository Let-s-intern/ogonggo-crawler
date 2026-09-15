"""대표 이미지 (2026-09-15 결정).

오공고 `Job.coverImageUrl` 로 보낼 대표 이미지는 회사 로고(자회사 → 모회사)가 먼저이고, 없으면
수집이 상세 페이지에서 읽은 `og:image` 다. 정규화가 정해 `normalized_jobs.cover_image_url` 에
저장한다.
실사이트에 나가지 않는다.

| 확인 | 깨지면 |
|---|---|
| 상세 페이지의 og:image 를 읽고 `data:` 는 뺀다 | 대표 이미지가 늘 비거나 박힌 아이콘이 나간다 |
| 원본 기록에 절대 주소로 싣고 해시에는 넣지 않는다 | 상대 주소가 나가거나 같은 공고가 또 쌓인다 |
| 자회사 로고 → 모회사 로고 → og:image 순으로 고른다 | 계열사 공고에 모회사 로고가 먼저 붙는다 |
| 사람이 고친 대표 이미지가 이긴다 | 검수에서 고친 값이 재정규화에 덮인다 |
| 회사 로고를 저장하면 그 회사 공고의 대표 이미지가 바로 바뀐다 | 옛 이미지로 오공고에 간다 |
| 0035 를 내리면 대표 이미지 보정·제안과 칸만 지운다 | 배포를 되돌리면 다른 보정이 사라진다 |
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from app import companies, db
from app.api.settings import get_connection
from app.crawler.hashing import content_hash
from app.crawler.parser import ListItem, og_image
from app.crawler.runner import _record
from app.main import app
from app.normalize.backfill import BackfillProgress, renormalize
from app.normalize.engine import insert_normalized

SAMSUNG_LOGO = "https://logo.test/samsung.png"
ELECTRO_LOGO = "https://logo.test/electro.png"


def detail(**fields: str) -> dict[str, str]:
    """`_record` 가 읽는 상세 칸. 주지 않은 칸은 빈 값이다."""
    base = {
        name: ""
        for name in (
            "title",
            "body",
            "qualifications",
            "recruitment_end_at",
            "department",
            "company_name",
        )
    }
    return {**base, **fields}


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    # 모회사는 크롤러가 정한다 (`app/normalize/engine.py` 의 `read_parent_company`)
    connection.execute(
        "INSERT INTO crawlers (id, name, list_url, status, default_company)"
        " VALUES (1, '삼성 채용', 'https://x', 'promoted', '삼성')"
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '삼성 채용')")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def client(tmp_path: pathlib.Path, conn: sqlite3.Connection) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(tmp_path / "jobs.db")
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[get_connection] = request_connection
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def add_job(conn: sqlite3.Connection, seq: int, company_name: str, og: str = "") -> tuple[int, int]:
    """공고 한 건을 정규화까지 넣는다. (수집 건 id, 정규화 행 id) 다."""
    record = {"source_url": f"https://x/{seq}", "title": f"공고 {seq}", "body": "본문"}
    record["company_name"] = company_name
    if og:
        record["og_image_url"] = og
    cursor = conn.execute(
        """
        INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash)
        VALUES (1, ?, ?, ?)
        """,
        (record["source_url"], json.dumps(record, ensure_ascii=False), f"hash-{seq}"),
    )
    raw_job_id = int(cursor.lastrowid or 0)
    return raw_job_id, insert_normalized(conn, raw_job_id, [])


def cover(conn: sqlite3.Connection, normalized_id: int) -> str | None:
    row = conn.execute(
        "SELECT cover_image_url FROM normalized_jobs WHERE id = ?", (normalized_id,)
    ).fetchone()
    return None if row["cover_image_url"] is None else str(row["cover_image_url"])


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        ('<meta property="og:image" content=" /img/cover.png ">', "/img/cover.png"),
        ('<meta name="og:image" content="https://cdn.test/a.png">', "https://cdn.test/a.png"),
        ('<meta property="og:image" content="data:image/png;base64,AAAA">', ""),
        ("<title>공고</title>", ""),
    ],
)
def test_상세_페이지의_og_image_를_읽는다(head: str, expected: str) -> None:
    soup = BeautifulSoup(f"<html><head>{head}</head><body></body></html>", "html.parser")

    assert og_image(soup) == expected


def test_원본_기록에_절대_주소로_싣고_해시에는_넣지_않는다() -> None:
    item = ListItem(index=0, title="공고", link="https://jobs.example.test/detail/7", date="")
    fields = detail(title="공고", body="본문")

    plain = _record(item, fields)
    relative = _record(item, fields, cover_image="/img/cover.png")
    script = _record(item, fields, cover_image="javascript:alert(1)")

    assert "og_image_url" not in plain
    assert relative["og_image_url"] == "https://jobs.example.test/img/cover.png"
    assert "og_image_url" not in script
    assert content_hash(relative) == content_hash(plain)


def test_자회사_로고_모회사_로고_og_image_순으로_고른다(conn: sqlite3.Connection) -> None:
    companies.ensure(conn, "삼성")
    companies.set_logo_url(conn, "삼성", SAMSUNG_LOGO)
    companies.ensure(conn, "삼성전기", "삼성")
    companies.set_logo_url(conn, "삼성전기", ELECTRO_LOGO)

    _, own_logo = add_job(conn, 1, "삼성전기", og="https://og.test/1.png")
    _, parent_logo = add_job(conn, 2, "삼성SDS", og="https://og.test/2.png")

    assert cover(conn, own_logo) == ELECTRO_LOGO
    assert cover(conn, parent_logo) == SAMSUNG_LOGO

    # 모회사 로고도 없으면 수집한 og:image 다. 그것도 없으면 비어 있다
    conn.execute("UPDATE crawlers SET default_company = '로고 없는 모회사'")
    _, from_page = add_job(conn, 3, "로고 없는 계열사", og="https://og.test/3.png")
    _, nothing = add_job(conn, 4, "로고 없는 계열사")

    assert cover(conn, from_page) == "https://og.test/3.png"
    assert cover(conn, nothing) is None


def test_사람이_고친_대표_이미지가_이긴다(conn: sqlite3.Connection) -> None:
    companies.ensure(conn, "삼성전기", "삼성")
    companies.set_logo_url(conn, "삼성전기", ELECTRO_LOGO)
    raw_job_id, normalized_id = add_job(conn, 1, "삼성전기")
    conn.execute(
        "INSERT INTO job_field_overrides (raw_job_id, field_name, value)"
        " VALUES (?, 'cover_image_url', 'https://human.test/cover.png')",
        (raw_job_id,),
    )

    renormalize(conn, BackfillProgress())

    assert cover(conn, normalized_id) == "https://human.test/cover.png"


def test_회사_로고를_저장하면_그_회사_공고의_대표_이미지가_바뀐다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _, electro = add_job(conn, 1, "삼성전기", og="https://og.test/1.png")
    _, sds = add_job(conn, 2, "삼성SDS")
    assert (cover(conn, electro), cover(conn, sds)) == ("https://og.test/1.png", None)

    body = client.put("/ui/companies/logo", data={"name": "삼성", "logo_url": SAMSUNG_LOGO}).text

    assert "공고 2건을 다시 정규화해 대표 이미지를 바꿨다" in body
    # 두 계열사 모두 자기 로고가 없어 모회사 로고가 og:image 보다 먼저다
    assert (cover(conn, electro), cover(conn, sds)) == (SAMSUNG_LOGO, SAMSUNG_LOGO)


def test_0035_를_내리면_대표_이미지_보정과_칸만_지운다(conn: sqlite3.Connection) -> None:
    raw_job_id, _ = add_job(conn, 1, "삼성전기")
    conn.execute(
        "INSERT INTO job_field_overrides (raw_job_id, field_name, value)"
        " VALUES (?, 'cover_image_url', 'https://human.test/cover.png'), (?, 'title', '고친 제목')",
        (raw_job_id, raw_job_id),
    )
    conn.execute(
        "INSERT INTO job_field_suggestions (raw_job_id, field_name, value)"
        " VALUES (?, 'cover_image_url', 'https://suggested.test/cover.png')",
        (raw_job_id,),
    )
    applied = db.applied_versions(conn)

    db.migrate_down(conn, steps=len(applied) - applied.index("0035"))

    overrides = conn.execute("SELECT field_name, value FROM job_field_overrides").fetchall()
    assert [tuple(row) for row in overrides] == [("title", "고친 제목")]
    assert conn.execute("SELECT count(*) FROM job_field_suggestions").fetchone()[0] == 0
    columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(normalized_jobs)")}
    assert "cover_image_url" not in columns
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO job_field_overrides (raw_job_id, field_name, value)"
            " VALUES (?, 'cover_image_url', '값')",
            (raw_job_id,),
        )
