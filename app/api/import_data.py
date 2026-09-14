"""올라온 SQLite 파일을 검증하고 기존 데이터에 더한다.

배포된 서버는 빈 DB 로 시작한다. 로컬에 쌓인 수집 데이터를 옮길 길이 화면에 없으면 운영자는
SSH 와 `docker cp` 로 파일을 밀어 넣게 되고, 그것은 이 서비스가 쓰라고 만든 길이 아니다.

## 덮지 않는다. 더한다

`raw_jobs` 는 append-only 다 (`.claude/rules/data-safety.md`). 올린 파일의 내용으로 기존 행을
고치지 않는다. 없는 것만 넣고, 있는 것은 건너뛴 건수로만 보고한다.

같은 공고인지는 `content_hash` 가 가른다. 그 값은 `app/crawler/hashing.py` 가 만들고 여기서
다시 만들지 않는다 — 올린 파일에 적힌 해시를 믿는 대신 `raw_data_json` 에서 같은 함수로 다시
계산한다. 그래야 이 서버가 이미 가진 행과 같은 잣대로 비교된다.

## 정규화 값은 가져오지 않는다

올린 파일의 `normalized_jobs` 는 읽지 않는다. 가져온 `raw_jobs` 를 **이 서버의**
`normalization_rules` 로 다시 정규화한다. 저쪽 규칙이 만든 값을 그대로 들여오면 이 서버의
규칙과 어긋난 채로 남는다. 그 어긋남은 다음 재정규화에서 값이 바뀌고서야 드러나고, 그때는
왜 바뀌었는지 아무도 모른다.

규칙 자체는 가져온다. 화면에서 한 줄씩 만든 것이라 안 가져오면 새 서버에서 전부 다시 만들어야
한다. 다만 들여오는 방식은 다른 테이블과 같다 — 없는 것만 더하고, 있는 규칙은 건드리지
않는다. **지워진 칸에 걸린 규칙은 들이지 않는다.** 0016 이전에 뜬 파일에는 `department`
규칙이 들어 있고, 그것이 들어오면 `load_rules` 가 터져 그 뒤의 정규화가 한 건도 되지 않는다
(`migrations/0016_drop_department_category_headcount.sql`).

**이 서버가 읽지 못하는 규칙도 들이지 않는다.** 들어오는 규칙은 화면에서 저장할 때와 같은
`build_rule` 검증을 지난다. 다만 `date_parse` 의 연도 없는 형식(`%m/%d`)은 그 형식만 빼고
들인다 — 0027 이전에 뜬 파일의 마감일 규칙이 거기 걸리는데, 규칙을 통째로 버리면 가져온
공고의 마감일이 날짜로 정리되지 않는다. 운영 DB 에서 같은 일을 한 것이
`migrations/0027_drop_yearless_date_formats.sql` 이다.

같은 규칙인지는 `field_name`, `rule_type`, `rule_config_json`, `priority` 넷으로 가른다.
`note` 는 사람이 읽는 이름표라 판정에 넣지 않는다 — 넣으면 메모만 다른 같은 규칙이
두 벌 쌓이고, 정규화는 그 둘을 차례로 태운다.

순서는 규칙이 먼저, 정규화가 나중이다. 그래야 방금 들여온 규칙이 방금 들여온 공고에 적용된다.

## delivered_at 은 가져오지 않는다

저쪽에서 이미 전달된 행이라도 이 서버의 소비 측은 그것을 받은 적이 없다. 전달 표시를 들여오면
소비 측이 영영 못 받는 공고가 생긴다. 가져온 행은 전부 미전달로 들어간다. 이 파일에
`delivered_at` 을 적는 문장이 없는 것이 그 보장이다 (`.claude/rules/data-safety.md`).

## crawl_runs 도 가져오지 않는다

저쪽 서버의 실행 기록이다. 이 서버에서 일어나지 않은 실행을 이 서버의 통계에 섞으면 "주기가
실제로 도는가" 라는 질문에 답할 수 없게 된다. 같은 이유로 `workflows` 의 누적 카운트와 마지막
실행 시각도 가져오지 않는다.

## 0031 전에 내보낸 파일은 칸 이름을 옮긴다

0031 이 칸 이름을 오공고(Spring) 이름으로 바꿨다 (`migrations/0031_spring_field_names.sql`). 그 전에
내보낸 파일의 셀렉터·수집 원본·규칙·보정은 옛 이름을 쓴다. 그대로 들이면 이 서버가 모르는 이름이라
값이 조용히 빈다. 그래서 마이그레이션과 같은 표(`app/field_names.py`)로 이름만 옮겨 들인다. 값은 한
글자도 바꾸지 않으므로 중복 판정 해시도 그대로다. 받을 칸이 없는 옛 자유 글자 직무의 규칙·보정은
들이지 않는다.

## 올라온 파일은 신뢰하지 않는다

임의의 파일이 들어온다. 손대기 전에 SQLite 인지, 우리가 읽을 테이블과 컬럼이 있는지,
마이그레이션 버전이 이 서버보다 앞서지 않는지 본다. 앞선 파일은 거절한다 — 이 서버가 모르는
컬럼을 읽을 방법이 없다.

거절 사유는 무엇이 틀렸는지 이름을 댄다. "잘못된 파일" 은 운영자가 다음에 무엇을 할지
알려주지 않는다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app import db, field_names
from app.crawler.hashing import content_hash
from app.llm import settings as llm_settings
from app.normalize.engine import (
    NormalizeError,
    RawJobMissingError,
    insert_normalized,
    load_rules,
)
from app.normalize.rules import RuleConfigError, build_rule, without_yearless_formats

logger = logging.getLogger(__name__)

# 올릴 수 있는 파일 크기 상한. 공고 한 건이 4KB 쯤이므로 64MB 는 1만 건을 훨씬 넘는다
# (`seeds/snapshot/README.md`). 상한이 없으면 디스크가 상한이 된다
MAX_UPLOAD_BYTES = 64 * 1024 * 1024

# 실패 사유를 몇 건까지 들고 있을지. 규칙 하나가 틀리면 같은 문장이 만 번 쌓인다
# (`app/normalize/backfill.py` 와 같은 이유, 같은 값)
MAX_ERRORS = 20

# SQLite 파일의 첫 16바이트. 확장자가 아니라 내용으로 판정한다
SQLITE_MAGIC = b"SQLite format 3\x00"

# 병합이 실제로 읽는 테이블과 컬럼. 읽지 않는 것은 요구하지 않는다 — `normalized_jobs` 와
# `crawl_runs` 가 없는 파일도 가져올 것은 다 가져올 수 있다
READ_TABLES: dict[str, tuple[str, ...]] = {
    "schema_migrations": ("version",),
    "crawlers": (
        "id",
        "name",
        "list_url",
        "detail_url",
        "selectors_json",
        "status",
        "default_company",
    ),
    "workflows": ("id", "crawler_id", "name", "interval_minutes", "status", "auto_stop_threshold"),
    "normalization_rules": (
        "field_name",
        "rule_type",
        "rule_config_json",
        "priority",
        "enabled",
        "note",
    ),
    "raw_jobs": ("id", "workflow_id", "source_url", "raw_data_json", "crawled_at"),
    "job_field_overrides": ("raw_job_id", "field_name", "value", "created_at", "updated_at"),
}


class ImportRejected(Exception):
    """올린 파일을 가져가지 않는다. `reason` 이 무엇이 틀렸는지를 이름으로 말한다."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def server_version(conn: sqlite3.Connection) -> str:
    """이 서버에 적용된 마지막 마이그레이션 버전. 하나도 없으면 빈 문자열이다."""
    applied = db.applied_versions(conn)
    return applied[-1] if applied else ""


