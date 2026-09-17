"""공고 한 건의 상세 — 칸 목록, 값이 어디서 왔는지, 수집·분류·전송 요약.

공고 목록의 오른쪽 패널(`app/api/review.py`)이 보기와 고치기에 같이 쓴다. 칸을 두 곳에 적으면
보기에는 있는데 고치기에는 없는 칸이 생긴다.

## 값의 출처

패널은 값마다 어디서 왔는지 적는다 (2026-09-17 결정, LC-3344). 정규화 순서가 규칙 → 분류 →
사람 보정이라(`app/normalize/engine.py`) 출처도 그 순서의 마지막 손이다.

| 출처 | 뜻 |
|---|---|
| 직접 수정 | `job_field_overrides` 에 그 칸이 있다. 다시 분류해도 이 값이 남는다 |
| AI | 분류가 채우는 칸이고 그 공고가 분류됐다 |
| 사이트 | 수집이 페이지에서 읽은 칸이다 |
| 자동 | 다른 값에서 정규화가 정한 칸이다 (모집 유형·자동 종료·대표 이미지·모회사) |
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from app.classify.schema import STORED_CLASSIFY_FIELDS, VALUE_LABELS
from app.normalize.engine import OVERRIDABLE_FIELDS
from app.normalize.rules import RULE_FIELDS

SOURCE_EDIT = "edit"
SOURCE_AI = "ai"
SOURCE_SITE = "site"
SOURCE_AUTO = "auto"

SOURCE_LABELS: dict[str, str] = {
    SOURCE_EDIT: "직접 수정",
    SOURCE_AI: "AI",
    SOURCE_SITE: "사이트",
    SOURCE_AUTO: "자동",
}

# 칸의 모양. 짧은 글·긴 글·보기 중 하나·날짜
KIND_TEXT = "text"
KIND_LONG = "long"
KIND_CHOICE = "choice"
KIND_DATE = "date"


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kind: str = KIND_TEXT

    @property
    def editable(self) -> bool:
        return self.name in OVERRIDABLE_FIELDS

    @property
    def choices(self) -> dict[str, str]:
        return VALUE_LABELS.get(self.name, {})


# 묶음과 칸. 오공고로 보내는 칸이 전부 들어 있고, 순서가 패널의 순서다
SECTIONS: tuple[tuple[str, tuple[Field, ...]], ...] = (
    (
        "기본 정보",
        (
            Field("company_name", "회사"),
            Field("parent_company_name", "모회사"),
            Field("title", "제목"),
            Field("industry", "산업"),
            Field("job_field", "직군"),
            Field("job_role", "직무"),
            Field("cover_image_url", "대표 이미지"),
        ),
    ),
    (
        "모집 조건",
        (
            Field("employment_type", "고용 형태", KIND_CHOICE),
            Field("experience_type", "경력", KIND_CHOICE),
            Field("experience_min_years", "최소 경력 연수"),
            Field("education_level", "학력", KIND_CHOICE),
            Field("region", "근무 지역"),
            Field("recruitment_type", "모집 유형", KIND_CHOICE),
            Field("recruitment_headcount", "모집 인원"),
            Field("application_method", "지원 방법", KIND_CHOICE),
            Field("recruitment_start_at", "모집 시작", KIND_DATE),
            Field("recruitment_end_at", "모집 마감", KIND_DATE),
            Field("closes_when_filled", "채용 시 마감", KIND_CHOICE),
            Field("auto_close_enabled", "자동 종료", KIND_CHOICE),
        ),
    ),
    (
        "공고 내용",
        (
            Field("company_and_team_introduction", "회사·팀 소개", KIND_LONG),
            Field("responsibilities", "주요 업무", KIND_LONG),
            Field("qualifications", "자격 요건", KIND_LONG),
            Field("preferred_qualifications", "우대 사항", KIND_LONG),
            Field("compensation", "급여·처우", KIND_LONG),
            Field("benefits", "복지·혜택", KIND_LONG),
            Field("hiring_process", "채용 절차", KIND_LONG),
            Field("recruitment_notice", "채용 안내사항", KIND_LONG),
        ),
    ),
)

FIELDS: tuple[Field, ...] = tuple(field for _, fields in SECTIONS for field in fields)
EDITABLE_FIELDS: tuple[Field, ...] = tuple(field for field in FIELDS if field.editable)

_CLASSIFIED = set(STORED_CLASSIFY_FIELDS)
_COLLECTED = set(RULE_FIELDS)


def overrides(conn: sqlite3.Connection, raw_job_id: int, part: int) -> dict[str, str]:
    """그 공고에 사람이 고쳐 둔 칸과 값."""
    return {
        str(row["field_name"]): str(row["value"])
        for row in conn.execute(
            "SELECT field_name, value FROM job_field_overrides WHERE raw_job_id = ? AND part = ?",
            (raw_job_id, part),
        )
    }


def source_of(name: str, edited: dict[str, str], classified: bool) -> str:
    """그 칸의 값이 어디서 왔는지. 표는 모듈 설명에 있다."""
    if name in edited:
        return SOURCE_EDIT
    if name in _CLASSIFIED and classified:
        return SOURCE_AI
    if name in _COLLECTED:
        return SOURCE_SITE
    return SOURCE_AUTO


def display(field: Field, value: Any) -> str:
    """화면에 적을 글자. 보기 중 하나인 칸은 화면 이름으로, 날짜는 분까지."""
    if value is None or str(value).strip() == "":
        return ""
    text = str(value)
    if field.kind == KIND_CHOICE:
        return field.choices.get(text, text)
    if field.kind == KIND_DATE:
        return text[:16]
    if field.name == "experience_min_years":
        return f"{text}년 이상"
    if field.name == "recruitment_headcount":
        return f"{text}명"
    return text


@dataclass(frozen=True)
class Classified:
    model: str
    classified_at: str
    filled: int
    total: int


def classification(conn: sqlite3.Connection, job: sqlite3.Row) -> Classified | None:
    """그 공고의 분류 요약. 분류가 채우는 칸 중 몇 칸이 찼는지 센다."""
    row = conn.execute(
        "SELECT model, classified_at FROM job_classifications WHERE raw_job_id = ? AND part = ?",
        (job["raw_job_id"], job["part"]),
    ).fetchone()
    if row is None:
        return None
    names = [field.name for field in FIELDS if field.name in _CLASSIFIED]
    filled = sum(1 for name in names if str(job[name] or "").strip())
    return Classified(str(row["model"]), str(row["classified_at"]), filled, len(names))


@dataclass(frozen=True)
class Delivery:
    status: str
    attempts: int
    last_error: str
    spring_job_id: int | None
    sent_at: str
    updated_at: str


def delivery(conn: sqlite3.Connection, source_url: str) -> Delivery | None:
    row = conn.execute(
        "SELECT status, attempts, last_error, spring_job_id, sent_at, updated_at"
        " FROM spring_deliveries WHERE source_url = ?",
        (source_url,),
    ).fetchone()
    if row is None:
        return None
    return Delivery(
        str(row["status"]),
        int(row["attempts"] or 0),
        str(row["last_error"] or ""),
        int(row["spring_job_id"]) if row["spring_job_id"] is not None else None,
        str(row["sent_at"] or ""),
        str(row["updated_at"] or ""),
    )


def parts_of(conn: sqlite3.Connection, raw_job_id: int) -> int:
    """한 수집 건이 몇 공고로 나뉘었는지."""
    row = conn.execute(
        "SELECT count(*) AS n FROM normalized_jobs WHERE raw_job_id = ?", (raw_job_id,)
    ).fetchone()
    return int(row["n"])
