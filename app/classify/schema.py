"""분류 응답의 스키마와 검증.

모델이 돌려준 것은 가설이지 결과가 아니다 (`.claude/rules/llm.md`). 이 파일은 받은 것이
읽어도 되는 모양인지만 판정하고, 그 값이 본문에 근거가 있는지는 `app/classify/grounding.py`
가 본다.

칸은 `normalized_jobs` 에 이미 있는 아홉 개다. 새로 만들지 않는다
(`migrations/0011_split_body_columns.sql`). 0016 이 부서·직군·모집인원을 뺐고
(`migrations/0016_drop_department_category_headcount.sql`) 0017 이 직무를 더했다
(`migrations/0017_job_role.sql`). 0028 이 오공고가 받는 다섯 칸(회사·팀 소개, 급여·처우,
복지·혜택, 학력, 모집인원)을 더했다 (`migrations/0028_add_posting_detail_fields.sql`).

## 칸이 두 가지다

**뽑는 칸**은 원문에 있는 글자를 그대로 가져온다. 모델은 글자를 쓰지 않고 몇 번 줄의 어느
부분인지를 조각으로 답하고, 저장하는 글자는 원문에서 잘라 온다 (`app/classify/pieces.py`).

`job_role` 만 원문이 본문이 아니라 **제목**이다. 열한 사이트 픽스처에서 제목이 직무를 말하는
곳이 아홉이고 그중 본문이 같은 글자를 되풀이하는 곳은 셋뿐이었다
(`tests/test_job_role_source.py`). 직무를 판정 칸으로 만들지 않는 것은 값이 자유 텍스트이기
때문이다 — 닫힌 목록을 만들 수 있었으면 그것이 직군이고, 직군은 0016 이 지웠다.

**판정하는 칸**은 본문을 읽고 정해진 값 중에서 고른다. `정규직 채용` 이라고 본문에 그대로
적혀 있지 않은 공고가 많다 — 글자 일치를 요구하면 이 칸은 영원히 빈다. 매핑 방식의 채움률이
고용형태 26%, 경력 45% 였던 이유가 그것이다.

판정 칸은 **반드시 닫힌 목록**이다. 목록을 정하지 않으면 같은 일이 사이트마다 다른 이름으로
쌓인다 — 운영 DB 640건에 `Permanent` 71건과 `정규직` 7건과 `정규` 3건이 따로 있고,
`Experienced` 77건과 `경력` 100건이 따로 있다. 그러면 소비 측이 그 칸으로 거를 수 없다.

목록은 프롬프트로 부탁하지 않고 **응답 스키마의 enum 으로 강제한다.** 부탁은 대개 지켜지고,
대개는 640건에서 스무 건쯤 어긋난다는 뜻이다.

판정 칸에는 근거 문장이 따라온다(`*_evidence`). 그 문장이 본문에 없으면 판정을 버린다 —
읽고 고른 것인지 지어낸 것인지 가를 방법이 그것뿐이다.

"본문만으로는 고를 수 없다" 를 답할 자리가 `판단불가` 다. 그 자리가 없으면 모델은 아무거나
고른다. 빈 문자열을 쓰지 않는 것은 **Gemini 가 빈 문자열이 든 enum 을 400 으로 거절하기
때문이다** (2026-08-26 확인: `response_schema.properties[career_level].enum[0]: cannot be
empty`). `판단불가` 는 저장되지 않고 빈 칸이 된다.

| reason | 뜻 |
|---|---|
| `unparsable` | JSON 이 아니거나, 객체·문자열이 아닌 자리에 다른 타입이 왔다 |
| `unknown_field` | 스키마에 없는 칸 이름이 왔다 |

`unparsable` 만 1회 재요청 대상이다. 스키마에 없는 칸을 지어낸 것은 모양이 아니라 내용의
문제라 다시 물어도 같은 답이 온다.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any, Final, Literal, get_args

from pydantic import BaseModel, Field, create_model

from app import taxonomy

# 아래 모델은 Gemini 의 response_schema 로 그대로 나간다. `extra="forbid"` 를 걸면
# `additionalProperties: false` 로 변환되는데 Gemini 가 그 필드를 모르고 400 을 낸다.
# 스키마에 없는 칸 이름을 거르는 일은 `validate_classification()` 이 받은 뒤에 한다.


# 본문만으로는 고를 수 없다는 답. 목록의 값이 아니라 "고르지 않았다" 는 표시이고, 저장될
# 때는 빈 칸이 된다. 빈 문자열을 쓰지 못하는 것은 Gemini 가 빈 값이 든 enum 을 거절해서다.
#
# 아래 `Literal` 안에는 이 이름 대신 같은 글자를 적는다. 타입 검사기는 Literal 안에서 변수를
# 읽지 못한다. 둘이 갈리지 않는지는 `_choices()` 아래의 검사가 본다
UNDECIDED: Final = "판단불가"


# 뽑는 칸의 조각 하나를 코드 안에서 들고 다니는 모양. (줄 번호, 그 줄에서 가져올 부분)
Piece = tuple[int, str]


class LinePiece(BaseModel):
    """뽑는 칸의 조각 하나. 몇 번 줄의 어느 부분인지를 모델이 답한다.

    저장하는 글자는 이 `text` 가 아니라 원문에서 잘라 온 것이다 (`app/classify/pieces.py`).
    """

    line: int
    text: str


class Classification(BaseModel):
    """공고 하나를 나눈 칸들과, 판정 칸의 근거 문장.

    뽑는 칸은 조각 목록이고 원문에 없으면 빈 목록이다. 판정 칸은 `Literal` 이라 목록에
    없는 값이 애초에 응답에 담기지 못한다. `판단불가` 가 목록에 있는 것은 "본문만으로는 고를
    수 없다" 를 답할 자리가 있어야 하기 때문이다 — 자리가 없으면 모델은 아무거나 고른다.
    """

    # 판정하는 칸. 목록은 운영 DB 640건의 실제 값에서 뽑았다
    employment_type: Literal["판단불가", "정규직", "계약직", "인턴", "기타"] = UNDECIDED
    employment_type_evidence: str = ""

    career_level: Literal["판단불가", "신입", "경력", "무관"] = UNDECIDED
    career_level_evidence: str = ""

    # 0028. 지원 자격이 요구하는 최소 학력. 우대사항에만 있는 학력은 고르지 않고, 학력을
    # 말하지 않는 공고는 판단불가(빈 칸)다. 오공고로 보낼 때 그쪽 목록으로 옮긴다
    education_level: Literal["판단불가", "무관", "고졸", "전문학사", "학사", "석사", "박사"] = (
        UNDECIDED
    )
    education_level_evidence: str = ""

    # 뽑는 칸. 모델은 글자를 쓰지 않고 몇 번 줄의 어느 부분인지를 조각으로 답한다. 저장은
    # 원문에서 잘라 온 글자다 (`app/classify/pieces.py`). `job_role` 만 0 번 줄(제목)에서 온다
    job_role: list[LinePiece] = Field(default_factory=list)
    work_location: list[LinePiece] = Field(default_factory=list)
    duties: list[LinePiece] = Field(default_factory=list)
    preferred: list[LinePiece] = Field(default_factory=list)
    hiring_process: list[LinePiece] = Field(default_factory=list)
    requirements: list[LinePiece] = Field(default_factory=list)
    etc_info: list[LinePiece] = Field(default_factory=list)
    # 0028. 회사·팀 소개는 공고에 그 소제목 구역이 있을 때만 채운다. 모집인원은 적힌 그대로다 —
    # 숫자로 바꾸는 것은 오공고로 보낼 때 한다
    company_and_team_introduction: list[LinePiece] = Field(default_factory=list)
    compensation: list[LinePiece] = Field(default_factory=list)
    benefits: list[LinePiece] = Field(default_factory=list)
    recruitment_headcount: list[LinePiece] = Field(default_factory=list)

    # 수집이 이미 채운 칸을 원문과 견줘 다르면 낸다 (Push 11, PRD 6절). 값이 같거나 판단할
    # 근거가 없으면 둘 다 빈 문자열이다 — 이 칸이 채워진다고 그 값이 그대로 저장되지 않는다.
    # 근거 검사(`app/classify/grounding.py`)를 통과한 것만 `job_field_suggestions` 로 가고,
    # 정규화의 어느 경로도 이 제안을 읽지 않는다(`app/normalize/engine.py` 는 그대로 둔다).
    company_suggestion: str = ""
    company_suggestion_reason: str = ""
    deadline_suggestion: str = ""
    deadline_suggestion_reason: str = ""
    start_date_suggestion: str = ""
    start_date_suggestion_reason: str = ""


# 본문을 읽고 정해진 값 중에서 고르는 칸
JUDGE_FIELDS: tuple[str, ...] = ("employment_type", "career_level", "education_level")

# 수집이 채우는 여섯 칸 중, 원문을 읽어 다른 값을 낼 수 있는 셋. `title` 은 이미 `job_role` 의
# 출처로 프롬프트에 그대로 들어가 있어 다시 비교할 이유가 없고, `body` 는 모델에게 보내는
# 원문 그 자체라 비교할 대상이 없다. `source_url` 은 공고의 신원이라 애초에 후보가 아니다.
#
# `deadline` 은 마감 지난 공고를 거르는 데 쓰이고 `company` 는 계열사를 가르는 값이라, 이
# 셋은 값이 있으면 아무리 근거가 있어도 자동으로 덮지 않고 제안으로만 낸다
# (`.claude/tasks/todo/prd-side-workflows.md` 6절).
COLLECTED_REVIEW_FIELDS: tuple[str, ...] = ("company", "deadline", "start_date")

# 화면에 보일 이름. 프롬프트에 값을 적을 때도 같은 이름을 쓴다
COLLECTED_REVIEW_LABELS: dict[str, str] = {
    "company": "회사명",
    "deadline": "마감일",
    "start_date": "모집 시작일",
}


def suggestion_field(name: str) -> str:
    """그 칸의 제안 값이 담기는 응답 필드 이름."""
    return f"{name}_suggestion"


def suggestion_reason_field(name: str) -> str:
    """그 칸의 제안 이유가 담기는 응답 필드 이름."""
    return f"{name}_suggestion_reason"


# 판정 칸마다 따라오는 근거 문장. 컬럼이 아니라 검증과 보고를 위한 값이다
EVIDENCE_FIELDS: tuple[str, ...] = tuple(f"{name}_evidence" for name in JUDGE_FIELDS)

# 원문에 있는 글자를 그대로 가져오는 칸. `job_role` 은 제목에서, 나머지는 본문에서 온다
EXTRACT_FIELDS: tuple[str, ...] = (
    "job_role",
    "work_location",
    "duties",
    "preferred",
    "hiring_process",
    "requirements",
    "etc_info",
    "company_and_team_introduction",
    "compensation",
    "benefits",
    "recruitment_headcount",
)

# 분류가 채우는 칸. `normalized_jobs` 의 같은 이름 컬럼으로 간다
CLASSIFY_FIELDS: tuple[str, ...] = (*JUDGE_FIELDS, *EXTRACT_FIELDS)

# 응답에 올 수 있는 이름 전부
RESPONSE_FIELDS: tuple[str, ...] = tuple(Classification.model_fields)

# 직무 분류. `job_taxonomy`(운영 DB 표)에서 고르는 판정 칸 둘이라 `Classification`(정적
# pydantic 모델)에도, 위 `CLASSIFY_FIELDS`/`RESPONSE_FIELDS`(둘 다 그 정적 모델에서 뽑는다)
# 에도 없다 — 목록이 배포 없이 바뀌어야 해서 호출 시점에 `build_classification_model()` 이
# 이 두 칸을 가진 모델을 새로 만든다. `CLASSIFY_FIELDS` 를 그대로 넓히지 않는 이유는
# `EXTRACT_FIELDS | JUDGE_FIELDS == CLASSIFY_FIELDS` (`tests/test_classify_body.py`)가
# "이 아홉 칸은 전부 정적 모델의 필드다" 를 지키는 불변식이기 때문이다. 근거 검사
# (`app/classify/grounding.py`)에 이 둘을 엮는 것은 Push 3 이 한다 — 지금은 저장 경로
# (`app/classify/store.py`, `app/normalize/engine.py`)만 이 두 칸을 안다
JOB_MAJOR: Final = "job_major"
JOB_MINOR: Final = "job_minor"
TAXONOMY_FIELDS: tuple[str, ...] = (JOB_MAJOR, JOB_MINOR)

# 저장 경로(분류 결과 표, 정규화)가 옮기는 칸 전부. `CLASSIFY_FIELDS` 에 직무 분류 둘을 더한
# 것이다 — `job_classifications`/`normalized_jobs` 양쪽 다 이 두 칸의 컬럼을 갖는다
# (`migrations/0025_job_major_minor.sql`)
STORED_CLASSIFY_FIELDS: tuple[str, ...] = (*CLASSIFY_FIELDS, *TAXONOMY_FIELDS)


def _choices(name: str) -> tuple[str, ...]:
    annotation = Classification.model_fields[name].annotation
    return tuple(value for value in get_args(annotation) if value and value != UNDECIDED)


# 판정 칸이 고를 수 있는 값. `판단불가` 는 값이 아니라 "고르지 않았다" 는 표시라 여기 없다.
# 모델에 보내는 목록과 받은 뒤 거르는 목록이 같아야 해서 스키마 하나에서 뽑는다 — 두 벌을
# 두면 목록을 넓힐 때 한쪽만 넓어진다
JUDGE_CHOICES: dict[str, tuple[str, ...]] = {name: _choices(name) for name in JUDGE_FIELDS}

# 스키마에 적은 글자와 위 상수가 갈리면 "고르지 않았다" 가 목록 안의 값이 되어 그대로 저장된다.
# 임포트 시점에 걸린다 — 640건을 돌린 뒤에 알게 될 일이 아니다
for _name in JUDGE_FIELDS:
    assert UNDECIDED in get_args(Classification.model_fields[_name].annotation), _name


def build_classification_model(conn: sqlite3.Connection) -> type[Classification]:
    """`job_taxonomy` 의 켜진 값으로 `job_major`/`job_minor` 를 더한 모델을 만든다.

    `Classification` 은 고치지 않는다 — 그 클래스는 배포 시점에 고정된 아홉 칸의 모양이고,
    직무 분류는 운영 중에 표가 바뀌면 다음 호출부터 목록이 따라와야 한다. 그래서 매 호출
    시점에 이 함수로 새 모델을 만든다.

    **켜진 대분류가 하나도 없으면(표가 비었거나 전부 껐으면) `Classification` 을 그대로
    돌려준다.** 고를 것이 없는 판정 칸을 모델에 보내면 그 자리를 채우라고 강요하는 것과
    같다. 대분류는 있는데 켜진 소분류가 하나도 없으면 `job_minor` 없이 `job_major` 만 더한다.
    """
    majors = taxonomy.list_majors(conn, enabled_only=True)
    if not majors:
        return Classification

    major_names = tuple(major.name for major in majors)
    minor_names = tuple(
        minor.name
        for major in majors
        for minor in taxonomy.list_minors(conn, major.id, enabled_only=True)
    )

    fields: dict[str, Any] = {
        JOB_MAJOR: (Literal[(*major_names, UNDECIDED)], UNDECIDED),
        f"{JOB_MAJOR}_evidence": (str, ""),
    }
    if minor_names:
        fields[JOB_MINOR] = (Literal[(*minor_names, UNDECIDED)], UNDECIDED)
        fields[f"{JOB_MINOR}_evidence"] = (str, "")

    return create_model("ClassificationWithTaxonomy", __base__=Classification, **fields)


class ClassifySchemaError(ValueError):
    """검증 실패. `reason` 은 위 표의 값 중 하나다."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def validate_classification(
    data: Any, response_fields: tuple[str, ...] = RESPONSE_FIELDS
) -> tuple[dict[str, str], dict[str, list[Piece]]]:
    """파싱된 응답을 검증해 글자 칸과 뽑는 칸의 조각을 따로 돌려준다. 없는 키는 빈 값이다.

    스키마에 없는 칸 이름이 오면 무엇을 말하려던 것인지 추측해서 고치지 않는다. 조용히 고친
    값은 나중에 왜 그 칸에 그 값이 들어갔는지 아무도 설명하지 못한다.

    `response_fields` 는 기본값이 정적 아홉 칸(`RESPONSE_FIELDS`)이지만, 직무 분류가 있는
    호출은 `build_classification_model()` 이 만든 그 모델의 필드 이름을 넘긴다 — 그 두 칸은
    호출마다 있을 수도 없을 수도 있어 고정 튜플에 넣을 수 없다(`app/classify/schema.py` 의
    `TAXONOMY_FIELDS` 설명).

    판정 칸의 값이 목록 밖이면 여기서 버리지 않고 그대로 넘긴다. 무엇을 왜 버렸는지 한자리에서
    세려고 판정은 `app/classify/grounding.py` 가 한다.
    """
    if not isinstance(data, Mapping):
        raise ClassifySchemaError("unparsable", f"응답이 객체가 아니다: {type(data).__name__}")

    unknown = sorted(str(key) for key in data if key not in response_fields)
    if unknown:
        raise ClassifySchemaError("unknown_field", f"스키마에 없는 칸이 있다: {', '.join(unknown)}")

    result: dict[str, str] = {}
    pieces: dict[str, list[Piece]] = {}
    for name in response_fields:
        raw = data.get(name, "")
        if name in EXTRACT_FIELDS:
            pieces[name] = _pieces(name, raw)
            continue
        if raw is None:
            raw = ""
        if not isinstance(raw, str):
            raise ClassifySchemaError(
                "unparsable", f"`{name}` 이 문자열이 아니다: {type(raw).__name__}"
            )
        result[name] = raw.strip()
    return result, pieces