def inspect_upload(path: Path, *, server_version: str) -> str:
    """검증한다. 통과하면 올린 파일의 마이그레이션 버전을 돌려준다.

    여기서는 올린 파일만 읽는다. 검증만으로 이 서버의 DB 는 한 글자도 바뀌지 않는다.
    """
    if not server_version:
        raise ImportRejected(
            "server_not_migrated",
            "이 서버에 스키마가 아직 없다. 마이그레이션을 먼저 적용한다",
        )

    _check_size(path)
    _check_magic(path)

    source = _open_read_only(path)
    try:
        tables = _table_names(source)
        if "schema_migrations" not in tables:
            raise ImportRejected(
                "missing_table",
                "schema_migrations 테이블이 없다. 이 서비스가 만든 DB 파일이 아니다",
            )
        version = _upload_version(source)
        if version > server_version:
            raise ImportRejected(
                "ahead_migration",
                f"올린 파일의 마이그레이션 버전 {version} 이 이 서버 {server_version} 보다"
                " 앞선다. 이 서버가 모르는 컬럼은 읽을 수 없다",
            )
        _check_tables(source, tables)
    finally:
        source.close()
    return version


def _check_size(path: Path) -> None:
    size = path.stat().st_size
    if size == 0:
        raise ImportRejected("empty_file", "빈 파일이다")
    if size > MAX_UPLOAD_BYTES:
        raise ImportRejected(
            "too_large",
            f"파일이 {size}바이트다. 상한은 {MAX_UPLOAD_BYTES}바이트다",
        )


