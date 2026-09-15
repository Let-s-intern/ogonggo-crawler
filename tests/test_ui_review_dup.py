"""공고 목록의 중복 찾기 (dup-filter 1.1).

실사이트에 나가지 않는다. 저장된 행을 넣고 조건 함수와 화면 경로로만 확인한다.

픽스처는 운영 DB 640건을 손으로 센 결과와 같은 모양이다. 제목만 같은 것이 7묶음 22건
(여분 15), 제목과 회사가 함께 같은 것이 1묶음 5건(여분 4)이다. 계열사가 나눠 올린
`R&D분야 외국인 경력사원 채용` 7건은 회사가 전부 달라 제목 기준에만 걸린다.

| 확인 | 깨지면 |
|---|---|
| 기준마다 묶음 수와 여분 수가 손으로 센 값과 같다 | 화면에 적힌 중복 건수가 거짓이 된다 |
| 여분이 아니라 묶음 전체가 걸린다 | 짝이 안 보여 어느 쪽을 지울지 정할 수 없다 |
| 다른 조건과 AND 로 겹친다 | `SK 안에서만 중복 찾기` 가 전체 중복을 보여준다 |
| 중복은 좁힌 조건 안에서 다시 센다 | 좁힌 뒤 짝을 잃은 한 건이 중복으로 남는다 |
| 빈 값끼리는 묶이지 않는다 | 셀렉터가 놓친 40건이 `중복 40건` 으로 읽힌다 |
| 같은 묶음이 붙어 나온다 | 페이지를 넘기면 짝이 다른 페이지로 갈라진다 |
| 중복으로 거른 뒤 한 건만 골라 지울 수 있다 | 중복을 찾아도 손댈 방법이 없다 |
"""

from __future__ import annotations

import pathlib
import re
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import crawlers as crawlers_api
from app.api.review_filter import (
    DUP_SOURCE_URL,
    DUP_TITLE,
    DUP_TITLE_COMPANY,
    JobFilter,
    count,
    dup_columns,
    dup_groups,
    filter_sql,
    order_clause,
)
from app.main import app

LIST_URL = "https://www.python.org/jobs/"

# 제목이 같고 회사가 다른 7건. 삼성 계열사가 각각 올린 공고다
SAMSUNG = "R&D분야 외국인 경력사원 채용"
SAMSUNG_COMPANIES = (
    "삼성중공업",
    "삼성바이오에피스",
    "삼성바이오로직스",
    "삼성SDS",
    "삼성전기",
    "삼성SDI",
    "삼성디스플레이",
)

# 제목도 회사도 같은 5건. 제목+회사 기준에 걸리는 유일한 묶음이다
POOL = "상시 인재 Pool 등록"
POOL_COMPANY = "D&O"

# 제목만 같은 두 건짜리 다섯 묶음. 회사는 서로 다르다
PAIRS = (
    "Business Operations Manager",
    "IT Manager",
    "기술연구분야 외국인 경력사원 채용",
    "설계/시공분야 외국인 계약직 채용",
    "해외영업분야 외국인 경력사원 채용",
)

# 어느 기준에도 걸리지 않는 행
SINGLES = (
    ("에스케이하이닉스", "백엔드 개발자"),
    ("에스케이텔레콤", "데이터 엔지니어"),
    ("엘지전자", "iOS 개발자"),
)


def _rows() -> list[tuple[int, str, str]]:
    """(raw_job_id, company_name, title) 한 벌. 워크플로우는 회사 첫 글자로 갈린다."""
    found: list[tuple[int, str, str]] = []
    for company_name in SAMSUNG_COMPANIES:
        found.append((len(found) + 1, company_name, SAMSUNG))
    for _ in range(5):
        found.append((len(found) + 1, POOL_COMPANY, POOL))
    for title in PAIRS:
        found.append((len(found) + 1, f"한화{title[:2]}", title))
        found.append((len(found) + 1, f"한화솔루션{title[:2]}", title))
    for company_name, title in SINGLES:
        found.append((len(found) + 1, company_name, title))
    # 제목이 비어 있는 두 건. 셀렉터가 놓친 것이지 중복이 아니다
    found.append((len(found) + 1, "에스케이온", ""))
    found.append((len(found) + 1, "에스케이스퀘어", "   "))
    return found


