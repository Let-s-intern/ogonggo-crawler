"""분류가 무엇을 대상으로 도는가. 범위 넷을 본다.

2.1.V ~ 2.5.V 다.

범위는 `side_workflows.target_scope` 에 저장된 낱말 하나이고, 그것이 실제로 어느 공고를
고르는지가 여기서 갈린다. 대상이 틀리면 화면이 적은 건수와 실제로 나가는 토큰이 다르다.

**네 범위가 "보낼 글이 있다" 를 같은 조건으로 본다.** 그 조건은 `app/classify/store.py` 의
`_CLASSIFY_TEXT` 하나이고, 원문이 있으면 원문, 없으면 본문이다. 범위마다 다시 쓰면 같은
공고가 어느 범위에서는 대상이고 어느 범위에서는 아니게 된다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db
from app.classify.schema import CLASSIFY_FIELDS
from app.classify.store import (
    ALL,
    CLASSIFY_SCOPES,
    EMPTY_FIELDS,
    RECENT,
    UNCLASSIFIED,
    ClassifyScopeError,
    pending_count,
    pending_ids,
    scope_count,
    scope_ids,
)


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (id, name, list_url, selectors_json) VALUES (1, '테스트', 'x', '{}')"
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '테스트')")
    try:
        yield connection
    finally:
        connection.close()


def add_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    body: str | None = "본문",
    source_text: str | None = None,
    crawled_at: str | None = None,
) -> None:
    """수집된 공고 하나. `body` 가 None 이면 키가 아예 없다 — 옛 건의 모양이다."""
    raw: dict[str, str] = {"title": f"공고 {job_id}"}
    if body is not None:
        raw["body"] = body
    if source_text is not None:
        raw["source_text"] = source_text
    conn.execute(
        """
        INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash, crawled_at)
        VALUES (?, 1, ?, ?, ?, coalesce(?, datetime('now')))
        """,
        (
            job_id,
            f"https://example.com/{job_id}",
            json.dumps(raw, ensure_ascii=False),
            f"hash{job_id}",
            crawled_at,
        ),
    )


def classify(conn: sqlite3.Connection, job_id: int, **fields: str) -> None:
    """분류 행 하나. 칸을 주지 않으면 전부 빈 분류다."""
    columns = ", ".join(fields)
    values = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT INTO job_classifications (raw_job_id, model{', ' + columns if fields else ''})"
        f" VALUES (?, '테스트'{', ' + values if fields else ''})",
        (job_id, *fields.values()),
    )


def test_범위_이름이_저장되는_값과_같다() -> None:
    """이름을 두 벌 두면 저장은 되는데 아무것도 고르지 못하는 워크플로우가 생긴다."""
    from app.side.store import CLASSIFY, SCOPES

    assert SCOPES[CLASSIFY] == CLASSIFY_SCOPES


def test_분류되지_않은_건만_고른다(conn: sqlite3.Connection) -> None:
    add_job(conn, 1)
    add_job(conn, 2)
    add_job(conn, 3)
    classify(conn, 2)

    assert scope_ids(conn, UNCLASSIFIED) == [3, 1]


def test_분류가_전부_빈_행이어도_분류된_것이다(conn: sqlite3.Connection) -> None:
    """행이 있으면 돈 것이다. 아무것도 안 나온 것과 아직 안 돈 것은 다르다."""
    add_job(conn, 1)
    classify(conn, 1)

    assert scope_ids(conn, UNCLASSIFIED) == []


def test_지금_도는_조회에_연결돼_있다(conn: sqlite3.Connection) -> None:
    """`unclassified` 는 새 조회가 아니라 `pending_ids` 그대로다.

    갈리면 화면에서 고른 범위와 지금 도는 분류가 서로 다른 공고를 집는다.
    """
    add_job(conn, 1)
    add_job(conn, 2, body="", source_text="원문만 있는 건")
    add_job(conn, 3, body=None)
    classify(conn, 1)

    assert scope_ids(conn, UNCLASSIFIED) == pending_ids(conn)
    assert scope_ids(conn, UNCLASSIFIED, limit=1) == pending_ids(conn, limit=1)


def test_상한이_걸리면_최근_것부터_자른다(conn: sqlite3.Connection) -> None:
    for job_id in (1, 2, 3):
        add_job(conn, job_id)

    assert scope_ids(conn, UNCLASSIFIED, limit=2) == [3, 2]


def test_모르는_범위는_사유와_함께_거절된다(conn: sqlite3.Connection) -> None:
    with pytest.raises(ClassifyScopeError) as caught:
        scope_ids(conn, "everything")

    assert "everything" in str(caught.value)
    assert UNCLASSIFIED in str(caught.value)


def test_칸이_전부_빈_분류만_고른다(conn: sqlite3.Connection) -> None:
    add_job(conn, 1)
    add_job(conn, 2)
    add_job(conn, 3)
    classify(conn, 1)
    classify(conn, 2, responsibilities="제휴사 데이터 연동 구조 기획")

    assert scope_ids(conn, EMPTY_FIELDS) == [1]


def test_분류_행이_없으면_대상이_아니다(conn: sqlite3.Connection) -> None:
    """아직 안 돈 것은 `unclassified` 가 가져간다. 두 범위가 같은 건을 집으면 안 된다."""
    add_job(conn, 1)

    assert scope_ids(conn, EMPTY_FIELDS) == []
    assert scope_ids(conn, UNCLASSIFIED) == [1]


@pytest.mark.parametrize("field", CLASSIFY_FIELDS)
def test_한_칸이라도_차_있으면_빠진다(conn: sqlite3.Connection, field: str) -> None:
    """칸 목록은 `app/classify/schema.py` 에서 읽는다. 열한 칸이 아니고, 또 바뀐다."""
    add_job(conn, 1)
    classify(conn, 1, **{field: "값"})

    assert scope_ids(conn, EMPTY_FIELDS) == []


def at(conn: sqlite3.Connection, *modifiers: str) -> str:
    """DB 가 계산한 수집 시각. 파이썬이 따로 계산하면 조회의 기준과 어긋난다.

    수정자는 하나씩 넘긴다. `'-7 days, +1 minutes'` 처럼 붙여 쓰면 SQLite 가 NULL 을
    돌려주고, 그 NULL 이 조용히 지금 시각이 되어 검사가 통과해 버린다.
    """
    holes = ", ".join("?" for _ in modifiers)
    row = conn.execute(f"SELECT datetime('now', {holes}) AS at", modifiers).fetchone()
    assert row["at"] is not None, modifiers
    return str(row["at"])


def test_경계일_앞뒤로_갈린다(conn: sqlite3.Connection) -> None:
    """7일이면 7일 전 직후는 들어오고 직전은 빠진다."""
    add_job(conn, 1, crawled_at=at(conn, "-7 days", "+1 minutes"))
    add_job(conn, 2, crawled_at=at(conn, "-7 days", "-1 minutes"))

    assert scope_ids(conn, RECENT, days=7) == [1]


def test_이미_분류된_건도_들어간다(conn: sqlite3.Connection) -> None:
    """다시 분류하는 범위다. 분류 여부를 보지 않는다."""
    add_job(conn, 1)
    add_job(conn, 2)
    classify(conn, 1, responsibilities="업무")

    assert scope_ids(conn, RECENT, days=1) == [2, 1]


def test_일수가_없으면_거절된다(conn: sqlite3.Connection) -> None:
    """며칠인지 모르는 `recent` 는 대상을 정할 수 없다. 기본값을 지어내지 않는다."""
    for days in (None, 0):
        with pytest.raises(ClassifyScopeError):
            scope_ids(conn, RECENT, days=days)


def test_전부는_이미_분류된_것도_포함한다(conn: sqlite3.Connection) -> None:
    add_job(conn, 1)
    add_job(conn, 2, body="", source_text="원문만 있는 건")
    add_job(conn, 3, crawled_at=at(conn, "-400 days"))
    classify(conn, 1, responsibilities="업무")

    assert scope_ids(conn, ALL) == [3, 2, 1]


@pytest.mark.parametrize("scope", CLASSIFY_SCOPES)
def test_보낼_글이_없으면_어느_범위에도_없다(conn: sqlite3.Connection, scope: str) -> None:
    """부를 이유가 없는 호출을 만들지 않는다. 그 호출은 실패 건수만 부풀린다.

    2번은 분류 행이 있고 칸이 전부 비어 있다. 글만 있었다면 `empty_fields` 의 대상이다.
    """
    add_job(conn, 1, body=None)
    add_job(conn, 2, body="")
    add_job(conn, 3, body="", source_text="")
    classify(conn, 2)

    assert scope_ids(conn, scope, days=365) == []


def populate(conn: sqlite3.Connection) -> None:
    """범위 넷이 서로 다른 건을 집도록 섞어 넣는다."""
    add_job(conn, 1)
    add_job(conn, 2)
    add_job(conn, 3)
    add_job(conn, 4, body="", source_text="원문만 있는 건")
    add_job(conn, 5, body=None)
    add_job(conn, 6, crawled_at=at(conn, "-400 days"))
    classify(conn, 2)
    classify(conn, 3, responsibilities="업무")


@pytest.mark.parametrize("scope", CLASSIFY_SCOPES)
def test_건수가_id_목록_길이와_같다(conn: sqlite3.Connection, scope: str) -> None:
    """확인 창이 적는 숫자와 실제로 도는 건수가 갈리면 어느 쪽이 맞는지 알 수 없다."""
    populate(conn)

    ids = scope_ids(conn, scope, days=30)
    assert ids, "범위가 아무것도 집지 못하면 검사가 헛돈다"
    assert scope_count(conn, scope, days=30) == len(ids)


def test_상한은_건수를_깎지_않는다(conn: sqlite3.Connection) -> None:
    """확인 창이 묻는 것은 범위가 몇 건이냐이고, 몇 건씩 끊어 도는지는 실행이 정한다."""
    populate(conn)

    assert scope_count(conn, ALL) == 5
    assert scope_ids(conn, ALL, limit=2) == [6, 4]


def test_남은_건수도_같은_조회를_지난다(conn: sqlite3.Connection) -> None:
    populate(conn)

    assert pending_count(conn) == scope_count(conn, UNCLASSIFIED)
    assert pending_count(conn) == len(pending_ids(conn))


def test_건수도_범위를_검증한다(conn: sqlite3.Connection) -> None:
    with pytest.raises(ClassifyScopeError):
        scope_count(conn, "everything")
    with pytest.raises(ClassifyScopeError):
        scope_count(conn, RECENT)