def _check_magic(path: Path) -> None:
    with path.open("rb") as handle:
        header = handle.read(len(SQLITE_MAGIC))
    if header != SQLITE_MAGIC:
        raise ImportRejected("not_sqlite", "SQLite 파일이 아니다. 파일 머리말이 맞지 않는다")


def _open_read_only(path: Path) -> sqlite3.Connection:
    """읽기 전용으로 연다. 올린 파일에는 쓰지 않는다."""
    uri = f"file:{path}?mode=ro"
    try:
        source = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise ImportRejected("not_sqlite", f"파일을 열지 못했다: {exc}") from exc
    source.row_factory = sqlite3.Row
    try:
        source.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.DatabaseError as exc:
        source.close()
        raise ImportRejected("not_sqlite", f"SQLite 파일로 읽지 못했다: {exc}") from exc
    return source


def _table_names(source: sqlite3.Connection) -> set[str]:
    rows = source.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row["name"]) for row in rows}


def _upload_version(source: sqlite3.Connection) -> str:
    row = source.execute("SELECT max(version) AS version FROM schema_migrations").fetchone()
    version = row["version"] if row is not None else None
    if version is None:
        raise ImportRejected(
            "no_migration_version",
            "schema_migrations 가 비어 있다. 어느 스키마인지 알 수 없다",
        )
    return str(version)


def _check_tables(source: sqlite3.Connection, tables: set[str]) -> None:
    for table, columns in READ_TABLES.items():
        if table not in tables:
            raise ImportRejected("missing_table", f"{table} 테이블이 없다")
        present = {
            str(row["name"]) for row in source.execute(f"PRAGMA table_info({table})").fetchall()
        }
        missing = [column for column in columns if column not in present]
        if missing:
            raise ImportRejected(
                "missing_column",
                f"{table} 에 컬럼이 없다: {', '.join(missing)}",
            )


@dataclass(frozen=True)
class ImportResult:
    """무엇이 몇 건 들어왔는지. 화면이 이 숫자를 항목별로 그대로 적는다.

    뭉뚱그린 숫자 하나로는 무엇이 들어왔는지 알 수 없다. 크롤러가 안 들어와서 0건인 것과
    이미 있어서 0건인 것은 운영자가 할 일이 서로 다르다.
    """

    version: str
    crawlers_added: int = 0
    crawlers_skipped: int = 0
    workflows_added: int = 0
    workflows_skipped: int = 0
    rules_added: int = 0
    rules_skipped: int = 0
    raw_added: int = 0
    raw_duplicate: int = 0
    overrides_added: int = 0
    overrides_skipped: int = 0
    llm_added: int = 0
    llm_skipped: int = 0
    normalized_added: int = 0
    normalize_failed: int = 0
    errors: tuple[str, ...] = ()


