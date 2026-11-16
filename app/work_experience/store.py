"""`work_experiences` 표를 읽고 쓴다 (`migrations/0049_work_experiences.sql`).

프로그램 하나가 행 하나다. CSV 를 올리면 줄마다 넣거나 바꾼다 (`app/work_experience/source.py`).
읽은 값이 그대로면(`page_hash`) AI 를 다시 부르지 않고, 바뀌었으면 새 값으로 덮고 정리·전송을 새로
한다.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import date
from typing import Any

from app import companies
from app.work_experience import source
from app.work_experience.fill import ProgramFill
from app.work_experience.source import ListItem, Program

NEW = "new"
CHANGED = "changed"
SAME = "same"


def _digest(data: dict[str, Any]) -> str:
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def card_hash(item: ListItem) -> str:
    return _digest(item.card_values())


def page_hash(item: ListItem, program: Program) -> str:
    """카드와 상세에서 읽은 값 전체의 해시."""
    return _digest({**item.card_values(), **_program_json(program)})


def upsert(conn: sqlite3.Connection, item: ListItem, program: Program) -> tuple[int, str]:
    """프로그램을 넣거나 바꾼다. (행 id, 새로 들어왔나·바뀌었나·같은가) 를 돌려준다."""
    digest = page_hash(item, program)
    values: dict[str, Any] = {
        "external_id": item.program_id,
        "type_code": item.type_code,
        "type_label": program.type_label or item.type_label,
        "title": program.title or item.title,
        "company": item.company,
        "operator": item.operator,
        "job": item.job,
        "region_label": program.region or item.region,
        "headcount": item.headcount,
        "recruitment_start_date": _iso(program.recruitment_start or item.recruitment_start),
        "recruitment_end_date": _iso(program.recruitment_end or item.recruitment_end),
        "work_start_date": _iso(program.work_start),
        "work_end_date": _iso(program.work_end),
        "sections_json": json.dumps(program.sections, ensure_ascii=False),
        "card_hash": card_hash(item),
        "page_hash": digest,
    }
    row = conn.execute(
        "SELECT id, page_hash FROM work_experiences WHERE source_url = ?", (item.source_url,)
    ).fetchone()
    if row is not None:
        save_share_url(conn, item)
    if row is None:
        values["share_url"] = item.share_url or None
        columns = ["source_url", *values]
        cursor = conn.execute(
            f"INSERT INTO work_experiences ({', '.join(columns)})"
            f" VALUES ({', '.join('?' for _ in columns)})",
            (item.source_url, *values.values()),
        )
        return int(cursor.lastrowid or 0), NEW
    if row["page_hash"] == digest:
        conn.execute(
            "UPDATE work_experiences SET card_hash = ?, last_seen_at = datetime('now')"
            " WHERE id = ?",
            (values["card_hash"], row["id"]),
        )
        return int(row["id"]), SAME
    assignments = ", ".join(f"{name} = ?" for name in values)
    conn.execute(
        f"UPDATE work_experiences SET {assignments}, fill_error = '', send_attempts = 0,"
        " last_seen_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
        (*values.values(), row["id"]),
    )
    return int(row["id"]), CHANGED


def save_share_url(conn: sqlite3.Connection, item: ListItem) -> None:
    """공유 주소를 둔다. 비어 있으면 읽어 둔 주소를 지우지 않는다 — 매핑이 한 번 빠진 날에 오공고
    주소가 열리지 않는 상세 주소로 돌아가지 않게 한다 (LC-3615).
    """
    if item.share_url:
        conn.execute(
            "UPDATE work_experiences SET share_url = ? WHERE source_url = ?",
            (item.share_url, item.source_url),
        )


def to_fill(
    conn: sqlite3.Connection, today: str, exclude: frozenset[int] | set[int] = frozenset()
) -> list[sqlite3.Row]:
    """AI 로 정리할 프로그램. 아직 정리하지 않았거나 값이 바뀐 것 중 모집이 끝나지 않은 것이다."""
    rows = conn.execute(
        "SELECT * FROM work_experiences"
        " WHERE (filled_hash IS NULL OR filled_hash <> page_hash) AND recruitment_end_date >= ?"
        " ORDER BY id",
        (today,),
    ).fetchall()
    return [row for row in rows if int(row["id"]) not in exclude]


def program_of(row: sqlite3.Row) -> Program:
    """저장한 행을 AI 에게 줄 프로그램으로 되살린다."""
    return Program(
        program_id=str(row["external_id"]),
        title=str(row["title"]),
        type_label=str(row["type_label"] or ""),
        region=str(row["region_label"] or ""),
        job_and_headcount=sections_of(row).get("모집정보", {}).get("직무(모집인원)", ""),
        recruitment_start=_day(row["recruitment_start_date"]),
        recruitment_end=_day(row["recruitment_end_date"]),
        work_start=_day(row["work_start_date"]),
        work_end=_day(row["work_end_date"]),
        sections=sections_of(row),
    )


def needs_fill(conn: sqlite3.Connection, row_id: int) -> bool:
    row = conn.execute(
        "SELECT page_hash, filled_hash FROM work_experiences WHERE id = ?", (row_id,)
    ).fetchone()
    return row is not None and row["filled_hash"] != row["page_hash"]


def save_fill(conn: sqlite3.Connection, row_id: int, fill: ProgramFill) -> None:
    conn.execute(
        """
        UPDATE work_experiences
           SET fill_json = ?, filled_hash = page_hash, fill_error = '', updated_at = datetime('now')
         WHERE id = ?
        """,
        (json.dumps(asdict(fill), ensure_ascii=False), row_id),
    )


def save_fill_error(conn: sqlite3.Connection, row_id: int, error: str) -> None:
    conn.execute(
        "UPDATE work_experiences SET fill_error = ?, updated_at = datetime('now') WHERE id = ?",
        (error[:500], row_id),
    )


def fill_of(row: sqlite3.Row) -> ProgramFill | None:
    """저장한 AI 정리. 아직 없으면 None 이다. 모르는 키는 버린다 — 칸이 줄어든 뒤의 옛 행이다."""
    if not row["fill_json"]:
        return None
    data = json.loads(str(row["fill_json"]))
    known = ProgramFill.__dataclass_fields__
    return ProgramFill(**{key: value for key, value in data.items() if key in known})


def sections_of(row: sqlite3.Row) -> dict[str, dict[str, str]]:
    return dict(json.loads(str(row["sections_json"] or "{}")))


def register_companies(conn: sqlite3.Connection) -> None:
    """모은 프로그램의 참여기업을 회사 표에 둔다. 있는 행은 고치지 않는다.

    회사 화면에서 로고를 올릴 자리를 만든다. 공고는 정규화가 회사 행을 만들지만 이 수집기는 그 길을
    지나지 않는다. 참여기업은 저마다 다른 회사라 모회사를 두지 않는다.
    """
    for row in conn.execute(
        "SELECT DISTINCT company FROM work_experiences WHERE company <> ''"
    ).fetchall():
        companies.register(conn, source.main_company(str(row["company"])), None)


def logo_url(conn: sqlite3.Connection, company: str) -> str | None:
    """참여기업에 등록한 로고. 없으면 None 이다. 읽기 전용이다."""
    found = companies.read(conn, company) if company.strip() else None
    return found.logo_url if found is not None and found.logo_url else None


def listing(conn: sqlite3.Connection, limit: int = 300) -> list[sqlite3.Row]:
    """화면 목록. 모집 마감이 늦은 프로그램이 위다 — 아직 신청할 수 있는 것부터 본다."""
    return conn.execute(
        "SELECT * FROM work_experiences ORDER BY recruitment_end_date DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def _program_json(program: Program) -> dict[str, Any]:
    data = asdict(program)
    return {key: _iso(value) if isinstance(value, date) else value for key, value in data.items()}


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
