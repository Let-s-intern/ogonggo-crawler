"""분류 AI 에게 주는 규칙 (2026-09-14 결정).

## 화면에서 고치는 것과 코드에 남는 것

AI 규칙 화면(`/prompt-rules`)에서 고치는 것은 둘이다 — 칸마다 무엇을 넣고 어떻게 고르는지(규칙 글과
'원문 → 넣을 값' 예시), 그리고 모든 칸에 걸리는 공통 규칙. 응답 모양(조각 `{line, text}`), 직무
나누기(`postings`/`common`), 줄 번호, 근거 문장 규칙 같은 골격은 `app/classify/classifier.py` 에
남는다. 골격이 화면에서 바뀌면 응답이 스키마와 어긋나 분류가 전부 실패하고, 그 실패는 다음 배치를
돌려야 드러난다.

## 판

저장할 때마다 새 판이 쌓인다(`classify_rule_versions`). 가장 최근 판이 지금 규칙이다. 되돌리기는 옛
판을 복사해 새 판으로 저장한다 — 이력을 지우거나 고치지 않는다. 저장한 판이 하나도 없으면 이 파일의
기본 규칙을 판 0 으로 쓴다.

분류 결과(`job_classifications.rules_version`)와 모델 호출 기록(`llm_calls.rules_version`)에 그 판
번호가 남는다. 나중에 "왜 이렇게 분류됐나" 와 "규칙을 바꾼 뒤 무엇이 달라졌나" 에 답하는 자리다.

## 저장된 판에 없는 칸은 기본 규칙으로 채운다

분류가 새 칸을 뽑기 시작하면 그 전에 저장한 판에는 그 칸의 규칙이 없다. 비워 두면 모델이 그 칸을
무엇으로 채울지 모르므로 기본 규칙으로 채운다. 판에 남아 있지만 이제는 없는 칸의 규칙은 버린다.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.classify.schema import (
    EXTRACT_FIELDS,
    INDUSTRY,
    JUDGE_FIELDS,
    NUMBER_FIELDS,
    POSTING_TITLE,
    REGION,
    TAXONOMY_FIELDS,
)

# 칸의 종류. 화면이 카드에 적고, 프롬프트는 종류마다 다른 구역에 넣는다
EXTRACT = "뽑는 칸"
JUDGE = "판정 칸"
TAXONOMY = "직무 분류"
INDUSTRIES = "산업 분류"
TITLE = "공고 제목"

# 칸 이름, 화면 이름, 종류. 순서가 화면과 프롬프트의 순서다
RULE_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("posting_title", "공고 제목 (오공고에 올라가는 제목)", TITLE),
    ("position_name", "직무 이름 (나눈 공고의 제목에 붙는다)", EXTRACT),
    ("responsibilities", "주요 업무", EXTRACT),
    ("qualifications", "자격 요건", EXTRACT),
    ("preferred_qualifications", "우대 사항", EXTRACT),
    ("hiring_process", "채용 절차", EXTRACT),
    ("company_and_team_introduction", "회사·팀 소개", EXTRACT),
    ("compensation", "급여·처우", EXTRACT),
    ("benefits", "복지·혜택", EXTRACT),
    ("recruitment_headcount", "모집 인원", EXTRACT),
    ("recruitment_notice", "채용 안내사항", EXTRACT),
    ("employment_type", "고용 형태", JUDGE),
    ("experience_type", "경력 구분", JUDGE),
    ("experience_min_years", "최소 경력 연수", JUDGE),
    ("education_level", "요구 학력", JUDGE),
    ("closes_when_filled", "채용 시 마감", JUDGE),
    ("application_method", "지원 방법", JUDGE),
    # 2026-09-21 부터 옮기는 칸이 아니라 큰 지역 목록에서 고르는 칸이다 (`app/regions.py`)
    ("region", "근무 지역", JUDGE),
    ("job_field", "직군", TAXONOMY),
    ("job_role", "직무", TAXONOMY),
    ("industry", "산업", INDUSTRIES),
)
FIELD_LABELS: dict[str, str] = {name: label for name, label, _ in RULE_FIELDS}


def names_of(kind: str) -> tuple[str, ...]:
    """그 종류의 칸 이름. 순서는 `RULE_FIELDS` 그대로다."""
    return tuple(name for name, _, group in RULE_FIELDS if group == kind)


# 분류가 채우는 칸마다 규칙이 있어야 한다. 칸이 늘었는데 여기 없으면 모델이 그 칸을 모른다
assert set(names_of(EXTRACT)) == set(EXTRACT_FIELDS)
assert set(names_of(JUDGE)) == {*JUDGE_FIELDS, *NUMBER_FIELDS, REGION}
assert set(names_of(TAXONOMY)) == set(TAXONOMY_FIELDS)
assert names_of(INDUSTRIES) == (INDUSTRY,)
assert names_of(TITLE) == (POSTING_TITLE,)

# 적을 수 있는 길이. 규칙은 공고마다 프롬프트에 실려 토큰이 되고, 긴 프롬프트는 모델이 뒤를 흘린다
MAX_COMMON_CHARS = 4000
MAX_RULE_CHARS = 2000
MAX_EXAMPLES = 20
MAX_EXAMPLE_CHARS = 300
MAX_NOTE_CHARS = 200


@dataclass(frozen=True)
class Example:
    """원문 문장과 그때 넣을 값 한 쌍."""

    source: str
    value: str


@dataclass(frozen=True)
class FieldRule:
    """칸 하나의 규칙 글과 예시."""

    rule: str = ""
    examples: tuple[Example, ...] = ()


@dataclass(frozen=True)
class RuleSet:
    """규칙 한 벌. `fields` 는 `RULE_FIELDS` 의 칸마다 하나다."""

    common: str
    fields: dict[str, FieldRule]


@dataclass(frozen=True)
class RuleVersion:
    """저장된 판 하나. 판 0 은 저장된 것이 아니라 코드의 기본 규칙이다."""

    number: int
    rules: RuleSet
    note: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class VersionEntry:
    """판 목록의 한 줄. 규칙 본문은 읽지 않는다 — 깨진 옛 판이 목록까지 막으면 안 된다."""

    number: int
    note: str
    created_at: str


class RuleSetError(ValueError):
    """저장하거나 읽을 수 없는 규칙. `reason` 을 화면이 그대로 적는다."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


