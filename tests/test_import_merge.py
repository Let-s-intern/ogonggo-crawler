"""올린 파일을 기존 데이터에 더하는 병합.

확인하는 것은 여섯이다.

- 겹치는 해시는 건너뛰고 새 것만 들어간다
- 기존 행은 한 글자도 바뀌지 않는다
- 가져온 행의 `delivered_at` 은 NULL 이다
- 정규화 값은 저쪽 것이 아니라 이 서버 규칙이 만든 값이다
- 실패하면 아무것도 남지 않는다
- 저쪽 서버의 실행 기록(`crawl_runs`, 워크플로우 누적 카운트)은 따라오지 않는다

올린 파일은 합성 픽스처와 저장소의 실제 스냅샷 둘 다로 만든다. 합성 쪽은 무엇이 왜 그런지를
좁혀 보고, 스냅샷 쪽은 실제로 받게 될 138건짜리 파일에서 건수가 맞는지를 본다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator, Sequence
from typing import Any

import pytest

from app import db
from app.api.import_data import ImportRejected, ImportResult, import_database
from app.crawler.hashing import content_hash
from app.normalize.rules import NORMALIZED_FIELDS, without_yearless_formats

SNAPSHOT = pathlib.Path(__file__).resolve().parent.parent / "seeds" / "snapshot" / "jobs.db"

LIST_URL = "https://example.test/jobs"
SELECTORS = json.dumps({"list": {"item": ".job"}, "detail": {"title": "h1"}}, ensure_ascii=False)

# 저쪽 서버가 만든 값. 이 값이 이 서버에 나타나면 `normalized_jobs` 를 복사한 것이다
FOREIGN_VALUE = "저쪽_규칙이_만든_값"
# 저쪽에서 이미 전달된 표시. 따라 들어오면 소비 측이 못 받는 공고가 생긴다
FOREIGN_DELIVERED = "2020-01-01 00:00:00"


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    """이 서버의 DB. 스키마만 있고 데이터는 없다 — 배포 직후의 상태다."""
    connection = db.connect(tmp_path / "server.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


def job(title: str, *, deadline: str = "2026-12-31", body: str = "본문") -> dict[str, str]:
    return {
        "company": "예시회사",
        "title": title,
        "department": "개발",
        "deadline": deadline,
        "body": body,
        "requirements": "무관",
    }


def make_upload(
    path: pathlib.Path,
    *,
    jobs: Sequence[dict[str, str]],
    rules: Sequence[tuple[str, str, str, int, str | None]] = (),
    overrides: Sequence[tuple[int, str, str]] = (),
    crawler_name: str = "예시사이트",
    list_url: str = LIST_URL,
) -> pathlib.Path:
    """올릴 파일 하나.

    저쪽 서버답게 만든다. `normalized_jobs` 에는 이 서버 규칙으로는 나올 수 없는 값이 들어
    있고, 워크플로우에는 누적 카운트가, `crawl_runs` 에는 실행 기록이 쌓여 있다.
    """
    upload = db.connect(path)
    db.migrate_up(upload)
    upload.execute(
        """
        INSERT INTO crawlers (name, list_url, detail_url, selectors_json, list_mode,
                              detail_mode, status, default_company)
        VALUES (?, ?, ?, ?, 'playwright', 'playwright', 'promoted', '기본회사')
        """,
        (crawler_name, list_url, list_url + "/{id}", SELECTORS),
    )
    upload.execute(
        """
        INSERT INTO workflows (crawler_id, name, interval_minutes, status, success_count,
                               fail_count, last_run_at, auto_stop_threshold)
        VALUES (1, ?, 30, 'active', 99, 7, ?, 5)
        """,
        (crawler_name, FOREIGN_DELIVERED),
    )
    upload.execute(
        """
        INSERT INTO crawl_runs (workflow_id, status, success_count, new_count, trigger)
        VALUES (1, 'success', 3, 3, 'schedule')
        """
    )
    for field_name, rule_type, config, priority, note in rules:
        upload.execute(
            """
            INSERT INTO normalization_rules (field_name, rule_type, rule_config_json, priority,
                                             enabled, note)
            VALUES (?, ?, ?, ?, 1, ?)
            """,
            (field_name, rule_type, config, priority, note),
        )
    for index, fields in enumerate(jobs, start=1):
        source_url = f"{list_url}/{index}"
        record = {**fields, "source_url": source_url}
        upload.execute(
            """
            INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash,
                                  crawled_at)
            VALUES (1, ?, ?, ?, '2026-08-01 09:00:00')
            """,
            (source_url, json.dumps(record, ensure_ascii=False), content_hash(record)),
        )
        upload.execute(
            """
            INSERT INTO normalized_jobs (raw_job_id, company, title, source_url, delivered_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (index, FOREIGN_VALUE, FOREIGN_VALUE, source_url, FOREIGN_DELIVERED),
        )
    for raw_job_id, field_name, value in overrides:
        upload.execute(
            """
            INSERT INTO job_field_overrides (raw_job_id, field_name, value, created_at,
                                             updated_at)
            VALUES (?, ?, ?, '2026-08-02 10:00:00', '2026-08-02 10:00:00')
            """,
            (raw_job_id, field_name, value),
        )
    upload.close()
    return path