def import_database(conn: sqlite3.Connection, path: Path) -> ImportResult:
    """올린 파일을 검증하고 이 서버의 데이터에 더한다. 트랜잭션 하나다.

    중간에 무엇이 틀어지든 아무것도 남지 않는다. 크롤러만 들어오고 공고는 안 들어온 절반짜리
    상태는 운영자가 손으로 풀 수 없다.

    한 건의 정규화 실패는 이 트랜잭션을 되돌리지 않는다. `raw_jobs` 는 남고
    `normalized_jobs` 만 비는데, 그것은 크롤링이 이미 그렇게 동작하고(`app/crawler/runner.py`)
    규칙을 고쳐 재정규화하면 복구되는 상태다. 수집 데이터를 통째로 되돌리는 쪽이 손해가 크다.
    """
    version = inspect_upload(path, server_version=server_version(conn))
    source = _open_read_only(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = _merge(conn, source, version)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        source.close()
    logger.info("데이터 가져오기: %s", result)
    return result


def _merge(conn: sqlite3.Connection, source: sqlite3.Connection, version: str) -> ImportResult:
    """열린 트랜잭션 안에서 도는 병합 본체.

    순서가 정해져 있다. 크롤러가 있어야 워크플로우가 매달리고, 워크플로우가 있어야 공고가
    매달린다. 규칙과 보정은 정규화보다 앞에 와야 방금 들여온 공고에 적용된다.
    """
    # 0031 전에 내보낸 파일은 칸 이름이 옛 이름이다. 들여오면서 이 서버의 이름으로 옮긴다
    renamed = field_names.renamed_before(version)
    crawler_ids, crawlers_added, crawlers_skipped = _merge_crawlers(conn, source, renamed=renamed)
    workflow_ids, workflows_added, workflows_skipped = _merge_workflows(conn, source, crawler_ids)
    rules_added, rules_skipped = _merge_rules(conn, source, renamed=renamed)
    raw_ids, new_raw_ids, raw_duplicate = _merge_raw_jobs(
        conn, source, workflow_ids, renamed=renamed
    )
    overrides_added, overrides_skipped = _merge_overrides(conn, source, raw_ids, renamed=renamed)
    llm_added, llm_skipped = _merge_llm_settings(conn, source)
    normalized_added, normalize_failed, errors = _normalize(conn, new_raw_ids)
    return ImportResult(
        version=version,
        crawlers_added=crawlers_added,
        crawlers_skipped=crawlers_skipped,
        workflows_added=workflows_added,
        workflows_skipped=workflows_skipped,
        rules_added=rules_added,
        rules_skipped=rules_skipped,
        raw_added=len(new_raw_ids),
        raw_duplicate=raw_duplicate,
        overrides_added=overrides_added,
        overrides_skipped=overrides_skipped,
        llm_added=llm_added,
        llm_skipped=llm_skipped,
        normalized_added=normalized_added,
        normalize_failed=normalize_failed,
        errors=tuple(errors),
    )


def _merge_crawlers(
    conn: sqlite3.Connection, source: sqlite3.Connection, *, renamed: bool = False
) -> tuple[dict[int, int], int, int]:
    """크롤러를 더한다. 이름과 리스트 URL 이 같으면 같은 크롤러로 본다.

    셀렉터를 포함해 통째로 가져온다. `selectors_json` 에는 사람이 손으로 고친 것이 섞여 있고,
    수집 방식을 놓치면 JS 로 그려지는 사이트가 정적으로 돌아 0건이 나온다. `status` 도
    그대로다 — `promoted` 인 크롤러를 `draft` 로 들여오면 워크플로우가 매달릴 곳이 없다.

    0008 이전에 뜬 파일에는 `list_mode` 대신 `render_mode` 하나가 있다. 그 값을 목록과 상세
    양쪽에 넣는다 — 그때는 한 값이 크롤러 전체의 경로였다. `api_config_json` 은 그 파일에
    없으므로 NULL 로 들어가고, `api` 모드였던 크롤러도 있을 수 없다.
    """
    known = {
        (str(row["name"]), str(row["list_url"])): int(row["id"])
        for row in conn.execute("SELECT id, name, list_url FROM crawlers ORDER BY id DESC")
    }
    mapping: dict[int, int] = {}
    added = skipped = 0
    columns = {str(row["name"]) for row in source.execute("PRAGMA table_info(crawlers)").fetchall()}
    modes = (
        "list_mode, detail_mode, api_config_json"
        if "list_mode" in columns
        else "render_mode AS list_mode, render_mode AS detail_mode, NULL AS api_config_json"
    )
    for row in source.execute(
        f"""
        SELECT id, name, list_url, detail_url, selectors_json, {modes}, status,
               default_company
          FROM crawlers ORDER BY id
        """
    ):
        key = (str(row["name"]), str(row["list_url"]))
        existing = known.get(key)
        if existing is not None:
            mapping[int(row["id"])] = existing
            skipped += 1
            continue
        selectors = row["selectors_json"]
        api_config = row["api_config_json"]
        if renamed:
            selectors = field_names.selectors_json(selectors)
            api_config = field_names.api_config_json(api_config)
        cursor = conn.execute(
            """
            INSERT INTO crawlers (name, list_url, detail_url, selectors_json, list_mode,
                                  detail_mode, api_config_json, status, default_company)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["name"],
                row["list_url"],
                row["detail_url"],
                selectors,
                row["list_mode"],
                row["detail_mode"],
                api_config,
                row["status"],
                row["default_company"],
            ),
        )
        new_id = int(cursor.lastrowid or 0)
        known[key] = new_id
        mapping[int(row["id"])] = new_id
        added += 1
    return mapping, added, skipped


def _merge_workflows(
    conn: sqlite3.Connection, source: sqlite3.Connection, crawler_ids: dict[int, int]
) -> tuple[dict[int, int], int, int]:
    """워크플로우를 더한다. 크롤러가 같고 이름이 같으면 같은 워크플로우로 본다.

    누적 카운트와 마지막 실행 시각은 가져오지 않는다. 저쪽 서버의 실행 기록이고, 이 서버에서
    일어나지 않은 실행을 이 서버의 통계에 섞으면 자동 중지 판정까지 흔들린다. `crawl_runs` 를
    가져오지 않는 것과 같은 이유다.
    """
    known = {
        (int(row["crawler_id"]), str(row["name"])): int(row["id"])
        for row in conn.execute("SELECT id, crawler_id, name FROM workflows ORDER BY id DESC")
    }
    mapping: dict[int, int] = {}
    added = skipped = 0
    for row in source.execute(
        """
        SELECT id, crawler_id, name, interval_minutes, status, auto_stop_threshold
          FROM workflows ORDER BY id
        """
    ):
        crawler_id = crawler_ids.get(int(row["crawler_id"]))
        if crawler_id is None:
            raise ImportRejected(
                "broken_reference",
                f"워크플로우 {row['name']!r} 가 없는 크롤러 {row['crawler_id']} 를 가리킨다",
            )
        key = (crawler_id, str(row["name"]))
        existing = known.get(key)
        if existing is not None:
            mapping[int(row["id"])] = existing
            skipped += 1
            continue
        cursor = conn.execute(
            """
            INSERT INTO workflows (crawler_id, name, interval_minutes, status,
                                   auto_stop_threshold)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                crawler_id,
                row["name"],
                row["interval_minutes"],
                row["status"],
                row["auto_stop_threshold"],
            ),
        )
        new_id = int(cursor.lastrowid or 0)
        known[key] = new_id
        mapping[int(row["id"])] = new_id
        added += 1
    return mapping, added, skipped


