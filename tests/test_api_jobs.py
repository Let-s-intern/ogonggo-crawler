"""제공 API 테스트. 계약은 `.claude/docs/api-contract.md` 다.

실사이트에 나가지 않는다. `normalized_jobs` 에 시드를 직접 넣고 응답만 본다.

핵심 단언은 하나다. 커서를 따라 끝까지 받은 id 집합이 시드 전체와 정확히 같아야 한다.
누락은 소비 측이 영영 못 받는 공고고, 중복은 같은 공고가 두 번 올라가는 것이다.

시드의 `normalized_at` 은 일부러 겹치게 넣는다. 실제 저장 형식이 초 단위라 한 번의 실행에서
적재된 행 수십 개가 같은 값을 갖는다. 그 상태에서 페이지 경계가 걸리는 것이 정상 상황이고,
`(normalized_at, id)` 쌍이 아니면 그때 행이 잘리거나 겹친다.
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import jobs as jobs_api
from app.main import app

# 같은 초에 적재된 것처럼 보이게 하는 값. 페이지 경계가 이 안쪽에 걸린다
BULK_AT = "2026-08-21 10:00:00"


def seed(conn: sqlite3.Connection, count: int, normalized_at: str | None = None) -> list[int]:
    """`normalized_jobs` 에 `count` 건을 넣고 id 목록을 돌려준다."""
    ids: list[int] = []
    for index in range(count):
        raw = conn.execute(
            """
            INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash)
            VALUES (1, ?, '{}', ?)
            """,
            (f"https://example.test/jobs/{index}", f"hash-{index}"),
        )
        raw_id = int(raw.lastrowid or 0)
        # 같은 초에 적재된 행이 뭉치도록 값을 직접 넣는다
        stamp = normalized_at if normalized_at is not None else BULK_AT
        cursor = conn.execute(
            """
            INSERT INTO normalized_jobs
                   (raw_job_id, company_name, title, recruitment_end_at, body, qualifications,
                    source_url, normalized_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raw_id,
                f"회사{index}",
                f"공고 {index}",
                "2026-09-30",
                "본문",
                "자격요건",
                f"https://example.test/jobs/{index}",
                stamp,
            ),
        )
        ids.append(int(cursor.lastrowid or 0))
    return ids


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status)"
        " VALUES ('시드', 'https://example.test/', 'promoted')"
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '시드')")
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

    app.dependency_overrides[jobs_api.get_connection] = request_connection
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def collect(client: TestClient, params: dict[str, Any] | None = None) -> list[int]:
    """`has_more` 가 꺼질 때까지 커서를 따라가며 받은 id 를 순서대로 모은다."""
    query: dict[str, Any] = dict(params or {})
    seen: list[int] = []
    for _ in range(50):  # 커서가 제자리를 돌면 여기서 멈춘다
        response = client.get("/api/jobs", params=query)
        assert response.status_code == 200
        payload = response.json()
        seen.extend(item["id"] for item in payload["items"])
        if not payload["has_more"]:
            return seen
        query["cursor"] = payload["next_cursor"]
    raise AssertionError("페이지를 50번 넘겨도 끝나지 않았다")