def rows(conn: sqlite3.Connection, sql: str) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in conn.execute(sql)]


def count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"])


def title_of(conn: sqlite3.Connection, source_url: str) -> str | None:
    row = conn.execute(
        "SELECT title FROM normalized_jobs WHERE source_url = ?", (source_url,)
    ).fetchone()
    return None if row is None else row["title"]


def test_빈_서버에_올리면_전부_새로_들어간다(
    conn: sqlite3.Connection, tmp_path: pathlib.Path
) -> None:
    upload = make_upload(tmp_path / "upload.db", jobs=[job("가"), job("나")])

    result = import_database(conn, upload)

    assert (result.crawlers_added, result.workflows_added) == (1, 1)
    assert (result.raw_added, result.raw_duplicate) == (2, 0)
    assert (result.normalized_added, result.normalize_failed) == (2, 0)
    assert count(conn, "raw_jobs") == 2
    assert count(conn, "normalized_jobs") == 2


def test_같은_파일을_두_번_올리면_두_번째는_전부_중복이다(
    conn: sqlite3.Connection, tmp_path: pathlib.Path
) -> None:
    upload = make_upload(tmp_path / "upload.db", jobs=[job("가"), job("나"), job("다")])
    import_database(conn, upload)
    before = rows(conn, "SELECT * FROM raw_jobs ORDER BY id")

    result = import_database(conn, upload)

    assert (result.raw_added, result.raw_duplicate) == (0, 3)
    assert (result.crawlers_added, result.workflows_added) == (0, 0)
    assert (result.crawlers_skipped, result.workflows_skipped) == (1, 1)
    assert result.normalized_added == 0
    assert count(conn, "raw_jobs") == 3
    assert count(conn, "normalized_jobs") == 3
    # 단언의 본체. 두 번째 업로드는 기존 행을 한 글자도 건드리지 않았다
    assert rows(conn, "SELECT * FROM raw_jobs ORDER BY id") == before


def test_겹치는_것은_건너뛰고_새_것만_들어간다(
    conn: sqlite3.Connection, tmp_path: pathlib.Path
) -> None:
    first = make_upload(tmp_path / "first.db", jobs=[job("가"), job("나")])
    import_database(conn, first)
    before = rows(conn, "SELECT * FROM raw_jobs ORDER BY id")

    # 앞의 두 건은 그대로 두고 한 건만 새로 붙인 파일
    second = make_upload(tmp_path / "second.db", jobs=[job("가"), job("나"), job("다")])
    result = import_database(conn, second)

    assert (result.raw_added, result.raw_duplicate) == (1, 2)
    assert rows(conn, "SELECT * FROM raw_jobs ORDER BY id")[:2] == before
    titles = [
        json.loads(row["raw_data_json"])["title"]
        for row in conn.execute("SELECT raw_data_json FROM raw_jobs ORDER BY id")
    ]
    assert titles == ["가", "나", "다"]


def test_가져온_행은_전부_미전달이다(conn: sqlite3.Connection, tmp_path: pathlib.Path) -> None:
    """저쪽에서 전달 표시된 행이라도 이 서버의 소비 측은 받은 적이 없다."""
    upload = make_upload(tmp_path / "upload.db", jobs=[job("가"), job("나")])

    import_database(conn, upload)

    delivered = rows(conn, "SELECT delivered_at FROM normalized_jobs ORDER BY id")
    assert delivered == [(None,), (None,)]


def test_정규화_값은_이_서버_규칙이_만든_값이다(
    conn: sqlite3.Connection, tmp_path: pathlib.Path
) -> None:
    """올린 파일의 `normalized_jobs` 를 복사하지 않는다."""
    conn.execute(
        """
        INSERT INTO normalization_rules (field_name, rule_type, rule_config_json, priority)
        VALUES ('title', 'mapping', ?, 0)
        """,
        (json.dumps({"map": {"가": "이_서버_규칙이_만든_값"}}, ensure_ascii=False),),
    )
    upload = make_upload(tmp_path / "upload.db", jobs=[job("가")])

    import_database(conn, upload)

    assert title_of(conn, f"{LIST_URL}/1") == "이_서버_규칙이_만든_값"
    assert FOREIGN_VALUE not in rows(conn, "SELECT title FROM normalized_jobs")[0]