DEFAULT_RULES = RuleSet(
    common=(
        "- 공고의 소제목이 칸 이름과 달라도 된다. `지원자격`·`필수요건`·`이런 분을 찾아요` 아래 "
        "내용은 qualifications 다. 소제목 자체는 조각에 넣지 않는다.\n"
        "- 어느 칸에도 맞지 않는 내용만 recruitment_notice 에 모은다. 본문 전체를 "
        "recruitment_notice 에 넣지 않는다.\n"
        '- **슬로건·화면 UI 문구는 어느 칸에도 옮기지 않는다.** "간편하면서도 안전한 금융을 '
        '만든다" 같은 한 줄 슬로건, "N개 계열사·N개의 포지션이 열려 있어요"·"1개 포지션" 같은 '
        "화면 카운트 문구는 이 공고 하나만 말하는 정보가 아니다. 회사·팀 소개는 소제목 구역이 "
        "있을 때 company_and_team_introduction 에만 담고, 다른 칸에는 옮기지 않는다. 공고 자체에 "
        "대한 안내만 recruitment_notice 에 담는다."
    ),
    fields={
        "posting_title": FieldRule(
            "오공고에 올라갈 이 공고의 제목이다. 사이트 제목([0])을 바탕으로 짓고 **이 posting "
            "의 직무 이름이 반드시 들어가게 한다.** 사이트 제목에 직무 이름이 이미 있으면 거의 "
            "그대로 쓴다. 없으면(`신입사원 모집`, `경력사원 채용`) 본문에서 이 posting 이 뽑는 "
            "직무를 읽어 넣는다. 직무마다 나눈 공고는 그 직무 이름을 넣는다. 본문에도 직무 "
            "이름이 없으면 고른 job_role 을 넣는다. 사이트 제목에 있는 모집 구분"
            "(신입·경력·인턴)과 연도는 살리고, `[공고]`·`(~9/30)`·`D-12` 같은 게시판 표시는 "
            "뺀다. 한 줄, 60자 안이다.",
            (
                Example(
                    "2026년 하반기 CJ제일제당 신입사원 모집 / 본문 직무: 마케팅",
                    "2026년 하반기 CJ제일제당 마케팅 신입사원 모집",
                ),
                Example(
                    "경력사원 채용(M&A, 유선 고객상담, 콘텐츠 PD) / 이 posting: 콘텐츠 PD",
                    "콘텐츠 PD 경력사원 채용",
                ),
                Example(
                    "[정보보안센터] IT보안 담당자 경력채용", "[정보보안센터] IT보안 담당자 경력채용"
                ),
            ),
        ),
        "position_name": FieldRule(
            "직무. **직무가 하나인 공고는 제목([0])에서만 가져온다.** 그 공고가 어떤 일을 "
            "할 사람을 뽑는지 제목이 말하는 부분이다. 회사명·연도·`경력사원 채용`·`영입` 같은 "
            "말은 빼고 직무를 가리키는 부분만 남긴다. 제목이 직무를 말하지 않으면"
            "(`전 직군 채용`, `신입사원 채용`) 빈 목록으로 둔다. **직무마다 나눈 공고는 "
            "본문에서 그 직무의 이름이 적힌 줄에서 가져온다**(`[Finance]` 이면 `Finance`). "
            "직무가 사업부·조직 아래 나뉘어 있으면 그 조직 이름이 적힌 줄도 조각으로 함께 "
            "낸다 — 조직 이름 조각을 먼저, 직무 이름 조각을 다음에 "
            "(`orgName: HS사업본부` 와 `[기계]` 이면 `HS사업본부`, `기계`). 다른 조직에 "
            "같은 이름의 직무가 있어 조직 이름이 없으면 어느 공고인지 알 수 없다"
        ),
        "responsibilities": FieldRule("주요 업무·담당 업무"),
        "qualifications": FieldRule("자격요건·지원자격"),
        "preferred_qualifications": FieldRule("우대사항"),
        "hiring_process": FieldRule("전형 절차"),
        "region": FieldRule(
            "근무지. 이 직무를 실제로 일하는 곳이 속한 큰 지역을 목록에서 고른다. **여러 곳이면 "
            "모두 고른다.** 구·시·군이나 사업장 이름만 적혀 있으면 그곳이 속한 큰 지역을 고른다. "
            "나라 밖이면 `해외` 다. `전국 현장`·`전국 각지` 처럼 곳을 특정하지 않고 전국이라고 "
            "적혀 있으면 `전국` 하나만 고른다. 근무지가 원문에 없으면 빈 목록으로 둔다 — 이 "
            "칸만은 비워도 된다. 회사 주소나 본사 소개에만 나온 지역은 근무지가 아니다",
            (
                Example("근무지: 성남시 분당구(판교)", "경기"),
                Example("울산 본사 및 분당 GRC", "울산, 경기"),
                Example("근무지 : 부산 해운대구 센텀", "부산"),
                Example("베트남 하노이 법인", "해외"),
                Example("근무지 : 본사(양재동) / 전국 현장", "전국"),
            ),
        ),
        "company_and_team_introduction": FieldRule(
            "회사·팀 소개. **공고에 `회사 소개`·`팀 소개`·`회사 및 팀 소개` 같은 소제목으로 "
            "된 구역이 있을 때만** 그 구역의 내용을 가져온다. 그런 구역이 없으면 빈 목록으로 "
            "둔다. 다른 곳에 흩어진 회사 소개 문장은 모아 오지 않는다"
        ),
        "compensation": FieldRule("급여·처우·연봉"),
        "benefits": FieldRule("복지·혜택"),
        "recruitment_headcount": FieldRule(
            "모집 인원. 적힌 그대로 옮긴다(`0명`, `O명`, `00명` 도 그대로)"
        ),
        "recruitment_notice": FieldRule(
            "위 어디에도 맞지 않는, **이 공고만의** 안내(전형 유의사항, 제출 서류, "
            "보훈·장애인 우대 문구 등)"
        ),
        "employment_type": FieldRule(
            "고용 형태. 전환을 약속해도 지금 뽑는 형태를 고른다. 주 몇 일·몇 시간만 일하면 "
            "PART_TIME 이다.",
            (
                Example("채용 후 정규직 전환", "INTERN"),
                Example("주 3일 근무", "PART_TIME"),
            ),
        ),
        "experience_type": FieldRule(
            "경력 구분. 신입과 경력을 함께 받으면 BOTH, 경력을 따지지 않거나 경력을 말하지 "
            "않으면 IRRELEVANT 다.",
            (
                Example("5년 이상 경험", "EXPERIENCED"),
                Example("신입/경력", "BOTH"),
                Example("경력 무관", "IRRELEVANT"),
            ),
        ),
        "experience_min_years": FieldRule(
            "최소 경력 연수. experience_type 이 EXPERIENCED 이고 원문에 최소 연수가 적혀 있을 "
            "때만 숫자만 적는다. 그 밖에는 빈 문자열이다. 신입·인턴 공고의 0 은 저장할 때 "
            "정해지므로 적지 않는다.",
            (Example("관련 경력 3년 이상", "3"),),
        ),
        "education_level": FieldRule(
            "요구 학력. 지원 자격이 요구하는 **최소 학력**을 고른다. 우대사항에만 있는 "
            "학력(`석사 우대`)은 요구 조건이 아니라 고르지 않는다. 학력을 말하지 않으면 "
            "ANY 다.",
            (
                Example("학사 이상", "BACHELOR"),
                Example("대졸", "BACHELOR"),
                Example("고졸 이상", "HIGH_SCHOOL"),
                Example("학력 무관", "ANY"),
            ),
        ),
        "closes_when_filled": FieldRule(
            "채용 시 마감 여부. 인원이 차면 마감일 전에 마감될 수 있다고 적혀 있으면 true, "
            "그런 말이 없으면 false 다.",
            (
                Example("적격자 채용 시 조기 마감", "true"),
                Example("충원 시 마감", "true"),
            ),
        ),
        "application_method": FieldRule(
            "지원 방법. 이메일로 지원서를 받는다고 적혀 있으면 EMAIL, 그 밖에는 EXTERNAL_PAGE 다.",
            (Example("지원서를 이메일로 제출", "EMAIL"),),
        ),
        "job_field": FieldRule(
            "**항상 채운다.** 정확히 들어맞는 대분류가 없어도, 이 공고가 하는 일과 가장 "
            "가까운 대분류를 고른다 — 완벽히 맞는 것을 찾는 것이 아니라 다른 후보보다 "
            "조금이라도 더 가까운 것을 고르는 일이다. 비워 두지 않는다."
        ),
        "job_role": FieldRule(
            "대분류는 골랐는데 그 밑의 소분류 중 맞는 것이 없으면, 그 대분류 목록의 마지막에 "
            "있는 `기타`로 시작하는 소분류(예: 기타IT·개발)를 고른다 — 비워 두지 않는다."
        ),
        "industry": FieldRule(
            "산업. 이 공고를 낸 회사가 속한 산업을 고른다. 회사 소개와 하는 일을 보고 가장 가까운 "
            "산업을 고르고, 계열사 공고는 그 계열사의 산업이다. 비워 두지 않는다.",
            (
                Example("반도체 메모리를 설계하고 생산합니다", "제조·생산·화학업"),
                Example("모바일 뱅킹 서비스를 운영합니다", "금융·은행업"),
            ),
        ),
    },
)