def test_two_pages_cover_every_row_exactly_once(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """limit 보다 많은 시드를 두 페이지로 이어 받으면 id 집합이 전체와 같아야 한다."""
    total = jobs_api.DEFAULT_LIMIT + 50
    expected = seed(conn, total)

    first = client.get("/api/jobs").json()
    assert len(first["items"]) == jobs_api.DEFAULT_LIMIT
    assert first["has_more"] is True

    second = client.get("/api/jobs", params={"cursor": first["next_cursor"]}).json()
    assert len(second["items"]) == 50
    assert second["has_more"] is False

    received = [item["id"] for item in first["items"]] + [item["id"] for item in second["items"]]
    assert len(received) == total
    assert len(set(received)) == total  # 중복 없음
    assert set(received) == set(expected)  # 누락 없음
    assert received == sorted(expected)


def test_cursor_walk_matches_full_set(client: TestClient, conn: sqlite3.Connection) -> None:
    """여러 페이지를 끝까지 따라가도 같다. 마지막 페이지에서 has_more 가 꺼진다."""
    expected = seed(conn, jobs_api.DEFAULT_LIMIT * 2 + 7)
    assert collect(client) == sorted(expected)


def test_row_inserted_between_polls_is_not_skipped(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """폴링 사이에 행이 들어와도 앞 페이지가 밀리지 않는다. 오프셋 기반이면 여기서 건너뛴다."""
    first_batch = seed(conn, jobs_api.DEFAULT_LIMIT + 10)

    first = client.get("/api/jobs").json()
    assert first["has_more"] is True
    # 첫 페이지보다 앞선 시각으로 한 건 삽입. 오프셋이면 두 번째 페이지가 한 건 건너뛴다
    inserted = seed(conn, 1, normalized_at="2026-08-21 09:00:00")

    rest = collect(client, {"cursor": first["next_cursor"]})
    received = [item["id"] for item in first["items"]] + rest
    # 앞에 끼어든 행은 커서보다 뒤라 이번 회차에서는 오지 않는다. 중요한 것은 나머지가 온전한 것
    assert set(received) == set(first_batch)
    assert len(received) == len(set(received))
    assert inserted[0] not in received


def test_empty_table_returns_empty_page(client: TestClient) -> None:
    payload = client.get("/api/jobs").json()
    assert payload == {"items": [], "next_cursor": None, "has_more": False}


def test_item_shape_matches_contract(client: TestClient, conn: sqlite3.Connection) -> None:
    """계약이 정한 필드만, 정한 이름으로 나간다. delivered_at 은 응답에 없다."""
    seed(conn, 1)
    item = client.get("/api/jobs").json()["items"][0]
    assert set(item) == {
        "id",
        # 0018 이 회사명을 두 칸으로 갈랐다. 모회사는 사이트를 운영하는 기업,
        # 자회사는 그 공고가 말한 계열사다
        "parent_company_name",
        "company_name",
        "title",
        # 0025 가 더한 직무 분류. job_taxonomy 표에서 고른 닫힌 값이다(5.3)
        "job_field",
        "job_role",
        "recruitment_end_at",
        "body",
        "qualifications",
        # 0011 이 더한 칸에서 0016 이 셋을 뺀 나머지
        "recruitment_start_at",
        "employment_type",
        "experience_type",
        "region",
        "responsibilities",
        "preferred_qualifications",
        "hiring_process",
        "recruitment_notice",
        # 0028 이 더한 다섯 칸. 오공고가 받는 칸이고 분류가 채운다
        "company_and_team_introduction",
        "compensation",
        "benefits",
        "education_level",
        "recruitment_headcount",
        "source_url",
        "normalized_at",
    }
    assert item["normalized_at"] == "2026-08-21T10:00:00Z"


# 0011 이 더한 칸에서 0016 이 셋을 뺀 나머지. 사이트가 주는 것만 채우고 나머지는 NULL 로 둔다
SPLIT_BODY_FIELDS = (
    "recruitment_start_at",
    "employment_type",
    "experience_type",
    "region",
    "responsibilities",
    "preferred_qualifications",
    "hiring_process",
    "recruitment_notice",
)


def test_new_columns_go_out_filled_or_null(client: TestClient, conn: sqlite3.Connection) -> None:
    """채워진 칸은 값 그대로, 사이트가 주지 않은 칸은 `null` 로 나간다."""
    seed(conn, 1)
    conn.execute(
        "UPDATE normalized_jobs SET region = ?, recruitment_start_at = ?",
        ("경기 수원시", "2026-09-01"),
    )

    item = client.get("/api/jobs").json()["items"][0]

    assert item["region"] == "경기 수원시"
    assert item["recruitment_start_at"] == "2026-09-01"
    assert [
        item[name] for name in SPLIT_BODY_FIELDS if name not in ("region", "recruitment_start_at")
    ] == [None] * (len(SPLIT_BODY_FIELDS) - 2)
    # 기존 필드의 값과 뜻은 그대로다
    assert item["recruitment_end_at"] == "2026-09-30"


def test_job_major_minor_go_out_filled_or_null(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """분류가 채운 값은 그대로, 아직 분류를 돌리지 않은 건은 `null` 로 나간다(5.3).

    `position_name` 과 같은 규칙이다 — 값이 없으면 다른 값으로 메우지 않는다.
    """
    ids = seed(conn, 2)
    conn.execute(
        "UPDATE normalized_jobs SET job_field = ?, job_role = ? WHERE id = ?",
        ("IT·개발", "서버·백엔드", ids[0]),
    )

    items = {item["id"]: item for item in client.get("/api/jobs").json()["items"]}

    assert items[ids[0]]["job_field"] == "IT·개발"
    assert items[ids[0]]["job_role"] == "서버·백엔드"
    assert items[ids[1]]["job_field"] is None
    assert items[ids[1]]["job_role"] is None


def test_job_major_minor_survive_two_cursor_pages(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """커서로 두 번 나눠 받아도 두 필드가 매 건 있고, 값이 누락·중복 없이 온다(5.3.V)."""
    ids = seed(conn, 4)
    majors = {ids[0]: "IT·개발", ids[2]: "경영·전략"}
    for job_id, major in majors.items():
        conn.execute("UPDATE normalized_jobs SET job_field = ? WHERE id = ?", (major, job_id))

    first = client.get("/api/jobs", params={"limit": 2}).json()
    second = client.get("/api/jobs", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    items = first["items"] + second["items"]

    assert {item["id"] for item in items} == set(ids)
    assert len({item["id"] for item in items}) == len(items)
    seen_majors = {item["id"]: item["job_field"] for item in items}
    for job_id in ids:
        assert seen_majors[job_id] == majors.get(job_id)
        # 두 필드 모두 응답에 있다 — 없는 값은 빠지는 것이 아니라 `null` 이다
        item = next(i for i in items if i["id"] == job_id)
        assert "job_field" in item
        assert "job_role" in item


def test_broken_cursor_is_rejected(client: TestClient, conn: sqlite3.Connection) -> None:
    """망가진 커서를 처음부터로 돌리면 소비 측이 같은 데이터를 다시 받는다."""
    seed(conn, 3)
    assert client.get("/api/jobs", params={"cursor": "!!not-base64!!"}).status_code == 400


# --- 8.2 updated_after 와 limit ---


def test_limit_slices_pages_and_cursor_covers_all(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """limit 을 줄여도 커서로 이어 받으면 누락·중복이 없다."""
    expected = seed(conn, 7)

    first = client.get("/api/jobs", params={"limit": 3}).json()
    assert [item["id"] for item in first["items"]] == sorted(expected)[:3]
    assert first["has_more"] is True

    second = client.get("/api/jobs", params={"limit": 3, "cursor": first["next_cursor"]}).json()
    assert [item["id"] for item in second["items"]] == sorted(expected)[3:6]
    assert second["has_more"] is True

    walked = collect(client, {"limit": 3})
    assert walked == sorted(expected)
    assert len(set(walked)) == len(expected)


def test_limit_over_cap_is_clipped_to_500(client: TestClient, conn: sqlite3.Connection) -> None:
    """limit=1000 은 500 으로 잘린다. 거절이 아니라 절삭이고, 나머지는 커서로 이어 받는다."""
    seed(conn, jobs_api.MAX_LIMIT + 20)

    payload = client.get("/api/jobs", params={"limit": 1000}).json()
    assert len(payload["items"]) == jobs_api.MAX_LIMIT
    assert payload["has_more"] is True

    rest = client.get("/api/jobs", params={"limit": 1000, "cursor": payload["next_cursor"]}).json()
    assert len(rest["items"]) == 20
    assert rest["has_more"] is False


def test_default_limit_is_100(client: TestClient, conn: sqlite3.Connection) -> None:
    seed(conn, 120)
    payload = client.get("/api/jobs").json()
    assert len(payload["items"]) == jobs_api.DEFAULT_LIMIT


def test_updated_after_boundary_excludes_the_instant_itself(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """경계값 앞뒤. 경계 시각과 같은 건은 오지 않고, 그 뒤만 온다."""
    before = seed(conn, 2, normalized_at="2026-08-21 09:59:59")
    at = seed(conn, 2, normalized_at="2026-08-21 10:00:00")
    after = seed(conn, 2, normalized_at="2026-08-21 10:00:01")

    received = collect(client, {"updated_after": "2026-08-21T10:00:00Z"})
    assert set(received) == set(after)
    assert not set(received) & (set(before) | set(at))

    everything = collect(client, {"updated_after": "2026-08-21T09:00:00Z"})
    assert set(everything) == set(before) | set(at) | set(after)


def test_updated_after_uses_normalized_at_not_crawled_at(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """수집은 옛날에 했어도 정규화가 최근이면 온다. 커서가 걸린 값은 normalized_at 이다."""
    kept = seed(conn, 1, normalized_at="2026-08-21 10:00:05")
    conn.execute("UPDATE raw_jobs SET crawled_at = '2020-01-01 00:00:00'")

    received = collect(client, {"updated_after": "2026-08-21T10:00:00Z"})
    assert received == kept


def test_updated_after_accepts_offset_and_naive_forms(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """+09:00 은 UTC 로 옮겨 비교한다. 로컬 시각으로 해석하면 시차만큼 못 받는 구간이 생긴다."""
    seed(conn, 1, normalized_at="2026-08-21 02:00:00")
    late = seed(conn, 1, normalized_at="2026-08-21 04:00:00")

    # 2026-08-21T12:00:00+09:00 == 2026-08-21T03:00:00Z
    by_offset = collect(client, {"updated_after": "2026-08-21T12:00:00+09:00"})
    assert by_offset == late
    # 타임존이 없으면 UTC 로 본다
    by_naive = collect(client, {"updated_after": "2026-08-21T03:00:00"})
    assert by_naive == late


def test_broken_updated_after_is_rejected(client: TestClient) -> None:
    response = client.get("/api/jobs", params={"updated_after": "어제"})
    assert response.status_code == 422


# --- 8.3 전달 확인 ---


def delivered_values(conn: sqlite3.Connection) -> dict[int, str | None]:
    rows = conn.execute("SELECT id, delivered_at FROM normalized_jobs ORDER BY id").fetchall()
    return {int(row["id"]): row["delivered_at"] for row in rows}


def test_delivered_marks_the_given_ids(client: TestClient, conn: sqlite3.Connection) -> None:
    ids = seed(conn, 3)
    payload = client.post("/api/jobs/delivered", json={"ids": ids[:2]}).json()
    assert payload == {"marked": 2, "already_delivered": 0, "missing": []}

    stored = delivered_values(conn)
    assert stored[ids[0]] is not None
    assert stored[ids[1]] is not None
    assert stored[ids[2]] is None  # 보내지 않은 건은 그대로다


def test_second_call_keeps_the_first_timestamp(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """같은 id 로 두 번 불러도 첫 번째 시각이 남는다.

    두 호출이 같은 초에 들어가면 값이 같아 덮어써도 통과하는 허수 검증이 된다. 그래서 첫 호출
    뒤 값을 눈에 띄게 과거로 바꿔 두고, 두 번째 호출이 그 값을 그대로 두는지 본다.
    """
    ids = seed(conn, 1)
    job_id = ids[0]

    first = client.post("/api/jobs/delivered", json={"ids": [job_id]}).json()
    assert first["marked"] == 1
    assert delivered_values(conn)[job_id] is not None

    # 오래 전에 전달된 상태로 만든다. 덮어쓰면 값이 오늘로 바뀌어 바로 드러난다
    long_ago = "2020-01-01 00:00:00"
    conn.execute("UPDATE normalized_jobs SET delivered_at = ? WHERE id = ?", (long_ago, job_id))

    second = client.post("/api/jobs/delivered", json={"ids": [job_id]}).json()
    assert second == {"marked": 0, "already_delivered": 1, "missing": []}
    assert delivered_values(conn)[job_id] == long_ago


def test_mixed_batch_marks_only_the_undelivered(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """이미 찍힌 건과 아직인 건이 섞여 와도 찍힌 쪽은 그대로다."""
    ids = seed(conn, 2)
    long_ago = "2020-01-01 00:00:00"
    conn.execute("UPDATE normalized_jobs SET delivered_at = ? WHERE id = ?", (long_ago, ids[0]))

    payload = client.post("/api/jobs/delivered", json={"ids": ids}).json()
    assert payload == {"marked": 1, "already_delivered": 1, "missing": []}

    stored = delivered_values(conn)
    assert stored[ids[0]] == long_ago
    assert stored[ids[1]] is not None


def test_unknown_id_is_reported_not_fatal(client: TestClient, conn: sqlite3.Connection) -> None:
    """없는 id 하나 때문에 배치 전체가 실패하면 소비 측이 나머지를 다시 받는다."""
    ids = seed(conn, 1)
    payload = client.post("/api/jobs/delivered", json={"ids": [ids[0], 9999]}).json()
    assert payload == {"marked": 1, "already_delivered": 0, "missing": [9999]}


def test_empty_ids_changes_nothing(client: TestClient, conn: sqlite3.Connection) -> None:
    seed(conn, 2)
    payload = client.post("/api/jobs/delivered", json={"ids": []}).json()
    assert payload == {"marked": 0, "already_delivered": 0, "missing": []}
    assert set(delivered_values(conn).values()) == {None}


def test_duplicate_ids_in_one_request_count_once(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    ids = seed(conn, 1)
    payload = client.post("/api/jobs/delivered", json={"ids": [ids[0], ids[0]]}).json()
    assert payload == {"marked": 1, "already_delivered": 0, "missing": []}


def test_delivered_does_not_touch_normalized_at(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """전달 표시가 normalized_at 을 밀면 소비 측 커서가 같은 건을 다시 받는다."""
    ids = seed(conn, 1)
    before = conn.execute("SELECT normalized_at FROM normalized_jobs").fetchone()["normalized_at"]
    client.post("/api/jobs/delivered", json={"ids": ids})
    after = conn.execute("SELECT normalized_at FROM normalized_jobs").fetchone()["normalized_at"]
    assert after == before


def test_the_two_company_keys_mean_what_the_contract_says(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """모회사는 사이트를 운영하는 기업, 자회사는 그 공고가 말한 계열사다 (3.5.V)."""
    seed(conn, 1)
    conn.execute(
        "UPDATE normalized_jobs SET parent_company_name = ?, company_name = ?",
        ("삼성전자", "삼성SDS"),
    )

    item = client.get("/api/jobs").json()["items"][0]

    assert item["parent_company_name"] == "삼성전자"
    assert item["company_name"] == "삼성SDS"


def test_the_subsidiary_goes_out_null_and_is_not_filled_with_the_parent(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """계열사를 말하지 않는 사이트다. 모회사 이름으로 메우면 두 칸이 같은 값이 된다."""
    seed(conn, 1)
    conn.execute(
        "UPDATE normalized_jobs SET parent_company_name = ?, company_name = NULL", ("토스",)
    )

    item = client.get("/api/jobs").json()["items"][0]

    assert item["parent_company_name"] == "토스"
    assert item["company_name"] is None