def test_규칙은_없는_것만_들어오고_먼저_들어와_정규화에_쓰인다(
    conn: sqlite3.Connection, tmp_path: pathlib.Path
) -> None:
    """빈 서버에 올릴 때 공고와 규칙이 같이 들어와야 값이 저쪽과 같아진다."""
    config = json.dumps({"map": {"가": "규칙이_바꾼_값"}}, ensure_ascii=False)
    upload = make_upload(
        tmp_path / "upload.db",
        jobs=[job("가")],
        rules=[("title", "mapping", config, 0, "메모")],
    )

    result = import_database(conn, upload)

    assert (result.rules_added, result.rules_skipped) == (1, 0)
    assert title_of(conn, f"{LIST_URL}/1") == "규칙이_바꾼_값"


def test_메모만_다른_같은_규칙은_두_번_들어오지_않는다(
    conn: sqlite3.Connection, tmp_path: pathlib.Path
) -> None:
    config = json.dumps({"collapse_whitespace": True})
    conn.execute(
        """
        INSERT INTO normalization_rules (field_name, rule_type, rule_config_json, priority, note)
        VALUES ('title', 'trim', ?, 0, '이_서버_메모')
        """,
        (config,),
    )
    upload = make_upload(
        tmp_path / "upload.db",
        jobs=[job("가")],
        rules=[("title", "trim", config, 0, "저쪽_메모")],
    )

    result = import_database(conn, upload)

    assert (result.rules_added, result.rules_skipped) == (0, 1)
    assert count(conn, "normalization_rules") == 1


def test_사람이_검수한_값은_따라오고_정규화에_반영된다(
    conn: sqlite3.Connection, tmp_path: pathlib.Path
) -> None:
    """보정은 다시 만들 수 없는 값이라 함께 가져온다. 규칙 다음에 덮는 순서도 그대로다."""
    upload = make_upload(
        tmp_path / "upload.db",
        jobs=[job("가"), job("나")],
        overrides=[(2, "title", "사람이_고친_제목")],
    )

    result = import_database(conn, upload)

    assert (result.overrides_added, result.overrides_skipped) == (1, 0)
    assert title_of(conn, f"{LIST_URL}/2") == "사람이_고친_제목"
    assert title_of(conn, f"{LIST_URL}/1") == "가"


def test_이_서버에_이미_있는_보정은_덮지_않는다(
    conn: sqlite3.Connection, tmp_path: pathlib.Path
) -> None:
    upload = make_upload(
        tmp_path / "upload.db",
        jobs=[job("가")],
        overrides=[(1, "title", "저쪽이_고친_제목")],
    )
    import_database(conn, upload)
    conn.execute("UPDATE job_field_overrides SET value = '이_서버가_고친_제목'")

    result = import_database(conn, upload)

    assert (result.overrides_added, result.overrides_skipped) == (0, 1)
    values = rows(conn, "SELECT value FROM job_field_overrides")
    assert values == [("이_서버가_고친_제목",)]


def test_나눈_공고의_보정은_번호마다_따로_들어온다(
    conn: sqlite3.Connection, tmp_path: pathlib.Path
) -> None:
    """같은 칸이어도 번호가 다르면 다른 보정이다. 1번 보정이 있다고 2번을 건너뛰지 않는다."""
    path = make_upload(
        tmp_path / "upload.db", jobs=[job("가")], overrides=[(1, "benefits", "1번_복지")]
    )
    upload = db.connect(path)
    upload.execute(
        """
        INSERT INTO job_field_overrides (raw_job_id, part, field_name, value, created_at,
                                         updated_at)
        VALUES (1, 2, 'benefits', '2번_복지', '2026-08-02 10:00:00', '2026-08-02 10:00:00')
        """
    )
    upload.close()

    result = import_database(conn, path)

    assert (result.overrides_added, result.overrides_skipped) == (2, 0)
    assert rows(conn, "SELECT part, value FROM job_field_overrides ORDER BY part") == [
        (1, "1번_복지"),
        (2, "2번_복지"),
    ]


