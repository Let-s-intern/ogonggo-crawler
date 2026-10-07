"""공고 분석 방법 — 화면에서 고치는 시스템 지시, 항목별 지침, 참고 파일과 그 판.

## 화면에서 고치는 것과 코드에 남는 것

분석 방법 화면(`/job-analysis`)에서 고치는 것은 셋이다 — 모델에게 주는 시스템 지시(역할·말투·원칙),
항목마다 무엇을 어떻게 쓰는지, 그리고 참고 파일(`.md`·`.txt`). 항목의 이름과 개수, '원문에 없으면
비운다', '공고 속 문구는 원문 그대로' 같은 골격은 오공고 서버·프런트와의 약속이라 코드에 남는다
(`app/job_analysis/analyzer.py`, `schema.py`).

## 판

`app/classify/prompt_rules.py` 의 AI 분류 규칙과 같은 방식이다. 저장할 때마다 새 판이 쌓이고
(`job_analysis_guide_versions`), 가장 최근 판이 지금 방법이다. 되돌리기는 옛 판을 복사해 새 판으로
저장한다. 저장한 판이 없으면 이 파일의 기본 방법을 판 0 으로 쓴다.

참고 파일은 판 안에 내용째 들어간다. 파일을 따로 두고 판이 이름만 가리키면, 파일을 지우거나 바꾼 뒤
옛 판으로 되돌렸을 때 그 판으로 만든 분석과 같은 프롬프트를 다시 만들 수 없다.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from typing import Any

# 항목 키, 화면 이름, 답의 어느 칸을 쓰는지. 순서가 화면과 프롬프트의 순서다
SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("tasks", "실제 하는 일", "tasks"),
    ("conditions", "지원 조건", "required, preferred"),
    ("employment", "고용 형태", "employment"),
    ("submission", "제출물과 전형", "submission"),
    ("competencies", "연결하기 좋은 경험", "competencies"),
)
SECTION_LABELS: dict[str, str] = {key: label for key, label, _ in SECTIONS}

# 적을 수 있는 길이. 방법은 공고마다 프롬프트에 실려 토큰이 된다
MAX_SYSTEM_CHARS = 4000
MAX_SECTION_CHARS = 2000
MAX_FILES = 5
MAX_FILE_CHARS = 20000
MAX_FILES_TOTAL_CHARS = 40000
MAX_NOTE_CHARS = 200
FILE_SUFFIXES: tuple[str, ...] = (".md", ".txt")


@dataclass(frozen=True)
class GuideFile:
    """참고 파일 하나. 올린 이름과 글이다."""

    name: str
    content: str

    @property
    def chars(self) -> int:
        return len(self.content)


@dataclass(frozen=True)
class Guide:
    """분석 방법 한 벌. `sections` 는 `SECTIONS` 의 항목마다 하나다."""

    system: str
    sections: dict[str, str]
    files: tuple[GuideFile, ...] = ()

    @property
    def file_chars(self) -> int:
        return sum(item.chars for item in self.files)


@dataclass(frozen=True)
class GuideVersion:
    """저장된 판 하나. 판 0 은 저장된 것이 아니라 코드의 기본 방법이다."""

    number: int
    guide: Guide
    note: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class VersionEntry:
    """판 목록의 한 줄. 본문은 읽지 않는다 — 깨진 옛 판이 목록까지 막으면 안 된다."""

    number: int
    note: str
    created_at: str


class GuideError(ValueError):
    """저장하거나 읽을 수 없는 방법. `reason` 을 화면이 그대로 적는다."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