ROWS = _rows()


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES ('samsung', ?, 'promoted')",
        (LIST_URL,),
    )
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES ('sk', ?, 'promoted')",
        ("https://example.com/jobs/",),
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '대기업')")
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (2, 'SK')")
    for raw_job_id, company_name, title in ROWS:
        workflow_id = 2 if company_name.startswith("에스케이") else 1
        connection.execute(
            """
            INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash,
                                  crawled_at)
            VALUES (?, ?, ?, '{}', ?, '2026-08-20 01:00:00')
            """,
            (raw_job_id, workflow_id, f"{LIST_URL}{raw_job_id}/", f"hash-{raw_job_id}"),
        )
        connection.execute(
            """
            INSERT INTO normalized_jobs (raw_job_id, company_name, title, source_url, normalized_at)
            VALUES (?, ?, ?, ?, '2026-08-20 02:00:00')
            """,
            (raw_job_id, company_name, title, f"{LIST_URL}{raw_job_id}/"),
        )
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

    app.dependency_overrides[crawlers_api.get_connection] = request_connection
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def measured(conn: sqlite3.Connection, picked: JobFilter) -> tuple[int, int, int]:
    """(묶음 수, 걸린 건수, 여분). 화면 머리글에 적히는 세 숫자와 같은 계산이다."""
    groups = dup_groups(conn, picked)
    total = count(conn, picked)
    return len(groups), total, total - len(groups)


def test_기준마다_묶음_수가_손으로_센_값과_같다(conn: sqlite3.Connection) -> None:
    assert measured(conn, JobFilter(dup=DUP_TITLE_COMPANY)) == (1, 5, 4)
    assert measured(conn, JobFilter(dup=DUP_TITLE)) == (7, 22, 15)
    # 원본 주소가 겹치는 것은 중복 판정이 고장 났을 때만 나온다. 지금은 0이다
    assert measured(conn, JobFilter(dup=DUP_SOURCE_URL)) == (0, 0, 0)


def test_조건을_고르지_않으면_전부_나온다(conn: sqlite3.Connection) -> None:
    assert count(conn, JobFilter()) == len(ROWS)
    assert dup_groups(conn, JobFilter()) == []


def test_여분이_아니라_묶음_전체가_걸린다(conn: sqlite3.Connection) -> None:
    groups = dup_groups(conn, JobFilter(dup=DUP_TITLE))
    assert [group["count"] for group in groups] == [7, 5, 2, 2, 2, 2, 2]
    assert groups[0]["parts"] == [("제목", SAMSUNG)]
    assert groups[0]["number"] == 1


def test_제목이_같아도_회사가_다르면_좁은_기준에는_안_걸린다(conn: sqlite3.Connection) -> None:
    groups = dup_groups(conn, JobFilter(dup=DUP_TITLE_COMPANY))
    assert [group["parts"] for group in groups] == [[("제목", POOL), ("회사", POOL_COMPANY)]]


def test_다른_조건과_함께_걸면_그_안에서만_센다(conn: sqlite3.Connection) -> None:
    """`SK 안에서만 중복 찾기` 다. SK 워크플로우에는 짝이 없다."""
    assert measured(conn, JobFilter(dup=DUP_TITLE, workflow_id=2)) == (0, 0, 0)
    assert measured(conn, JobFilter(dup=DUP_TITLE, workflow_id=1)) == (7, 22, 15)
    assert measured(conn, JobFilter(dup=DUP_TITLE, query=POOL_COMPANY)) == (1, 5, 4)


def test_좁힌_뒤에_짝을_잃은_한_건은_중복이_아니다(conn: sqlite3.Connection) -> None:
    """전체에서 센 묶음을 나중에 거르면 이 한 건이 `중복` 으로 남는다."""
    assert measured(conn, JobFilter(dup=DUP_TITLE, query="삼성SDI")) == (0, 0, 0)