def test_저쪽_서버의_실행_기록은_따라오지_않는다(
    conn: sqlite3.Connection, tmp_path: pathlib.Path
) -> None:
    upload = make_upload(tmp_path / "upload.db", jobs=[job("가")])

    import_database(conn, upload)

    assert count(conn, "crawl_runs") == 0
    workflow = conn.execute("SELECT * FROM workflows").fetchone()
    assert (workflow["success_count"], workflow["fail_count"]) == (0, 0)
    assert workflow["last_run_at"] is None
    # 저쪽의 실행 기록이 아닌 설정값은 그대로 온다
    assert (workflow["interval_minutes"], workflow["auto_stop_threshold"]) == (30, 5)
    assert workflow["status"] == "active"


def test_크롤러는_셀렉터와_렌더_방식을_그대로_가져온다(
    conn: sqlite3.Connection, tmp_path: pathlib.Path
) -> None:
    """수집 방식을 놓치면 JS 사이트가 정적으로 돌아 0건이 나온다."""
    upload = make_upload(tmp_path / "upload.db", jobs=[job("가")])

    import_database(conn, upload)

    crawler = conn.execute("SELECT * FROM crawlers").fetchone()
    assert crawler["selectors_json"] == SELECTORS
    assert (crawler["list_mode"], crawler["detail_mode"]) == ("playwright", "playwright")
    assert crawler["status"] == "promoted"
    assert crawler["default_company"] == "기본회사"
    assert crawler["detail_url"] == LIST_URL + "/{id}"


def test_id_는_다시_매겨진다(conn: sqlite3.Connection, tmp_path: pathlib.Path) -> None:
    """기존 행과 부딪히지 않게 이 서버가 새 id 를 준다."""
    first = make_upload(tmp_path / "first.db", jobs=[job("가")], crawler_name="첫번째")
    import_database(conn, first)
    second = make_upload(
        tmp_path / "second.db",
        jobs=[job("나")],
        crawler_name="두번째",
        list_url="https://other.test/jobs",
    )

    import_database(conn, second)

    assert rows(conn, "SELECT id FROM crawlers ORDER BY id") == [(1,), (2,)]
    assert rows(conn, "SELECT id FROM raw_jobs ORDER BY id") == [(1,), (2,)]
    linked = rows(conn, "SELECT id, crawler_id FROM workflows ORDER BY id")
    assert linked == [(1, 1), (2, 2)]
    assert rows(conn, "SELECT workflow_id FROM raw_jobs ORDER BY id") == [(1,), (2,)]


def test_중간에_틀어지면_아무것도_남지_않는다(
    conn: sqlite3.Connection, tmp_path: pathlib.Path
) -> None:
    """크롤러와 워크플로우가 이미 들어간 뒤 공고에서 실패하는 파일."""
    upload = make_upload(tmp_path / "upload.db", jobs=[job("가"), job("나")])
    broken = sqlite3.connect(upload)
    broken.execute("UPDATE raw_jobs SET raw_data_json = '{끊긴 json' WHERE id = 2")
    broken.commit()
    broken.close()

    with pytest.raises(ImportRejected) as caught:
        import_database(conn, upload)

    assert caught.value.reason == "broken_row"
    for table in ("crawlers", "workflows", "raw_jobs", "normalized_jobs"):
        assert count(conn, table) == 0, f"{table} 에 절반이 남았다"


def test_스냅샷이_통째로_들어오고_두_번째는_전부_중복이다(conn: sqlite3.Connection) -> None:
    """저장소의 실제 수집 데이터로 도는 확인. 19.3.V 가 화면에서 볼 숫자와 같다.

    기대 건수는 파일에서 읽는다. 스냅샷은 운영 볼륨에서 다시 복사해 갱신되므로
    (`seeds/snapshot/README.md`) 숫자를 적어 두면 갱신 때마다 이 테스트가 깨진다. 단언의
    본체는 "파일에 있는 것이 전부 들어왔는가" 이고, 그것은 건수를 읽어 와도 그대로 선다.
    """
    source = _source_counts(SNAPSHOT)
    first = import_database(conn, SNAPSHOT)

    assert first.crawlers_added == source["crawlers"]
    assert first.workflows_added == source["workflows"]
    assert first.rules_added == source["normalization_rules_kept"]
    assert first.raw_added == source["raw_jobs"]
    assert first.overrides_added == source["job_field_overrides"]
    assert first.raw_duplicate == 0
    assert first.normalized_added + first.normalize_failed == source["raw_jobs"]
    assert count(conn, "raw_jobs") == source["raw_jobs"]
    assert count(conn, "normalized_jobs") == first.normalized_added
    # 저쪽 서버의 실행 기록은 오지 않고, 전달 표시도 오지 않는다
    assert count(conn, "crawl_runs") == 0
    assert count(conn, "normalized_jobs") > 0
    assert rows(conn, "SELECT count(*) AS n FROM normalized_jobs WHERE delivered_at IS NULL") == [
        (count(conn, "normalized_jobs"),)
    ]

    before = rows(conn, "SELECT * FROM raw_jobs ORDER BY id")
    second = import_database(conn, SNAPSHOT)

    assert _added(second) == (0, 0, 0, 0, 0)
    assert second.raw_duplicate == source["raw_jobs"]
    assert second.crawlers_skipped == source["crawlers"]
    assert second.workflows_skipped == source["workflows"]
    assert second.rules_skipped == source["normalization_rules"]  # 지워진 칸의 규칙까지 센다
    assert second.overrides_skipped == source["job_field_overrides"]
    assert rows(conn, "SELECT * FROM raw_jobs ORDER BY id") == before