def _merge_rules(
    conn: sqlite3.Connection, source: sqlite3.Connection, *, renamed: bool = False
) -> tuple[int, int]:
    """정규화 규칙을 더한다. 이미 있는 규칙은 건드리지 않는다.

    같은 규칙인지는 `field_name`, `rule_type`, `rule_config_json`, `priority` 넷으로 가른다.
    `note` 는 사람이 읽는 이름표라 판정에 넣지 않는다 — 넣으면 메모만 다른 같은 규칙이 두 벌
    쌓이고, 정규화는 그 둘을 차례로 태운다.

    이 서버가 읽지 못하는 규칙은 건너뛴 것으로 센다 — 지워진 칸에 걸린 규칙도, 설정이 지금의
    검증을 통과하지 못하는 규칙도. 화면으로는 저장할 수 없는 규칙이라
    (`app/normalize/rules.py` 의 `build_rule`) 파일로 들어오는 길만 열어 둘 이유가 없고,
    들어오면 `load_rules` 가 그 파일의 공고 전부를 정규화하지 못한다.

    연도 없는 날짜 형식만은 그 형식을 빼고 들인다. 운영 DB 에서 같은 일을 한 것이
    `migrations/0027_drop_yearless_date_formats.sql` 이고, 빼는 모양도 그것과 같다.
    """
    columns = "field_name, rule_type, rule_config_json, priority"
    known = {
        (str(row[0]), str(row[1]), str(row[2]), int(row[3]))
        for row in conn.execute(f"SELECT {columns} FROM normalization_rules")
    }
    added = skipped = 0
    for row in source.execute(
        f"SELECT {columns}, enabled, note FROM normalization_rules ORDER BY id"
    ):
        rule_type = str(row["rule_type"])
        name = str(row["field_name"])
        if renamed:
            translated = field_names.field_name(name)
            if translated is None:
                # 옛 자유 글자 직무에 걸린 규칙이다. 받을 칸이 없다
                skipped += 1
                continue
            name = translated
        key = (
            name,
            rule_type,
            without_yearless_formats(rule_type, str(row["rule_config_json"])),
            int(row["priority"]),
        )
        if key in known or not _readable(*key):
            skipped += 1
            continue
        conn.execute(
            """
            INSERT INTO normalization_rules (field_name, rule_type, rule_config_json, priority,
                                             enabled, note)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (*key, row["enabled"], row["note"]),
        )
        known.add(key)
        added += 1
    return added, skipped


def _readable(field_name: str, rule_type: str, config: str, priority: int) -> bool:
    """이 서버의 `load_rules` 가 읽을 수 있는 규칙인가. 화면에서 저장할 때와 같은 검증이다."""
    try:
        build_rule(field_name, rule_type, config, priority=priority)
    except RuleConfigError:
        return False
    return True


def _merge_llm_settings(conn: sqlite3.Connection, source: sqlite3.Connection) -> tuple[int, int]:
    """AI 제공자 설정을 더한다. 이미 값이 있는 항목은 건드리지 않는다.

    **키가 같이 옮겨지는 것은 결정된 사항이다** (2026-08-27,
    `.claude/tasks/todo/prd-llm-providers.md`). 서버를 옮길 때 키를 다시 넣지 않아도 되는
    편이 낫다는 판단이고, 그래서 내보내기 화면이
    이 파일에 키가 들어 있다고 알린다.

    옮기는 것은 `app/llm/settings.py` 의 행뿐이다. `app_settings` 를 통째로 옮기면 알림 주소와
    동시 실행 상한까지 따라와서, 이 서버의 운영 설정이 남의 파일 하나로 바뀐다.

    이미 있는 값을 덮지 않는 것은 가져오기 전체의 규칙과 같다. 지금 도는 서버의 키가 올린
    파일의 키로 조용히 바뀌면, 다음 호출이 어느 계정에서 나가는지 아무도 모른다.
    """
    if "app_settings" not in _table_names(source):
        # 이 표가 없는 옛 파일도 나머지는 다 가져올 수 있다
        return 0, 0

    # 화면에서 추가한 회사의 정의와 키는 이름이 정해져 있지 않아 행 목록으로 고를 수 없다.
    # 전부 읽고 이 저장소의 행만 남긴다 (`app/llm/settings.py` 의 `is_llm_row`)
    known = {
        str(row["key"])
        for row in conn.execute("SELECT key FROM app_settings")
        if llm_settings.is_llm_row(str(row["key"]))
    }
    added = skipped = 0
    for row in source.execute("SELECT key, value FROM app_settings ORDER BY key"):
        if not llm_settings.is_llm_row(str(row["key"])):
            continue
        if str(row["key"]) in known:
            skipped += 1
            continue
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)",
            (str(row["key"]), str(row["value"])),
        )
        added += 1
    return added, skipped


def _merge_raw_jobs(
    conn: sqlite3.Connection,
    source: sqlite3.Connection,
    workflow_ids: dict[int, int],
    *,
    renamed: bool = False,
) -> tuple[dict[int, int], list[int], int]:
    """공고를 더한다. 없는 것만 넣고 기존 행은 한 글자도 고치지 않는다.

    `content_hash` 는 올린 파일에 적힌 값을 믿지 않고 `raw_data_json` 에서 다시 계산한다.
    그래야 이 서버가 이미 가진 행과 같은 잣대로 비교된다 (`app/crawler/hashing.py`).

    중복 판정 범위는 워크플로우 안이다. 크롤링이 쓰는 범위와 같다 — 여기서만 전역으로 보면
    같은 파일을 두 번 올린 결과와 크롤링이 한 번 더 돈 결과가 서로 달라진다.

    돌려주는 것은 셋이다. 올린 파일의 id 에서 이 서버의 id 로 가는 지도(보정이 쓴다), 새로
    들어온 id 목록(정규화가 쓴다), 중복이라 건너뛴 건수.
    """
    known = {
        (int(row["workflow_id"]), str(row["content_hash"])): int(row["id"])
        for row in conn.execute(
            "SELECT id, workflow_id, content_hash FROM raw_jobs ORDER BY id DESC"
        )
    }
    mapping: dict[int, int] = {}
    added: list[int] = []
    duplicate = 0
    for row in source.execute(
        """
        SELECT id, workflow_id, source_url, raw_data_json, crawled_at
          FROM raw_jobs ORDER BY id
        """
    ):
        workflow_id = workflow_ids.get(int(row["workflow_id"]))
        if workflow_id is None:
            raise ImportRejected(
                "broken_reference",
                f"공고 {row['id']} 가 없는 워크플로우 {row['workflow_id']} 를 가리킨다",
            )
        fields = _raw_fields(row)
        raw_data_json = str(row["raw_data_json"])
        if renamed:
            fields = field_names.record(fields)
            raw_data_json = json.dumps(fields, ensure_ascii=False)
        digest = content_hash(fields)
        key = (workflow_id, digest)
        existing = known.get(key)
        if existing is not None:
            mapping[int(row["id"])] = existing
            duplicate += 1
            continue
        cursor = conn.execute(
            """
            INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash,
                                  crawled_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (workflow_id, row["source_url"], raw_data_json, digest, row["crawled_at"]),
        )
        new_id = int(cursor.lastrowid or 0)
        known[key] = new_id
        mapping[int(row["id"])] = new_id
        added.append(new_id)
    return mapping, added, duplicate


