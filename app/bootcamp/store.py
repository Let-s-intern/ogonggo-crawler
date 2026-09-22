"""`bootcamps` 표를 읽고 쓴다 (`migrations/0044_bootcamps.sql`).

과정 하나가 행 하나다. 정리까지 끝난 과정(`is_done`)은 수집이 다시 읽지 않는다 (2026-09-22 결정).
정리에 실패한 과정만 다시 받아 넣는데, 그사이 파서가 읽은 값이 바뀌었으면(`page_hash`) 새 값으로
덮고 시도 수를 새로 센다.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import date
from typing import Any

from app.bootcamp.fill import BootcampFill
from app.bootcamp.sesac import Course, CurriculumGroup

NEW = "new"
CHANGED = "changed"
SAME = "same"


def page_hash(course: Course, thumbnail_url: str) -> str:
    """파서가 읽은 값 전체의 해시. 목록 썸네일도 대표 이미지라 넣는다."""
    data = {**_course_json(course), "thumbnail_url": thumbnail_url}
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def upsert(
    conn: sqlite3.Connection, course: Course, thumbnail_url: str, status_label: str = ""
) -> tuple[int, str]:
    """과정을 넣거나 바꾼다. (행 id, 새로 들어왔나·바뀌었나·같은가) 를 돌려준다.

    모집 상태는 목록 카드의 것(`status_label`)이 먼저다. 없으면 상세의 것이다.
    """
    digest = page_hash(course, thumbnail_url)
    values: dict[str, Any] = {
        "external_id": course.crs_sn,
        "title": course.title,
        "campus": course.campus,
        "category": course.category,
        "status_label": status_label or course.status,
        "recruitment_start_date": _iso(course.recruitment_start),
        "recruitment_end_date": _iso(course.recruitment_end),
        "program_start_date": _iso(course.program_start),
        "program_end_date": _iso(course.program_end),
        "hours": course.hours,
        "thumbnail_url": thumbnail_url or course.og_image_url,
        "overview_text": course.overview_text,
        "overview_images_json": json.dumps(list(course.overview_images), ensure_ascii=False),
        "curriculum_json": json.dumps(
            [_group_json(group) for group in course.curriculum], ensure_ascii=False
        ),
        "page_hash": digest,
    }
    row = conn.execute(
        "SELECT id, page_hash FROM bootcamps WHERE source_url = ?", (course.source_url,)
    ).fetchone()
    if row is None:
        columns = ["source_url", *values]
        cursor = conn.execute(
            f"INSERT INTO bootcamps ({', '.join(columns)})"
            f" VALUES ({', '.join('?' for _ in columns)})",
            (course.source_url, *values.values()),
        )
        return int(cursor.lastrowid or 0), NEW
    if row["page_hash"] == digest:
        touch(conn, course.source_url, str(values["status_label"]))
        return int(row["id"]), SAME
    assignments = ", ".join(f"{name} = ?" for name in values)
    conn.execute(
        f"UPDATE bootcamps SET {assignments}, fill_error = '', send_attempts = 0,"
        " last_seen_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
        (*values.values(), row["id"]),
    )
    return int(row["id"]), CHANGED


def is_done(conn: sqlite3.Connection, source_url: str) -> bool:
    """이미 모아 AI 정리까지 끝난 과정인가. 그런 과정은 상세를 다시 받지 않는다."""
    row = conn.execute(
        "SELECT page_hash, filled_hash FROM bootcamps WHERE source_url = ?", (source_url,)
    ).fetchone()
    return row is not None and row["filled_hash"] == row["page_hash"]


def touch(conn: sqlite3.Connection, source_url: str, status_label: str) -> bool:
    """목록에서 다시 본 과정. 본 시각과 목록 카드의 모집 상태를 적는다. 상태가 바뀌었으면 True.

    상태가 바뀌면 전송 시도 수를 새로 센다 — 오공고에 새 상태를 보내야 한다
    (`app/bootcamp/deliver.py`).
    """
    row = conn.execute(
        "SELECT status_label FROM bootcamps WHERE source_url = ?", (source_url,)
    ).fetchone()
    changed = row is not None and bool(status_label) and row["status_label"] != status_label
    if changed:
        conn.execute(
            "UPDATE bootcamps SET status_label = ?, send_attempts = 0,"
            " last_seen_at = datetime('now'), updated_at = datetime('now') WHERE source_url = ?",
            (status_label, source_url),
        )
    else:
        conn.execute(
            "UPDATE bootcamps SET last_seen_at = datetime('now') WHERE source_url = ?",
            (source_url,),
        )
    return changed


def needs_fill(conn: sqlite3.Connection, bootcamp_id: int) -> bool:
    row = conn.execute(
        "SELECT page_hash, filled_hash FROM bootcamps WHERE id = ?", (bootcamp_id,)
    ).fetchone()
    return row is not None and row["filled_hash"] != row["page_hash"]


def save_fill(conn: sqlite3.Connection, bootcamp_id: int, fill: BootcampFill) -> None:
    conn.execute(
        """
        UPDATE bootcamps
           SET short_description = ?, content = ?, eligibility = ?, capacity = ?,
               manager_email = ?, filled_hash = page_hash, fill_error = '',
               updated_at = datetime('now')
         WHERE id = ?
        """,
        (
            fill.short_description,
            fill.content,
            fill.eligibility_and_selection_process,
            fill.capacity,
            fill.manager_email,
            bootcamp_id,
        ),
    )


def save_fill_error(conn: sqlite3.Connection, bootcamp_id: int, error: str) -> None:
    conn.execute(
        "UPDATE bootcamps SET fill_error = ?, updated_at = datetime('now') WHERE id = ?",
        (error[:500], bootcamp_id),
    )


def curriculum(row: sqlite3.Row) -> list[CurriculumGroup]:
    groups: list[CurriculumGroup] = []
    for item in json.loads(str(row["curriculum_json"] or "[]")):
        groups.append(
            CurriculumGroup(
                name=str(item.get("name", "")),
                first_day=_parse(item.get("first_day")),
                last_day=_parse(item.get("last_day")),
                lessons=tuple(str(lesson) for lesson in item.get("lessons", [])),
            )
        )
    return groups


def listing(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    """화면 목록. 신청할 수 있는 과정이 위고, 그 안에서는 새싹 번호가 큰(최근) 과정이 위다."""
    return conn.execute(
        "SELECT * FROM bootcamps"
        " ORDER BY status_label NOT IN ('모집중', '모집예정'), CAST(external_id AS INTEGER) DESC"
        " LIMIT ?",
        (limit,),
    ).fetchall()


def get(conn: sqlite3.Connection, bootcamp_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM bootcamps WHERE id = ?", (bootcamp_id,)).fetchone()


def _course_json(course: Course) -> dict[str, Any]:
    data = asdict(course)
    data["curriculum"] = [_group_json(group) for group in course.curriculum]
    return {key: _iso(value) if isinstance(value, date) else value for key, value in data.items()}


def _group_json(group: CurriculumGroup) -> dict[str, Any]:
    return {
        "name": group.name,
        "first_day": _iso(group.first_day),
        "last_day": _iso(group.last_day),
        "lessons": list(group.lessons),
    }


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _parse(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