def test_빈_값끼리는_묶지_않는다(conn: sqlite3.Connection) -> None:
    titles = {
        value for group in dup_groups(conn, JobFilter(dup=DUP_TITLE)) for _, value in group["parts"]
    }
    assert "" not in titles
    assert count(conn, JobFilter(dup=DUP_TITLE)) == 22


def test_같은_묶음이_붙어_나오고_큰_묶음이_먼저다(conn: sqlite3.Connection) -> None:
    picked = JobFilter(dup=DUP_TITLE)
    where, params = filter_sql(picked)
    rows = conn.execute(
        f"SELECT n.title AS title{dup_columns(picked.dup)}"
        f"  FROM normalized_jobs n JOIN raw_jobs r ON r.id = n.raw_job_id{where}"
        f"{order_clause(picked.dup)}",
        params,
    ).fetchall()
    sizes = [int(row["dup_size"]) for row in rows]
    assert sizes[:12] == [7] * 7 + [5] * 5
    titles = [str(row["title"]) for row in rows]
    assert titles[:7] == [SAMSUNG] * 7
    assert len(set(titles)) == 7


def test_표에_없는_기준은_조건을_걸지_않는다(conn: sqlite3.Connection) -> None:
    """화면에서 온 문자열이 SQL 로 새지 않는다."""
    where, params = filter_sql(JobFilter(dup="'; DROP TABLE raw_jobs; --"))
    assert where == ""
    assert params == []


# 표의 묶음 칸. `3번 묶음 · 2건` 처럼 번호와 그 묶음의 건수를 함께 적는다
GROUP_CELL = re.compile(r"(\d+)번 묶음 · (\d+)건")


def test_화면이_묶음_수와_여분을_숫자로_적는다(client: TestClient) -> None:
    html = client.get("/ui/review", params={"dup": DUP_TITLE}).text
    assert "중복 묶음 (제목 기준)" in html
    assert "7묶음 22건" in html
    assert "15건이" in html and "여분이다" in html


def test_표의_행마다_묶음_번호와_건수가_붙는다(client: TestClient) -> None:
    """한 페이지는 20건이다. 큰 묶음이 먼저 오고 같은 묶음이 붙어 있다."""
    found = GROUP_CELL.findall(client.get("/ui/review", params={"dup": DUP_TITLE}).text)
    assert len(found) == 20
    assert found[:7] == [("1", "7")] * 7
    assert found[7:12] == [("2", "5")] * 5


def test_페이지를_넘겨도_중복_조건이_유지된다(client: TestClient) -> None:
    """페이지 버튼의 주소는 서버가 지금 조건을 달아 만든다."""
    html = client.get("/ui/review", params={"dup": DUP_TITLE}).text
    assert "dup=title" in html
    second = client.get("/ui/review", params={"dup": DUP_TITLE, "page": "2"}).text
    assert "21-22번째" in second and "2 / 2 페이지" in second
    assert "7묶음 22건" in second


def test_중복이_없으면_무엇을_하면_되는지_적는다(client: TestClient) -> None:
    html = client.get("/ui/review", params={"dup": DUP_SOURCE_URL}).text
    assert "0묶음 0건" in html
    assert "원본 주소 기준으로 겹치는 공고가 없다" in html


def test_중복_조건을_안_걸면_묶음_칸이_없다(client: TestClient) -> None:
    html = client.get("/ui/review").text
    assert "중복 묶음" not in html
    assert GROUP_CELL.search(html) is None


def test_걸린_전부_고르기가_묶음_전체임을_적는다(client: TestClient) -> None:
    """여분만 걸린 줄 알고 켜면 짝까지 사라진다. 한 건도 남지 않는다."""
    html = client.get("/ui/review", params={"dup": DUP_TITLE}).text
    assert "중복 조건에 걸린 22건은 묶음 전체다" in html
    assert "여분 15건이 아니라 짝까지 전부 지워진다" in html


def test_필터_폼에_중복_기준이_모두_있다(client: TestClient) -> None:
    html = client.get("/ui/review/filters").text
    assert 'name="dup"' in html
    for value, label in (
        (DUP_TITLE_COMPANY, "제목 + 회사"),
        (DUP_TITLE, "제목"),
        (DUP_SOURCE_URL, "원본 주소"),
    ):
        assert f'value="{value}"' in html
        assert label in html


