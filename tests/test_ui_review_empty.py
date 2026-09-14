"""값이 빈 공고를 찾는 조건 (30.3).

실사이트에 나가지 않는다. 저장된 행을 넣고 화면 경로로만 조회한다.

셀렉터가 한 필드만 놓치면 나머지 다섯은 멀쩡하게 들어온다. 그 건은 목록에서 정상으로 보이고,
찾는 방법은 전체를 눈으로 훑는 것밖에 없다. 이 조건이 그 자리를 대신한다.

건수가 정확해야 하는 이유는 그 숫자를 보고 셀렉터를 고치러 가기 때문이다. `마감 148건이
비었다` 가 실제로는 상시채용 148건이면, 멀쩡한 셀렉터를 고치는 데 시간을 쓴다.

| 확인 | 깨지면 |
|---|---|
| 필드별 빈 건수가 직접 센 수와 같다 | 그 숫자를 보고 엉뚱한 셀렉터를 고친다 |
| NULL·빈 문자열·공백뿐인 값이 모두 빈 값이다 | 빈 태그를 잡은 셀렉터가 정상으로 보인다 |
| 고른 필드가 빈 공고만 걸린다 | 조건이 아무것도 좁히지 못한다 |
| `아무 필드나` 가 하나라도 빈 것을 전부 잡는다 | 칸 수만큼 걸러 봐야 한다 |
| 보정으로 채운 필드는 빈 것이 아니다 | 검수한 건이 검수 대상에 계속 남는다 |
| 보정으로 비운 필드는 빈 것이다 | 사람이 비운 판단이 화면에서 사라진다 |
| 건수는 빈 값 조건을 빼고 센다 | 한 필드를 고른 순간 나머지가 0이 되어 아무 말도 못 한다 |
| 나머지 조건은 건수에 걸린다 | 워크플로우를 좁혔는데 전체 건수가 나온다 |
| 빈 값이 정상일 수 있는 필드는 그렇다고 적는다 | 상시채용을 셀렉터 실패로 읽는다 |
| 새로 생긴 직무·자회사에도 그 메모가 붙는다 | 정상적으로 빈 건수를 셀렉터 실패로 읽는다 |
| `아무 필드나` 메모가 실제로 보는 칸 수를 적는다 | 여섯이라고 적힌 조건이 열넷을 본다 |
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
from app.api.review_filter import EMPTY_NOTES, FIELD_LABELS
from app.main import app
from app.normalize.rules import NORMALIZED_FIELDS

LIST_URL = "https://www.python.org/jobs/"

# 이 파일이 세는 여섯 칸 밖의 나머지. 세는 것이 아래 여섯이라 나머지는 값으로 채워 둔다.
# `region` 이 여섯에 들어 있는 것은 0016 이 `department` 를 지웠기 때문이다 —
# 값이 있다가 없다가 하는 칸이 하나는 있어야 빈 값 세기를 볼 수 있다
SPLIT_COLUMNS: tuple[str, ...] = tuple(
    name
    for name in NORMALIZED_FIELDS
    if name
    not in ("company_name", "title", "region", "recruitment_end_at", "body", "qualifications")
)

# 화면이 그 칸들을 부르는 이름. 건수 표에 한 줄씩 나온다
SPLIT_LABELS: tuple[str, ...] = tuple(FIELD_LABELS[name] for name in SPLIT_COLUMNS)

# (raw_job_id, workflow_id, company_name, title, region, recruitment_end_at, body, qualifications)
# 빈 값의 세 가지 모양을 섞는다. 화면에서는 셋이 구분되지 않는다
ROWS = (
    (1, 1, "엘지전자", "백엔드 개발자", "플랫폼", "2099-12-31", "본문", "자격요건"),
    (2, 1, "엘지전자", "프론트 개발자", None, "2099-12-31", "본문", "자격요건"),
    (3, 1, "엘지화학", "데이터 엔지니어", "데이터", "", "본문", "자격요건"),
    (4, 2, "이그잼플", "iOS 개발자", "모바일", "2099-12-31", "   \n  ", "자격요건"),
    (5, 2, "이그잼플", "안드로이드 개발자", None, "2099-12-31", "본문", None),
)

# 표 위의 `필드별 빈 값 건수` 표에서 한 줄
COUNT_ROW = re.compile(
    r"<td>(?P<label>[^<]+)</td>\s*<td class=\"num\">(?P<count>\d+)건</td>",
)

# 표의 제목 칸 (`fragments/review_cell_macro.html`)
TITLE_CELL = re.compile(r'id="review-cell-\d+-title".*?<span[^>]*>([^<]+)</span>', re.DOTALL)


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES ('lg', ?, 'promoted')",
        (LIST_URL,),
    )
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES ('example', ?, 'promoted')",
        ("https://example.com/jobs/",),
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, 'LG')")
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (2, 'example')")
    for (
        raw_job_id,
        workflow_id,
        company_name,
        title,
        region,
        recruitment_end_at,
        body,
        qualifications,
    ) in ROWS:
        connection.execute(
            """
            INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)
            VALUES (?, ?, ?, '{}', ?)
            """,
            (raw_job_id, workflow_id, f"{LIST_URL}{raw_job_id}/", f"hash-{raw_job_id}"),
        )
        # 나머지 칸은 전부 채워 둔다. 여기서 세는 것은 위 여섯 칸의 빈 건수이고,
        # 나머지까지 비워 두면 다섯 행 모두가 `아무 필드나` 에 걸려 그 셈이 뜻을 잃는다
        connection.execute(
            f"""
            INSERT INTO normalized_jobs (raw_job_id, company_name, title, region,
                                         recruitment_end_at, body, qualifications, source_url,
                                         {", ".join(SPLIT_COLUMNS)})
            VALUES (?, ?, ?, ?, ?, ?, ?, ?{", ?" * len(SPLIT_COLUMNS)})
            """,
            (
                raw_job_id,
                company_name,
                title,
                region,
                recruitment_end_at,
                body,
                qualifications,
                f"{LIST_URL}{raw_job_id}/",
                *("있음" for _ in SPLIT_COLUMNS),
            ),
        )
    connection.commit()
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


def counts(client: TestClient, **params: str) -> dict[str, int]:
    """화면이 적은 필드별 빈 건수."""
    html = client.get("/ui/review", params=params).text
    return {found.group("label"): int(found.group("count")) for found in COUNT_ROW.finditer(html)}


def titles(client: TestClient, **params: str) -> list[str]:
    return TITLE_CELL.findall(client.get("/ui/review", params=params).text)


def test_필드별_빈_건수가_직접_센_수와_같다(client: TestClient) -> None:
    """숫자가 틀리면 그 숫자를 보고 엉뚱한 셀렉터를 고치러 간다."""
    assert counts(client) == {
        "자회사": 0,
        "제목": 0,
        "근무지": 2,  # 2번, 5번
        "마감": 1,  # 3번 (빈 문자열)
        "본문": 1,  # 4번 (공백뿐)
        "자격요건": 1,  # 5번
        "아무 필드나": 4,  # 2·3·4·5번
        # 0011 이 더한 열 칸은 이 픽스처에서 다 채워져 있다
        **dict.fromkeys(SPLIT_LABELS, 0),
    }


def test_NULL_과_빈_문자열과_공백뿐인_값이_모두_빈_값이다(client: TestClient) -> None:
    """셋 다 화면에서는 빈 칸이다. 하나라도 빠지면 빈 태그를 잡은 셀렉터가 정상으로 보인다."""
    # 기본 정렬은 미전달 우선에 최신 수집 순이라 나중 것이 앞에 온다
    assert set(titles(client, empty="region")) == {"프론트 개발자", "안드로이드 개발자"}
    assert titles(client, empty="recruitment_end_at") == ["데이터 엔지니어"]
    assert titles(client, empty="body") == ["iOS 개발자"]


def test_아무_필드나_는_하나라도_빈_것을_전부_잡는다(client: TestClient) -> None:
    found = titles(client, empty="any")

    assert set(found) == {"프론트 개발자", "데이터 엔지니어", "iOS 개발자", "안드로이드 개발자"}
    assert "백엔드 개발자" not in found


def test_보정으로_채운_필드는_빈_것이_아니다(client: TestClient, conn: sqlite3.Connection) -> None:
    """화면에 보이는 값을 기준으로 판정한다. 검수한 건이 검수 대상에 계속 남으면 안 된다."""
    conn.execute(
        "INSERT INTO job_field_overrides (raw_job_id, field_name, value) VALUES (2, 'region', ?)",
        ("플랫폼",),
    )
    conn.commit()

    assert titles(client, empty="region") == ["안드로이드 개발자"]
    assert counts(client)["근무지"] == 1


def test_보정으로_비운_필드는_빈_것이다(client: TestClient, conn: sqlite3.Connection) -> None:
    """빈 문자열 보정은 "이 필드는 비어 있는 것이 맞다" 는 사람의 판단이다."""
    conn.execute(
        "INSERT INTO job_field_overrides (raw_job_id, field_name, value) VALUES (1, 'title', '')"
    )
    conn.commit()

    # 값 칸은 빈 값을 낱말로 적는다. 빈 칸은 반대 뜻으로 읽힌다 (`.claude/rules/writing.md`)
    assert titles(client, empty="title") == ["값 없음"]
    assert counts(client)["제목"] == 1


def test_건수는_빈_값_조건을_빼고_센다(client: TestClient) -> None:
    """조건을 걸기 전에 어디가 문제인지 보여주는 숫자다. 걸린 뒤에도 그대로여야 한다."""
    assert counts(client, empty="recruitment_end_at") == counts(client)


def test_나머지_조건은_건수에_걸린다(client: TestClient) -> None:
    """워크플로우를 좁혔으면 그 안에서 센 수가 나와야 한다."""
    assert counts(client, workflow_id="2") == {
        "자회사": 0,
        "제목": 0,
        "근무지": 1,  # 5번
        "마감": 0,
        "본문": 1,  # 4번
        "자격요건": 1,  # 5번
        "아무 필드나": 2,  # 4·5번
        **dict.fromkeys(SPLIT_LABELS, 0),
    }


def test_빈_값이_정상일_수_있는_필드는_그렇다고_적는다(client: TestClient) -> None:
    """마감이 없는 것은 상시채용일 수 있다. 저장된 값만으로는 구분되지 않는다."""
    html = client.get("/ui/review").text

    assert "상시채용이면 비어 있는 것이 맞다" in html
    assert "근무지를 따로 주지 않는 사이트면 늘 빈다" in html
    assert "본문에 자격요건이 섞여 있는 사이트면 늘 빈다" in html
    # 구분이 되는 필드는 놓친 것이라고 적는다
    assert "비어 있으면 셀렉터가 놓친 것이다" in html
    assert "있을 수 있음" in html and "아니오" in html


def test_자회사_칸도_빈_것이_정상일_수_있다고_적는다(client: TestClient) -> None:
    """0018 의 자회사다.

    계열사를 말하지 않는 사이트에서 통째로 빈다. 메모가 없으면 그 건수가 전부 셀렉터가 놓친
    것으로 읽히고, 멀쩡한 셀렉터를 고치러 간다. 0017 의 자유 글자 직무는 0031 이 지웠다.
    """
    html = client.get("/ui/review").text

    assert EMPTY_NOTES["company_name"] in html


def test_아무_필드나_메모가_실제로_보는_칸_수를_적는다(client: TestClient) -> None:
    """`여섯` 으로 굳어 있으면 0011·0016·0017 을 지나며 조용히 틀린 말이 된다."""
    html = client.get("/ui/review").text

    assert f"위 {len(NORMALIZED_FIELDS)}칸 중 하나라도 빈 공고" in html
    # 판정 칸을 빈 칸으로 두지 않는다 (`.claude/rules/writing.md`)
    assert "칸마다 다름" in html


def test_조회_조건에_빈_값_칸이_있다(client: TestClient) -> None:
    html = client.get("/ui/review/filters").text

    assert 'name="empty"' in html
    for label in ("아무 필드나", "자회사", "제목", "근무지", "마감", "본문", "자격요건"):
        assert f">{label}</option>" in html


def test_지우기가_빈_값_조건을_그대로_들고_간다(client: TestClient) -> None:
    """조건에 걸린 전부를 지울 때 표가 센 것과 같은 행이어야 한다."""
    html = client.post(
        "/ui/review/delete/confirm", data={"all_filtered": "1", "empty": "region"}
    ).text

    assert "빈 값 근무지" in html
    # 근무지가 빈 두 건만 대상이다
    assert "2건</td>" in html