DEFAULT_GUIDE = Guide(
    system=(
        "너는 렛츠커리어 '오늘의 공고' 의 공고 분석가다.\n"
        "인턴·신입 취준생이 공고 원문을 다 읽지 않아도 무슨 일을 하고, 누구를 뽑고,\n"
        "무엇을 내야 하는지 한눈에 알 수 있게 정리한다.\n"
        "\n"
        "- 친근한 해요체로 쓴다. 문장은 짧게, 한 항목에 한 가지 내용만 담는다.\n"
        "- 공고의 표현을 살리되 취준생이 모를 사내 용어는 쉬운 말로 풀어 쓴다.\n"
        "- 원문에 없는 사실은 쓰지 않는다. 짐작이 필요하면 '~로 보여요' 처럼 짐작임을 드러낸다."
    ),
    sections={
        "tasks": (
            "- 입사하면 실제로 하는 일을 3개까지 고른다.\n"
            "- tag 는 일의 성격을 2~4글자로 쓴다. 예: 기획, 제품화, 협업, 운영, 분석\n"
            "- text 는 공고 문장을 한 문장으로 다듬어 쓴다.\n"
            "- 회사 자랑이나 비전은 넣지 않는다."
        ),
        "conditions": (
            "- required 는 자격 요건(필수), preferred 는 우대 사항이다.\n"
            "- 공고의 항목 하나를 한 줄로 옮긴다.\n"
            "- 지원자가 스스로 체크할 수 있게 한 줄에 조건 하나만 담는다.\n"
            "- 공고가 'Problem Solver:' 처럼 이름을 붙였으면 '이름 — 내용' 으로 쓴다.\n"
            "- 공고에 없으면 빈 목록이다."
        ),
        "employment": (
            "- type: 고용 형태와 기간. 예: 전환형 인턴십 3개월\n"
            "- conversion: 정규직 전환 조건\n"
            "- salary: 급여\n"
            "- affiliation: 소속 조직·직군\n"
            "- note 에는 지원자가 알아 두면 좋은 보충을 짧게 쓴다.\n"
            "  예: 전환 인원·비율은 공고에 없음, 면접에서 확인할 항목"
        ),
        "submission": (
            "- documents: 제출 서류와 형식. 필수·선택을 구분한다.\n"
            "- essay: 자기소개서 문항. 없으면 '별도 문항 없음'\n"
            "- process: 전형 단계를 → 로 이어 쓴다.\n"
            "- deadline: 마감 일시. 날짜가 없고 채용 시 마감이면 value 는 비우고\n"
            "  note 에 '상시 채용으로 보이며, 조기 마감될 수 있어요' 처럼 쓴다."
        ),
        "competencies": (
            "- 이 공고가 가장 중요하게 보는 역량 3가지를 고른다.\n"
            "- name: 짧은 역량 이름\n"
            "- quote: 그 역량을 요구하는 공고 문장\n"
            "- description: 그 역량이 무엇이고 전형 어디에서 확인할 가능성이 높은지 2문장\n"
            "- experiences: 취준생이 대학 생활·대외활동·인턴에서 꺼내 연결할 수 있는 경험 3가지.\n"
            "  '~한 경험' 으로 끝나게 쓴다."
        ),
    },
)