def _raw_fields(row: sqlite3.Row) -> dict[str, object]:
    """`raw_data_json` 을 필드 묶음으로 읽는다. 읽히지 않으면 거절한다.

    값은 고치지 않는다. 0031 전에 내보낸 파일이면 키 이름만 이 서버의 이름으로 옮기는데, 그것은
    이 함수가 아니라 `_merge_raw_jobs` 가 한다.
    """
    try:
        data = json.loads(str(row["raw_data_json"]))
    except json.JSONDecodeError as exc:
        raise ImportRejected(
            "broken_row", f"공고 {row['id']} 의 raw_data_json 을 읽지 못했다: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ImportRejected(
            "broken_row",
            f"공고 {row['id']} 의 raw_data_json 이 객체가 아니다: {type(data).__name__}",
        )
    return data


def _merge_overrides(
    conn: sqlite3.Connection,
    source: sqlite3.Connection,
    raw_ids: dict[int, int],
    *,
    renamed: bool = False,
) -> tuple[int, int]:
    """사람이 검수한 값을 가져온다. 다시 만들 수 없는 값이라 빠뜨리지 않는다.

    이 서버에 이미 그 공고·그 번호의 그 필드가 있으면 건너뛴다. 이쪽 사람이 고쳐 둔 값을 저쪽 값으로
    덮지 않는다.

    중복이라 건너뛴 공고에 붙은 보정도 가져온다. 그 공고의 확정 값은 다음 재정규화에서 바뀐다 —
    보정을 저장하는 검수 화면이 이미 그 순서로 동작한다 (`app/api/review.py`).
    """
    known = {
        (int(row["raw_job_id"]), int(row["part"]), str(row["field_name"]))
        for row in conn.execute("SELECT raw_job_id, part, field_name FROM job_field_overrides")
    }
    columns = {
        str(row["name"])
        for row in source.execute("PRAGMA table_info(job_field_overrides)").fetchall()
    }
    # 0029 전에 내보낸 파일에는 번호 칸이 없다. 그때는 공고를 나누지 않았으니 전부 1번이다
    part = "part" if "part" in columns else "1 AS part"
    added = skipped = 0
    for row in source.execute(
        f"""
        SELECT raw_job_id, {part}, field_name, value, created_at, updated_at
          FROM job_field_overrides ORDER BY id
        """
    ):
        raw_job_id = raw_ids.get(int(row["raw_job_id"]))
        if raw_job_id is None:
            raise ImportRejected(
                "broken_reference",
                f"보정이 없는 공고 {row['raw_job_id']} 를 가리킨다",
            )
        name = str(row["field_name"])
        if renamed:
            translated = field_names.field_name(name)
            if translated is None:
                # 옛 자유 글자 직무를 고친 값이다. 받을 칸이 없다
                skipped += 1
                continue
            name = translated
        key = (raw_job_id, int(row["part"]), name)
        if key in known:
            skipped += 1
            continue
        conn.execute(
            """
            INSERT INTO job_field_overrides (raw_job_id, part, field_name, value, created_at,
                                             updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                raw_job_id,
                row["part"],
                name,
                row["value"],
                row["created_at"],
                row["updated_at"],
            ),
        )
        known.add(key)
        added += 1
    return added, skipped


def _normalize(conn: sqlite3.Connection, raw_ids: list[int]) -> tuple[int, int, list[str]]:
    """새로 들어온 공고를 **이 서버의** 규칙으로 정규화한다.

    올린 파일의 `normalized_jobs` 는 읽지 않는다. `delivered_at` 도 쓰지 않는다 — 여기서
    부르는 `insert_normalized` 가 그 컬럼을 적지 않는 것이 그 보장이다
    (`.claude/rules/data-safety.md`).
    """
    if not raw_ids:
        return 0, 0, []
    try:
        rules = load_rules(conn)
    except NormalizeError as exc:
        # 규칙을 못 읽으면 어떤 건도 정규화할 수 없다. 수집 데이터는 그대로 들어간다
        return 0, len(raw_ids), [f"정규화 규칙을 읽지 못했다: {exc}"]

    added = failed = 0
    errors: list[str] = []
    for raw_id in raw_ids:
        try:
            insert_normalized(conn, raw_id, rules)
            added += 1
        except (NormalizeError, RawJobMissingError) as exc:
            failed += 1
            if len(errors) < MAX_ERRORS:
                errors.append(f"raw_jobs {raw_id}: {exc}")
    return added, failed, errors
