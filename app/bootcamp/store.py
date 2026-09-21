"""`bootcamps` 표를 읽고 쓴다 (`migrations/0044_bootcamps.sql`).

과정 하나가 행 하나다. 파서가 읽은 값이 바뀌면(`page_hash`) AI 칸과 전송 시도 수를 새로 센다 — 바뀐
안내로 다시 채우고 다시 보내야 해서다. 채운 글과 오공고 id 는 지우지 않는다. 다시 채우기
전까지 보이는 글이 있어야 하고, 다시 보낼 때 id 로 교체해야 한다.
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


def upsert(conn: sqlite3.Connection, course: Course, thumbnail_url: str) -> tuple[int, str]:
    """과정을 넣거나 바꾼다. (행 id, 새로 들어왔나·바뀌었나·같은가) 를 돌려준다."""
    digest = page_hash(course, thumbnail_url)
    values: dict[str, Any] = {
        "external_id": course.crs_sn,
        "title": course.title,
        "campus": course.campus,
        "category": course.category,
        "status_label": course.status,
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
        conn.execute(
            "UPDATE bootcamps SET last_seen_at = datetime('now') WHERE id = ?", (row["id"],)
        )
        return int(row["id"]), SAME
    assignments = ", ".join(f"{name} = ?" for name in values)
    conn.execute(
        f"UPDATE bootcamps SET {assignments}, fill_error = '', send_attempts = 0,"
        " last_seen_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
        (*values.values(), row["id"]),
    )
    return int(row["id"]), CHANGED


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
    """화면 목록. 최근에 본 과정이 위다."""
    return conn.execute(
        "SELECT * FROM bootcamps ORDER BY last_seen_at DESC, id DESC LIMIT ?", (limit,)
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
