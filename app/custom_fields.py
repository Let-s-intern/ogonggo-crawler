"""운영자가 화면에서 더한 수집 항목 (2026-09-17 결정, LC-3344).

오공고로 보내는 칸은 코드와 오공고 서버가 함께 정한다. 그 밖에 운영자가 공고에서 더 보고 싶은 칸
(재택 여부, 근무 시간 같은 것)을 배포 없이 더하는 자리가 이것이다. 값은 크롤러 안에만 남고 오공고로
보내지 않는다 — 오공고에 받을 칸이 없다.

## AI 가 본문에서 찾는다

항목마다 이름·설명(AI 에게 주는 지시)·값 형식을 적는다. 분류가 끝난 공고마다 켜진 항목을 한 번에
묻는 호출이 하나 더 나간다 (`fill_job`). 항목이 하나도 없으면 호출도 없다. 분류와 같은 제공자·모델을
쓰고 호출 기록도 분류(`classify`)로 남는다 — 비용 화면에서 공고 분류에 합쳐 보인다.

분류 본 호출에 섞지 않은 이유는 그 호출이 공고 나누기·줄 번호·근거 검사까지 얽혀 있어서다. 운영자가
더한 칸 때문에 오공고로 가는 칸의 정확도가 흔들리면 안 된다.

값을 본문에서 확인하지는 않는다(근거 검사 없음). 대신 형식이 `보기 중 하나` 면 목록 밖 값을 버리고,
`숫자`·`날짜` 면 모양이 맞지 않는 값을 버린다.

사이트 페이지에서 셀렉터로 읽는 항목은 아직 없다. 본문이 이미 상세 페이지의 글이라 AI 가 찾는 것으로
대부분 채워진다.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import Field, create_model

from app.config import Settings
from app.llm import settings as llm_settings
from app.llm.base import LlmCallError
from app.llm.log import CLASSIFY, record_call
from app.llm.providers import for_feature

logger = logging.getLogger(__name__)

TYPE_TEXT = "text"
TYPE_LONG = "long"
TYPE_DATE = "date"
TYPE_NUMBER = "number"
TYPE_CHOICE = "choice"
TYPES: dict[str, str] = {
    TYPE_TEXT: "짧은 글",
    TYPE_LONG: "긴 글",
    TYPE_DATE: "날짜",
    TYPE_NUMBER: "숫자",
    TYPE_CHOICE: "보기 중 하나",
}

# 한 번에 보내는 본문 상한. 분류 본 호출보다 짧게 둔다 — 운영자가 더한 칸은 보통 앞쪽 요약에 있다
MAX_BODY_CHARS = 12_000
MAX_LABEL = 40
MAX_INSTRUCTION = 400

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")

_SYSTEM = (
    "너는 채용공고 본문에서 운영자가 정한 항목의 값을 찾는다."
    " 본문에 근거가 있는 값만 적고, 없으면 빈 문자열로 둔다. 추측하지 않는다."
)


class CustomFieldError(ValueError):
    """항목 정의가 잘못됐다. 화면에 그대로 보인다."""


@dataclass(frozen=True)
class CustomField:
    id: int
    label: str
    instruction: str
    value_type: str
    choices: tuple[str, ...]
    enabled: bool

    @property
    def key(self) -> str:
        return f"field_{self.id}"

    @property
    def type_label(self) -> str:
        return TYPES.get(self.value_type, self.value_type)


def _row(row: sqlite3.Row) -> CustomField:
    return CustomField(
        id=int(row["id"]),
        label=str(row["label"]),
        instruction=str(row["instruction"]),
        value_type=str(row["value_type"]),
        choices=tuple(line for line in str(row["choices"]).splitlines() if line.strip()),
        enabled=bool(row["enabled"]),
    )


def list_fields(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[CustomField]:
    where = " WHERE enabled = 1" if enabled_only else ""
    return [
        _row(row)
        for row in conn.execute(f"SELECT * FROM custom_fields{where} ORDER BY id").fetchall()
    ]


def read_field(conn: sqlite3.Connection, field_id: int) -> CustomField | None:
    row = conn.execute("SELECT * FROM custom_fields WHERE id = ?", (field_id,)).fetchone()
    return _row(row) if row is not None else None


def validate(
    label: str, instruction: str, value_type: str, choices: str
) -> tuple[str, str, str, str]:
    """화면에서 온 값을 다듬고 거절할 것은 사유와 함께 거절한다."""
    label, instruction = label.strip(), instruction.strip()
    if not label:
        raise CustomFieldError("항목 이름이 비었다")
    if len(label) > MAX_LABEL:
        raise CustomFieldError(f"항목 이름은 {MAX_LABEL}자까지다")
    if len(instruction) > MAX_INSTRUCTION:
        raise CustomFieldError(f"AI 에게 줄 설명은 {MAX_INSTRUCTION}자까지다")
    if value_type not in TYPES:
        raise CustomFieldError(f"모르는 값 형식이다: {value_type}")
    lines = [line.strip() for line in choices.splitlines() if line.strip()]
    if value_type == TYPE_CHOICE and len(lines) < 2:
        raise CustomFieldError("보기 중 하나 형식은 보기를 줄마다 하나씩 두 개 이상 적는다")
    return label, instruction, value_type, "\n".join(lines) if value_type == TYPE_CHOICE else ""


def add_field(
    conn: sqlite3.Connection, label: str, instruction: str, value_type: str, choices: str = ""
) -> CustomField:
    label, instruction, value_type, choices = validate(label, instruction, value_type, choices)
    try:
        cursor = conn.execute(
            "INSERT INTO custom_fields (label, instruction, value_type, choices)"
            " VALUES (?, ?, ?, ?)",
            (label, instruction, value_type, choices),
        )
    except sqlite3.IntegrityError as exc:
        raise CustomFieldError(f"같은 이름의 항목이 이미 있다: {label}") from exc
    found = read_field(conn, int(cursor.lastrowid or 0))
    assert found is not None
    return found


def set_enabled(conn: sqlite3.Connection, field_id: int, enabled: bool) -> None:
    conn.execute("UPDATE custom_fields SET enabled = ? WHERE id = ?", (int(enabled), field_id))


def delete_field(conn: sqlite3.Connection, field_id: int) -> None:
    """항목과 그 값을 지운다. 되돌릴 수 없다."""
    conn.execute("DELETE FROM job_custom_values WHERE field_id = ?", (field_id,))
    conn.execute("DELETE FROM custom_fields WHERE id = ?", (field_id,))


def values_for(
    conn: sqlite3.Connection, raw_job_id: int, part: int
) -> list[tuple[CustomField, str]]:
    """공고 하나의 추가 항목 값. 켜진 항목 전부를 빈 값까지 돌려준다."""
    stored = {
        int(row["field_id"]): str(row["value"])
        for row in conn.execute(
            "SELECT field_id, value FROM job_custom_values WHERE raw_job_id = ? AND part = ?",
            (raw_job_id, part),
        )
    }
    return [(field, stored.get(field.id, "")) for field in list_fields(conn, enabled_only=True)]


def fill_counts(conn: sqlite3.Connection) -> tuple[dict[int, int], int]:
    """항목마다 값이 찬 공고 수와, 전체 공고 수."""
    total = int(conn.execute("SELECT count(*) AS n FROM normalized_jobs").fetchone()["n"])
    filled = {
        int(row["field_id"]): int(row["n"])
        for row in conn.execute(
            "SELECT field_id, count(*) AS n FROM job_custom_values WHERE trim(value) <> ''"
            " GROUP BY field_id"
        )
    }
    return filled, total


def _clean(field: CustomField, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if field.value_type == TYPE_CHOICE and text not in field.choices:
        return ""
    if field.value_type == TYPE_NUMBER and not _NUMBER.match(text.replace(",", "")):
        return ""
    if field.value_type == TYPE_NUMBER:
        return text.replace(",", "")
    if field.value_type == TYPE_DATE and not _DATE.match(text):
        return ""
    return text


def _prompt(fields: Sequence[CustomField], title: str, body: str) -> str:
    lines = ["아래 채용공고에서 항목마다 값을 찾아 적는다.", "", "## 항목"]
    for field in fields:
        line = f"- {field.key}: {field.label} ({field.type_label})"
        if field.instruction:
            line += f" — {field.instruction}"
        if field.value_type == TYPE_CHOICE:
            line += f" / 보기: {', '.join(field.choices)}"
        if field.value_type == TYPE_DATE:
            line += " / YYYY-MM-DD 로 적는다"
        lines.append(line)
    lines += ["", "## 공고 제목", title, "", "## 공고 본문", body[:MAX_BODY_CHARS]]
    return "\n".join(lines)


def _schema(fields: Sequence[CustomField]) -> Any:
    definitions: dict[str, Any] = {
        field.key: (str, Field(default="", description=field.label)) for field in fields
    }
    return create_model("CustomFieldValues", **definitions)


async def extract(
    conn: sqlite3.Connection,
    fields: Sequence[CustomField],
    title: str,
    body: str,
    *,
    settings: Settings | None = None,
    client: Any | None = None,
) -> dict[int, str]:
    """켜진 항목을 한 번에 묻는다. 항목 id → 다듬은 값. 호출은 분류로 기록한다."""
    if not fields or not body.strip():
        return {}
    resolved = llm_settings.settings_for(conn, CLASSIFY, settings)
    provider, model = for_feature(CLASSIFY, resolved)
    used_client = client or provider.build_client(resolved)
    try:
        text, usage = await provider.call_model(
            used_client,
            model,
            _prompt(fields, title, body),
            1,
            "추가 항목",
            response_schema=_schema(fields),
            system_instruction=_SYSTEM,
        )
    except LlmCallError:
        raise
    record_call(conn, feature=CLASSIFY, usage=usage)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LlmCallError("unparsable", f"추가 항목 응답이 JSON 이 아니다: {exc}") from exc
    if not isinstance(data, dict):
        raise LlmCallError("unparsable", "추가 항목 응답이 객체가 아니다")
    return {field.id: _clean(field, data.get(field.key)) for field in fields}


def save_values(
    conn: sqlite3.Connection, raw_job_id: int, part: int, values: dict[int, str]
) -> None:
    for field_id, value in values.items():
        conn.execute(
            """
            INSERT INTO job_custom_values (raw_job_id, part, field_id, value)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (raw_job_id, part, field_id)
            DO UPDATE SET value = excluded.value, updated_at = datetime('now')
            """,
            (raw_job_id, part, field_id, value),
        )


def _job_text(conn: sqlite3.Connection, raw_job_id: int, part: int) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT title, body FROM normalized_jobs WHERE raw_job_id = ? AND part = ?",
        (raw_job_id, part),
    ).fetchone()
    if row is None:
        return None
    return str(row["title"] or ""), str(row["body"] or "")


async def fill_job(
    conn: sqlite3.Connection,
    raw_job_id: int,
    *,
    fields: Sequence[CustomField] | None = None,
    settings: Settings | None = None,
    client: Any | None = None,
) -> int:
    """공고 하나(나눈 공고면 번호마다)의 추가 항목을 채운다. 채운 공고 수를 돌려준다.

    실패해도 예외를 올리지 않는다. 분류는 이미 끝났고, 추가 항목 때문에 분류가 실패로 보이면
    안 된다.
    """
    chosen = list(fields) if fields is not None else list_fields(conn, enabled_only=True)
    if not chosen:
        return 0
    filled = 0
    parts = [
        int(row["part"])
        for row in conn.execute(
            "SELECT part FROM normalized_jobs WHERE raw_job_id = ? ORDER BY part", (raw_job_id,)
        )
    ]
    for part in parts:
        text = _job_text(conn, raw_job_id, part)
        if text is None:
            continue
        try:
            values = await extract(conn, chosen, *text, settings=settings, client=client)
        except LlmCallError as exc:
            logger.warning("추가 항목을 채우지 못했다 raw_jobs %s: %s", raw_job_id, exc)
            continue
        save_values(conn, raw_job_id, part, values)
        filled += 1
    return filled