def deleted_ids(client: TestClient, ids: list[str], **params: str) -> dict[str, str]:
    """확인 창을 지나 실제로 지운다. 화면이 밟는 두 요청을 그대로 밟는다."""
    form = {"scope": "selected", "raw_job_id": ids, **params}
    confirm = client.post("/ui/review/delete/confirm", data=form)
    assert confirm.status_code == 200
    done = client.post("/ui/review/delete", data=form)
    assert done.status_code == 200
    return {"confirm": confirm.text, "done": done.text}


def test_중복으로_거른_뒤_한_건만_골라_지운다(client: TestClient, conn: sqlite3.Connection) -> None:
    """실제 쓰임이다. 묶음을 보고 어느 쪽을 남길지 정한 뒤 그것만 지운다."""
    pair = conn.execute(
        "SELECT raw_job_id FROM normalized_jobs WHERE title = ? ORDER BY raw_job_id",
        (PAIRS[1],),
    ).fetchall()
    assert len(pair) == 2
    goes, stays = int(pair[0]["raw_job_id"]), int(pair[1]["raw_job_id"])

    html = deleted_ids(client, [str(goes)], dup=DUP_TITLE)
    assert "중복 제목" in html["confirm"]
    assert "지웠다" in html["done"]

    left = conn.execute(
        "SELECT raw_job_id FROM normalized_jobs WHERE title = ?", (PAIRS[1],)
    ).fetchall()
    assert [int(row["raw_job_id"]) for row in left] == [stays]
    assert conn.execute("SELECT count(*) FROM raw_jobs WHERE id = ?", (goes,)).fetchone()[0] == 0
    # 짝을 잃은 한 건은 더 이상 중복이 아니다. 그 묶음이 통째로 빠진다
    assert measured(conn, JobFilter(dup=DUP_TITLE)) == (6, 20, 14)


def test_조건_전체_지우기는_묶음_전체를_대상으로_한다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    confirm = client.post("/ui/review/delete/confirm", data={"all_filtered": "1", "dup": DUP_TITLE})
    assert confirm.status_code == 200
    assert "22건" in confirm.text
    assert "중복 제목" in confirm.text
    # 확인만 하고 지우지 않는다
    assert count(conn, JobFilter()) == len(ROWS)


def _add_row(
    conn: sqlite3.Connection, raw_job_id: int, parent: str, company_name: str | None, title: str
) -> None:
    """자회사가 빈 행을 하나 더한다."""
    conn.execute(
        """
        INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash,
                              crawled_at)
        VALUES (?, 1, ?, '{}', ?, '2026-08-20 01:00:00')
        """,
        (raw_job_id, f"{LIST_URL}{raw_job_id}/", f"hash-{raw_job_id}"),
    )
    conn.execute(
        """
        INSERT INTO normalized_jobs (raw_job_id, parent_company_name, company_name, title,
                                     source_url, normalized_at)
        VALUES (?, ?, ?, ?, ?, '2026-08-20 02:00:00')
        """,
        (raw_job_id, parent, company_name, title, f"{LIST_URL}{raw_job_id}/"),
    )


def test_자회사가_비면_제목과_회사_기준이_모회사를_본다(conn: sqlite3.Connection) -> None:
    """회사명을 주지 않는 사이트의 중복이 이 기준에서 통째로 빠지지 않게 한다."""
    before = measured(conn, JobFilter(dup=DUP_TITLE_COMPANY))
    _add_row(conn, 100, "토스", None, "프론트엔드 개발자")
    _add_row(conn, 101, "토스", None, "프론트엔드 개발자")
    # 모회사가 다르면 같은 제목이어도 묶이지 않는다
    _add_row(conn, 102, "우아한형제들", None, "안드로이드 개발자")
    _add_row(conn, 103, "당근", None, "안드로이드 개발자")

    assert before == (1, 5, 4)
    assert measured(conn, JobFilter(dup=DUP_TITLE_COMPANY)) == (2, 7, 5)
