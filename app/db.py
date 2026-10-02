"""SQLite 연결과 마이그레이션 러너.

스키마는 `migrations/` 의 파일로만 바뀐다. DB 파일을 지우고 새로 만드는 경로는 두지 않는다
(`.claude/rules/data-safety.md`).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from app.config import get_settings
from app.normalize import loose_date

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
SCHEMA_TABLE = "schema_migrations"

_UP_MARKER = "-- migrate:up"
_DOWN_MARKER = "-- migrate:down"
_FILENAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class MigrationError(RuntimeError):
    """마이그레이션 파일이 잘못됐거나 적용 상태가 파일과 어긋날 때."""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path
    up_sql: str
    down_sql: str


def connect(database_path: str | Path | None = None) -> sqlite3.Connection:
    """연결을 연다. 외래키 제약을 켜고, 트랜잭션은 러너가 직접 제어한다."""
    path = str(database_path) if database_path is not None else get_settings().database_path
    if path != ":memory:":
        parent = Path(path).expanduser().resolve().parent
        parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: 암묵적 트랜잭션을 끄고 BEGIN/COMMIT 을 명시한다
    # check_same_thread=False: FastAPI 가 의존성과 동기 엔드포인트를 스레드풀에서 돌려 연결을 만든
    # 스레드와 쓰는 스레드가 갈린다. 연결은 요청 1건이 열고 닫으므로 동시에 공유되지는 않는다
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 글자로 적힌 날짜를 `YYYY-MM-DD` 로. 공고 목록이 AI 가 짚은 마감일과 저장된 마감일이 같은
    # 날인지 SQL 안에서 견준다 (`app/api/review_filter.py` 의 `DEADLINE_DIFF_SQL`)
    conn.create_function("loose_day", 1, _loose_day, deterministic=True)
    return conn


def _loose_day(text: object) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    read = loose_date.read(text, date.today(), end=True)
    return read[:10] if read else None


def ensure_schema_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_TABLE} (
            version    TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def applied_versions(conn: sqlite3.Connection) -> list[str]:
    ensure_schema_table(conn)
    rows = conn.execute(f"SELECT version FROM {SCHEMA_TABLE} ORDER BY version").fetchall()
    return [row["version"] for row in rows]


def _parse_migration(path: Path) -> Migration:
    matched = _FILENAME_RE.match(path.name)
    if matched is None:
        raise MigrationError(f"파일명이 NNNN_name.sql 형식이 아니다: {path.name}")
    text = path.read_text(encoding="utf-8")
    if _UP_MARKER not in text or _DOWN_MARKER not in text:
        raise MigrationError(
            f"{path.name} 에 '{_UP_MARKER}' 와 '{_DOWN_MARKER}' 가 모두 있어야 한다"
        )
    _, _, after_up = text.partition(_UP_MARKER)
    up_sql, _, down_sql = after_up.partition(_DOWN_MARKER)
    if not up_sql.strip() or not down_sql.strip():
        raise MigrationError(f"{path.name} 의 up 또는 down 이 비어 있다")
    return Migration(
        version=matched.group(1),
        name=matched.group(2),
        path=path,
        up_sql=up_sql.strip(),
        down_sql=down_sql.strip(),
    )


def load_migrations(directory: Path | None = None) -> list[Migration]:
    """`migrations/` 의 파일을 버전 순으로 읽는다."""
    source = directory or MIGRATIONS_DIR
    if not source.is_dir():
        raise MigrationError(f"마이그레이션 디렉터리가 없다: {source}")
    migrations = sorted(
        (_parse_migration(path) for path in source.glob("*.sql")),
        key=lambda migration: migration.version,
    )
    versions = [migration.version for migration in migrations]
    duplicated = {version for version in versions if versions.count(version) > 1}
    if duplicated:
        raise MigrationError(f"버전이 중복됐다: {sorted(duplicated)}")
    return migrations


def _run(conn: sqlite3.Connection, sql: str, record: tuple[str, ...], is_up: bool) -> None:
    """스크립트와 버전 기록을 트랜잭션 하나로 묶는다.

    `executescript` 는 대기 중인 트랜잭션이 있으면 먼저 COMMIT 하므로, BEGIN 을 스크립트
    안에 넣어 러너가 연 트랜잭션이 중간에 끊기지 않게 한다.
    """
    try:
        conn.executescript(f"BEGIN;\n{sql}")
        if is_up:
            conn.execute(
                f"INSERT INTO {SCHEMA_TABLE} (version, name, applied_at) VALUES (?, ?, ?)",
                record,
            )
        else:
            conn.execute(f"DELETE FROM {SCHEMA_TABLE} WHERE version = ?", record)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def migrate_up(conn: sqlite3.Connection, directory: Path | None = None) -> list[str]:
    """아직 적용되지 않은 마이그레이션만 버전 순으로 적용한다. 적용된 버전을 돌려준다."""
    ensure_schema_table(conn)
    already = set(applied_versions(conn))
    applied: list[str] = []
    for migration in load_migrations(directory):
        if migration.version in already:
            continue
        now = datetime.now(UTC).isoformat(timespec="seconds")
        _run(conn, migration.up_sql, (migration.version, migration.name, now), is_up=True)
        applied.append(migration.version)
    return applied


def migrate_down(
    conn: sqlite3.Connection, steps: int = 1, directory: Path | None = None
) -> list[str]:
    """적용된 마지막 마이그레이션부터 `steps` 개를 역적용한다. 되돌린 버전을 돌려준다."""
    if steps < 1:
        raise ValueError("steps 는 1 이상이어야 한다")
    ensure_schema_table(conn)
    by_version = {migration.version: migration for migration in load_migrations(directory)}
    targets = list(reversed(applied_versions(conn)))[:steps]
    reverted: list[str] = []
    for version in targets:
        migration = by_version.get(version)
        if migration is None:
            raise MigrationError(f"적용된 버전 {version} 의 마이그레이션 파일이 없다")
        _run(conn, migration.down_sql, (version,), is_up=False)
        reverted.append(version)
    return reverted


def migration_status(
    conn: sqlite3.Connection, directory: Path | None = None
) -> list[tuple[str, str]]:
    """(버전, `applied` 또는 `pending`) 목록. 읽기 전용이다."""
    already = set(applied_versions(conn))
    migrations: Iterable[Migration] = load_migrations(directory)
    return [
        (migration.version, "applied" if migration.version in already else "pending")
        for migration in migrations
    ]