def current(conn: sqlite3.Connection) -> GuideVersion:
    """지금 쓰는 판. 저장한 판이 없으면 기본 방법(판 0)이다. 가장 최근 판이 깨졌으면 거절한다."""
    row = conn.execute(
        "SELECT id, guide_json, note, created_at FROM job_analysis_guide_versions"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return GuideVersion(0, DEFAULT_GUIDE, "기본 방법")
    return _version(row)


def read(conn: sqlite3.Connection, number: int) -> GuideVersion:
    """판 하나. 0 은 기본 방법이다."""
    if number == 0:
        return GuideVersion(0, DEFAULT_GUIDE, "기본 방법")
    row = conn.execute(
        "SELECT id, guide_json, note, created_at FROM job_analysis_guide_versions WHERE id = ?",
        (number,),
    ).fetchone()
    if row is None:
        raise GuideError("not_found", f"판 {number} 이 없다")
    return _version(row)


def history(conn: sqlite3.Connection, limit: int = 50) -> list[VersionEntry]:
    """저장한 판. 최근 것부터다."""
    rows = conn.execute(
        "SELECT id, note, created_at FROM job_analysis_guide_versions ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [VersionEntry(int(row["id"]), str(row["note"]), str(row["created_at"])) for row in rows]


def save(conn: sqlite3.Connection, guide: Guide, note: str = "") -> GuideVersion:
    """새 판으로 저장한다. 지금 판과 같으면 판을 만들지 않고 거절한다."""
    validate(guide)
    note = note.strip()
    if len(note) > MAX_NOTE_CHARS:
        raise GuideError("too_long", f"바꾼 이유가 {len(note)}자다. {MAX_NOTE_CHARS}자까지 적는다")
    text = to_json(guide)
    try:
        latest: GuideVersion | None = current(conn)
    except GuideError:
        # 깨진 판 위에는 무엇이든 새로 저장할 수 있어야 한다. 그것이 깨진 판을 대신하는 길이다
        latest = None
    if latest is not None and to_json(latest.guide) == text:
        raise GuideError("unchanged", f"판 {latest.number} 과 방법이 같아 새 판을 만들지 않았다")
    cursor = conn.execute(
        "INSERT INTO job_analysis_guide_versions (guide_json, note) VALUES (?, ?)", (text, note)
    )
    return read(conn, int(cursor.lastrowid or 0))


def restore(conn: sqlite3.Connection, number: int) -> GuideVersion:
    """옛 판을 복사해 새 판으로 저장한다. 옛 판은 그대로 남는다."""
    return save(conn, read(conn, number).guide, f"판 {number} 으로 되돌림")


def validate(guide: Guide) -> None:
    """프롬프트를 넘치게 하거나 뜻 없이 갈 방법을 거절한다."""
    if not guide.system.strip():
        raise GuideError("empty_system", "시스템 지시가 비었다")
    if len(guide.system) > MAX_SYSTEM_CHARS:
        raise GuideError(
            "too_long", f"시스템 지시가 {len(guide.system)}자다. {MAX_SYSTEM_CHARS}자까지 적는다"
        )
    for key, label, _ in SECTIONS:
        text = guide.sections.get(key, "")
        if len(text) > MAX_SECTION_CHARS:
            raise GuideError(
                "too_long", f"{label} 지침이 {len(text)}자다. {MAX_SECTION_CHARS}자까지 적는다"
            )
    if len(guide.files) > MAX_FILES:
        raise GuideError("too_many", f"참고 파일이 {len(guide.files)}개다. {MAX_FILES}개까지 둔다")
    names = [item.name for item in guide.files]
    if len(set(names)) != len(names):
        raise GuideError("duplicate_file", "같은 이름의 참고 파일이 둘 있다")
    for item in guide.files:
        check_file(item)
    if guide.file_chars > MAX_FILES_TOTAL_CHARS:
        raise GuideError(
            "too_long",
            f"참고 파일이 모두 {guide.file_chars:,}자다. 합쳐서 {MAX_FILES_TOTAL_CHARS:,}자까지 "
            f"둔다",
        )


def check_file(item: GuideFile) -> None:
    if not item.name.lower().endswith(FILE_SUFFIXES):
        raise GuideError("file_type", f"{item.name} — .md, .txt 파일만 올린다")
    if not item.content.strip():
        raise GuideError("empty_file", f"{item.name} — 내용이 비었다")
    if item.chars > MAX_FILE_CHARS:
        raise GuideError(
            "too_long", f"{item.name} 이 {item.chars:,}자다. 파일 하나는 {MAX_FILE_CHARS:,}자까지다"
        )


def decode_file(name: str, data: bytes) -> GuideFile:
    """올린 파일을 글로 읽는다. UTF-8 이 아니면 거절한다 — 깨진 글자가 프롬프트에 실린다."""
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise GuideError("file_encoding", f"{name} — UTF-8 글 파일이 아니다") from exc
    item = GuideFile(name.strip(), content.replace("\r\n", "\n").strip())
    check_file(item)
    return item


def with_files(guide: Guide, files: tuple[GuideFile, ...]) -> Guide:
    return replace(guide, files=files)


def to_json(guide: Guide) -> str:
    """저장하는 모양. 항목 순서는 `SECTIONS` 그대로라 같은 방법이면 글자도 같다."""
    return json.dumps(
        {
            "system": guide.system,
            "sections": {key: guide.sections.get(key, "") for key, _, _ in SECTIONS},
            "files": [{"name": item.name, "content": item.content} for item in guide.files],
        },
        ensure_ascii=False,
    )


def from_json(text: str, number: int = 0) -> Guide:
    """저장된 모양에서 읽는다. 판에 없는 항목은 기본 방법으로 채운다."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GuideError("broken_version", f"판 {number} 을 읽지 못했다: {exc}") from exc
    if not isinstance(data, dict):
        raise GuideError("broken_version", f"판 {number} 이 방법 모양이 아니다")
    stored = data.get("sections")
    sections_in: dict[str, Any] = stored if isinstance(stored, dict) else {}
    sections = {
        key: str(sections_in[key]) if key in sections_in else DEFAULT_GUIDE.sections[key]
        for key, _, _ in SECTIONS
    }
    files = tuple(
        GuideFile(str(item.get("name", "")), str(item.get("content", "")))
        for item in data.get("files") or []
        if isinstance(item, dict)
    )
    return Guide(str(data.get("system", "")), sections, files)


def _version(row: sqlite3.Row) -> GuideVersion:
    number = int(row["id"])
    return GuideVersion(
        number, from_json(str(row["guide_json"]), number), str(row["note"]), str(row["created_at"])
    )