def _pieces(name: str, raw: Any) -> list[Piece]:
    """뽑는 칸 하나의 조각 목록. 빈 문자열과 None 은 조각이 없는 것으로 읽는다."""
    if raw is None or raw == "":
        return []
    if not isinstance(raw, list):
        raise ClassifySchemaError(
            "unparsable", f"`{name}` 이 조각 목록이 아니다: {type(raw).__name__}"
        )
    pieces: list[Piece] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ClassifySchemaError(
                "unparsable", f"`{name}` 의 조각이 객체가 아니다: {type(item).__name__}"
            )
        line = item.get("line")
        text = item.get("text", "")
        if isinstance(line, bool) or not isinstance(line, int):
            raise ClassifySchemaError("unparsable", f"`{name}` 의 조각에 줄 번호가 없다: {line!r}")
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise ClassifySchemaError(
                "unparsable", f"`{name}` 의 조각 글자가 문자열이 아니다: {type(text).__name__}"
            )
        pieces.append((line, text))
    return pieces


def parse_classification(
    text: str, response_fields: tuple[str, ...] = RESPONSE_FIELDS
) -> tuple[dict[str, str], dict[str, list[Piece]]]:
    """모델 응답 문자열을 파싱하고 검증한다. 글자 칸과 뽑는 칸의 조각을 따로 돌려준다."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClassifySchemaError("unparsable", f"JSON 으로 읽을 수 없다: {exc}") from exc
    return validate_classification(data, response_fields)