def _source_counts(path: pathlib.Path) -> dict[str, int]:
    """올릴 파일에 무엇이 몇 건 있는지. 읽기 전용으로 연다."""
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        counts = {
            table: int(source.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in (
                "crawlers",
                "workflows",
                "normalization_rules",
                "raw_jobs",
                "job_field_overrides",
            )
        }
        # 지워진 칸의 규칙은 들이지 않는다. 이 파일은 0016 이전에 뜬 것이라 `department`
        # 규칙 둘이 들어 있고, 들어오면 그 뒤의 정규화가 한 건도 되지 않는다
        # (`app/api/import_data.py`)
        placeholders = ", ".join("?" for _ in NORMALIZED_FIELDS)
        counts["normalization_rules_kept"] = int(
            source.execute(
                f"SELECT count(*) FROM normalization_rules WHERE field_name IN ({placeholders})",
                NORMALIZED_FIELDS,
            ).fetchone()[0]
        )
        return counts
    finally:
        source.close()


def _added(result: ImportResult) -> tuple[int, int, int, int, int]:
    return (
        result.crawlers_added,
        result.workflows_added,
        result.rules_added,
        result.raw_added,
        result.overrides_added,
    )


def test_연도_없는_날짜_형식은_빼고_들이고_읽지_못하는_규칙은_건너뛴다(
    conn: sqlite3.Connection, tmp_path: pathlib.Path
) -> None:
    """0027 이전에 뜬 파일의 마감일 규칙이 그대로 들어오면 `load_rules` 가 터져 가져온 공고가
    한 건도 정규화되지 않는다. 형식만 빼고 들이고, 그래도 못 읽는 규칙은 건너뛴다."""
    upload = make_upload(
        tmp_path / "upload.db",
        jobs=[job("공고", deadline="2026.12.31")],
        rules=[
            ("deadline", "date_parse", json.dumps({"formats": ["%Y.%m.%d", "%m/%d"]}), 10, None),
            ("deadline", "date_parse", json.dumps({"formats": ["%m.%d"]}), 20, None),
            ("title", "regex", json.dumps({"pattern": "([unclosed"}), 30, None),
        ],
    )

    result = import_database(conn, upload)

    assert result.rules_added == 1
    assert result.rules_skipped == 2
    stored = conn.execute("SELECT rule_config_json FROM normalization_rules").fetchall()
    assert [json.loads(row[0])["formats"] for row in stored] == [["%Y.%m.%d"]]
    assert result.normalize_failed == 0
    assert result.normalized_added == 1
    assert rows(conn, "SELECT deadline FROM normalized_jobs") == [("2026-12-31",)]


def test_연도_없는_형식을_빼는_모양이_0027_과_같다() -> None:
    """같은 규칙이 이미 0027 로 정리돼 있으면 가져온 규칙과 글자 그대로 같아야 중복으로 걸린다."""
    config = json.dumps({"formats": ["%Y.%m.%d", "%m/%d", "%m.%d"], "output_format": "%Y-%m-%d"})
    migration = (
        pathlib.Path(__file__).resolve().parent.parent
        / "migrations"
        / "0027_drop_yearless_date_formats.sql"
    ).read_text(encoding="utf-8")
    up = migration.partition("-- migrate:up")[2].partition("-- migrate:down")[0]
    memory = sqlite3.connect(":memory:")
    memory.execute("CREATE TABLE normalization_rules (rule_type TEXT, rule_config_json TEXT)")
    memory.execute("INSERT INTO normalization_rules VALUES ('date_parse', ?)", (config,))
    memory.executescript(up)
    (migrated,) = memory.execute("SELECT rule_config_json FROM normalization_rules").fetchone()

    assert without_yearless_formats("date_parse", config) == migrated
    assert without_yearless_formats("regex", '{"pattern": "%m/%d"}') == '{"pattern": "%m/%d"}'