def current(conn: sqlite3.Connection) -> RuleVersion:
    """지금 쓰는 판. 저장한 판이 없으면 기본 규칙(판 0)이다. 가장 최근 판이 깨졌으면 거절한다.

    깨진 판을 건너뛰고 그 앞 판을 쓰지 않는다 — 누군가 DB 를 직접 고쳤다는 뜻이고, 조용히 옛
    규칙으로 분류하면 화면에 보이는 규칙과 실제로 쓴 규칙이 갈린다.
    """
    row = conn.execute(
        "SELECT id, rules_json, note, created_at FROM classify_rule_versions"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return RuleVersion(0, DEFAULT_RULES, "기본 규칙")
    return _version(row)


def read(conn: sqlite3.Connection, number: int) -> RuleVersion:
    """판 하나. 0 은 기본 규칙이다."""
    if number == 0:
        return RuleVersion(0, DEFAULT_RULES, "기본 규칙")
    row = conn.execute(
        "SELECT id, rules_json, note, created_at FROM classify_rule_versions WHERE id = ?",
        (number,),
    ).fetchone()
    if row is None:
        raise RuleSetError("not_found", f"판 {number} 이 없다")
    return _version(row)


def history(conn: sqlite3.Connection, limit: int = 50) -> list[VersionEntry]:
    """저장한 판. 최근 것부터다."""
    rows = conn.execute(
        "SELECT id, note, created_at FROM classify_rule_versions ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [VersionEntry(int(row["id"]), str(row["note"]), str(row["created_at"])) for row in rows]


def save(conn: sqlite3.Connection, rules: RuleSet, note: str = "") -> RuleVersion:
    """새 판으로 저장한다. 지금 판과 규칙이 같으면 판을 만들지 않고 거절한다."""
    validate(rules)
    note = note.strip()
    if len(note) > MAX_NOTE_CHARS:
        raise RuleSetError(
            "too_long", f"바꾼 이유가 {len(note)}자다. {MAX_NOTE_CHARS}자까지 적는다"
        )
    text = to_json(rules)
    try:
        latest = current(conn)
    except RuleSetError:
        # 깨진 판 위에는 무엇이든 새로 저장할 수 있어야 한다. 그것이 깨진 판을 대신하는 길이다
        latest = None
    if latest is not None and to_json(latest.rules) == text:
        raise RuleSetError("unchanged", f"판 {latest.number} 과 규칙이 같아 새 판을 만들지 않았다")
    cursor = conn.execute(
        "INSERT INTO classify_rule_versions (rules_json, note) VALUES (?, ?)", (text, note)
    )
    return read(conn, int(cursor.lastrowid or 0))


def restore(conn: sqlite3.Connection, number: int) -> RuleVersion:
    """옛 판을 복사해 새 판으로 저장한다. 옛 판은 그대로 남는다."""
    return save(conn, read(conn, number).rules, f"판 {number} 으로 되돌림")


def validate(rules: RuleSet) -> None:
    """모델에 뜻 없이 가거나 프롬프트를 넘치게 할 규칙을 거절한다."""
    if len(rules.common) > MAX_COMMON_CHARS:
        raise RuleSetError(
            "too_long", f"공통 규칙이 {len(rules.common)}자다. {MAX_COMMON_CHARS}자까지 적는다"
        )
    unknown = sorted(set(rules.fields) - set(FIELD_LABELS))
    if unknown:
        raise RuleSetError("unknown_rule_field", f"규칙을 둘 수 없는 칸이다: {', '.join(unknown)}")
    for name, label, _ in RULE_FIELDS:
        rule = rules.fields.get(name, FieldRule())
        if len(rule.rule) > MAX_RULE_CHARS:
            raise RuleSetError(
                "too_long", f"{label} 규칙이 {len(rule.rule)}자다. {MAX_RULE_CHARS}자까지 적는다"
            )
        if len(rule.examples) > MAX_EXAMPLES:
            raise RuleSetError(
                "too_many", f"{label} 예시가 {len(rule.examples)}개다. {MAX_EXAMPLES}개까지 둔다"
            )
        for example in rule.examples:
            if not example.source.strip() or not example.value.strip():
                raise RuleSetError("half_example", f"{label} 예시는 원문과 넣을 값을 둘 다 적는다")
            if max(len(example.source), len(example.value)) > MAX_EXAMPLE_CHARS:
                raise RuleSetError(
                    "too_long", f"{label} 예시 한 칸은 {MAX_EXAMPLE_CHARS}자까지 적는다"
                )


def parse_form(form: Any) -> RuleSet:
    """화면 폼에서 규칙 한 벌을 읽는다. 검사는 하지 않는다 — 틀려도 친 내용을 그대로 돌려줘야 한다.

    원문과 넣을 값이 둘 다 빈 예시 줄은 버린다. 화면이 끝에 붙여 두는 빈 줄이다. 한쪽만 적은 줄은
    남긴다 — 버리면 반쯤 적은 예시가 말없이 사라지고, `validate` 가 사유를 댈 기회도 없다.
    """
    fields: dict[str, FieldRule] = {}
    for name, _, _ in RULE_FIELDS:
        sources = [_text(value) for value in form.getlist(f"example_source__{name}")]
        values = [_text(value) for value in form.getlist(f"example_value__{name}")]
        examples = tuple(
            Example(source, value)
            for source, value in zip(sources, values, strict=False)
            if source or value
        )
        fields[name] = FieldRule(_text(form.get(f"rule__{name}")), examples)
    return RuleSet(_text(form.get("common")), fields)


def to_json(rules: RuleSet) -> str:
    """저장하는 모양. 칸 순서는 `RULE_FIELDS` 그대로라 같은 규칙이면 글자도 같다."""
    return json.dumps(
        {
            "common": rules.common,
            "fields": {
                name: {
                    "rule": rules.fields.get(name, FieldRule()).rule,
                    "examples": [
                        {"source": example.source, "value": example.value}
                        for example in rules.fields.get(name, FieldRule()).examples
                    ],
                }
                for name, _, _ in RULE_FIELDS
            },
        },
        ensure_ascii=False,
    )


def from_json(text: str, number: int = 0) -> RuleSet:
    """저장된 모양에서 읽는다. 판에 없는 칸은 기본 규칙으로 채운다."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuleSetError("broken_version", f"판 {number} 을 읽지 못했다: {exc}") from exc
    if not isinstance(data, dict):
        raise RuleSetError("broken_version", f"판 {number} 이 규칙 모양이 아니다")
    raw_fields = data.get("fields")
    stored: dict[str, Any] = raw_fields if isinstance(raw_fields, dict) else {}
    fields: dict[str, FieldRule] = {}
    for name, _, _ in RULE_FIELDS:
        entry = stored.get(name)
        if not isinstance(entry, dict):
            fields[name] = DEFAULT_RULES.fields[name]
            continue
        examples = tuple(
            Example(str(item.get("source", "")), str(item.get("value", "")))
            for item in entry.get("examples") or []
            if isinstance(item, dict)
        )
        fields[name] = FieldRule(str(entry.get("rule", "")), examples)
    return RuleSet(str(data.get("common", "")), fields)


def render_fields(
    rules: RuleSet, names: Sequence[str], choices: Mapping[str, str] | None = None
) -> str:
    """칸 규칙을 프롬프트 줄로 옮긴다. 판정 칸은 고를 수 있는 값을 규칙 글 바로 뒤에 적는다."""
    lines: list[str] = []
    for name in names:
        rule = rules.fields.get(name, DEFAULT_RULES.fields[name])
        text = rule.rule.strip().splitlines() or [""]
        lines.append(f"- {name}: {text[0]}".rstrip())
        lines.extend(f"  {line}".rstrip() for line in text[1:])
        if choices and name in choices:
            lines.append(f"  고를 수 있는 값: {choices[name]}")
        lines.extend(f"  예: `{example.source}` → `{example.value}`" for example in rule.examples)
    return "\n".join(lines)


def render_common(rules: RuleSet) -> str:
    """공통 규칙 구역. 비워 두면 구역 자체가 없다."""
    text = rules.common.strip()
    return f"\n# 공통 규칙\n\n{text}\n" if text else ""


def _version(row: sqlite3.Row) -> RuleVersion:
    number = int(row["id"])
    return RuleVersion(
        number, from_json(str(row["rules_json"]), number), str(row["note"]), str(row["created_at"])
    )


def _text(value: Any) -> str:
    """폼 값 하나. 브라우저가 보내는 CRLF 를 줄바꿈 하나로 맞춘다."""
    return value.replace("\r\n", "\n").strip() if isinstance(value, str) else ""
