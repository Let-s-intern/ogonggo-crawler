"""분류 응답의 스키마와 검증.

모델이 돌려준 것은 가설이지 결과가 아니다 (`.claude/rules/llm.md`). 이 파일은 받은 것이
읽어도 되는 모양인지만 판정하고, 그 값이 본문에 근거가 있는지는 `app/classify/grounding.py`
가 본다.

칸은 `normalized_jobs` 에 이미 있는 아홉 개다. 새로 만들지 않는다
(`migrations/0011_split_body_columns.sql`). 0016 이 부서·직군·모집인원을 뺐고
(`migrations/0016_drop_department_category_headcount.sql`) 0017 이 직무를 더했다
(`migrations/0017_job_role.sql`). 0028 이 오공고가 받는 다섯 칸(회사·팀 소개, 급여·처우,
복지·혜택, 학력, 모집인원)을 더했다 (`migrations/0028_add_posting_detail_fields.sql`). 0033 이
오공고가 받는 판정 칸 셋(최소 경력 연수, 채용 시 마감, 지원 방법)을 더했다
(`migrations/0033_spring_job_values.sql`).

## 칸이 세 가지다

**뽑는 칸**은 원문에 있는 글자를 그대로 가져온다. 모델은 글자를 쓰지 않고 몇 번 줄의 어느
부분인지를 조각으로 답하고, 저장하는 글자는 원문에서 잘라 온다 (`app/classify/pieces.py`).

`position_name` 만 원문이 본문이 아니라 **제목**이다. 열한 사이트 픽스처에서 제목이 직무를 말하는
곳이 아홉이고 그중 본문이 같은 글자를 되풀이하는 곳은 셋뿐이었다
(`tests/test_job_role_source.py`).

**판정하는 칸**은 본문을 읽고 정해진 값 중에서 고른다. `정규직 채용` 이라고 본문에 그대로
적혀 있지 않은 공고가 많다 — 글자 일치를 요구하면 이 칸은 영원히 빈다. 매핑 방식의 채움률이
고용형태 26%, 경력 45% 였던 이유가 그것이다.

판정 칸은 **반드시 닫힌 목록**이다. 목록을 정하지 않으면 같은 일이 사이트마다 다른 이름으로
쌓인다 — 운영 DB 640건에 `Permanent` 71건과 `정규직` 7건과 `정규` 3건이 따로 있고,
`Experienced` 77건과 `경력` 100건이 따로 있다. 그러면 소비 측이 그 칸으로 거를 수 없다.

목록은 오공고(Spring) enum 과 같은 이름이다(`FULL_TIME` 등). 크롤러 DB 에도 그 이름 그대로
저장하고, 화면에만 한글 이름을 보인다 (2026-09-14 결정). 목록은 프롬프트로 부탁하지 않고 **응답
스키마의 enum 으로 강제한다.** 부탁은 대개 지켜지고, 대개는 640건에서 스무 건쯤 어긋난다는 뜻이다.

**판정 칸은 늘 하나를 고른다.** "본문만으로는 고를 수 없다" 를 답하던 `판단불가` 는 없앴다 —
빈 값으로 보내 오공고가 기본값을 만들게 하지 않고, 크롤러 AI 가 공고를 읽고 가장 그럴듯한 값을
고른다 (2026-09-14 결정). 근거 문장(`*_evidence`)은 함께 받되, 본문에서 찾지 못해도 값은 남기고
검수 화면이 `근거 없음` 으로 보인다 (`app/classify/grounding.py`).

**숫자 칸**(최소 경력 연수)은 사실 값이라 원문에 근거가 있을 때만 채운다. 근거 문장을 찾지 못한
값은 버린다.

## 공고가 여럿일 수 있다

공고 한 건에 직무가 여럿이면 직무마다 공고 하나로 나눈다 (2026-09-11 결정). 응답은 직무마다
하나인 `postings` 와, 모든 직무에 같은 뽑는 칸을 한 번만 담는 `common` 으로 온다. 칸마다
`common` 의 조각과 그 공고의 조각을 코드가 합친다 — 공통 내용을 직무 수만큼 되풀이하게 하면
삼성 한 건에서 2,000자를 열두 번 적어 응답이 잘린다. 직무가 하나인 공고는 `postings` 가 하나다.

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
from dataclasses import dataclass
from typing import Any, Final, Literal, cast, get_args

from pydantic import BaseModel, Field, create_model

from app import industries, regions, taxonomy

# 아래 모델은 Gemini 의 response_schema 로 그대로 나간다. `extra="forbid"` 를 걸면
# `additionalProperties: false` 로 변환되는데 Gemini 가 그 필드를 모르고 400 을 낸다.
# 스키마에 없는 칸 이름을 거르는 일은 `validate_classification()` 이 받은 뒤에 한다.


# 오공고(Spring) enum 과 같은 값. 키가 저장하는 이름이고 값이 화면 이름이다. 화면 이름은 오공고
# enum 의 설명을 그대로 옮겼다 (`ogonggo-core/.../job/domain/*.kt`)
EMPLOYMENT_TYPES: Final[dict[str, str]] = {
    "FULL_TIME": "정규직",
    "CONTRACT": "계약직",
    "INTERN": "인턴",
    "PART_TIME": "파트타임",
    "ETC": "기타",
}
EXPERIENCE_TYPES: Final[dict[str, str]] = {
    "NEWCOMER": "신입",
    "EXPERIENCED": "경력",
    "BOTH": "신입·경력",
    "IRRELEVANT": "경력 무관",
}
EDUCATION_LEVELS: Final[dict[str, str]] = {
    "ANY": "학력 무관",
    "HIGH_SCHOOL": "고등학교 졸업",
    "ASSOCIATE": "전문학사",
    "BACHELOR": "학사",
    "MASTER": "석사",
    "DOCTORATE": "박사",
}
CLOSES_WHEN_FILLED: Final[dict[str, str]] = {"true": "채용 시 마감", "false": "마감일까지 접수"}
APPLICATION_METHODS: Final[dict[str, str]] = {"EXTERNAL_PAGE": "외부 페이지", "EMAIL": "이메일"}
# 모델이 고르지 않고 정규화가 마감일에서 정하는 두 칸 (`app/normalize/engine.py`)
RECRUITMENT_TYPES: Final[dict[str, str]] = {"PERIOD": "기간 채용", "ALWAYS_OPEN": "상시 채용"}
AUTO_CLOSE: Final[dict[str, str]] = {"true": "마감일에 자동 종료", "false": "자동 종료 안 함"}


# 뽑는 칸의 조각 하나를 코드 안에서 들고 다니는 모양. (줄 번호, 그 줄에서 가져올 부분)
Piece = tuple[int, str]


class LinePiece(BaseModel):
    """뽑는 칸의 조각 하나. 몇 번 줄의 어느 부분인지를 모델이 답한다.

    저장하는 글자는 이 `text` 가 아니라 원문에서 잘라 온 것이다 (`app/classify/pieces.py`).
    """

    line: int
    text: str


class Posting(BaseModel):
    """나눈 공고 하나의 칸들과, 판정 칸의 근거 문장.

    뽑는 칸은 조각 목록이고 원문에 없으면 빈 목록이다. 판정 칸은 `Literal` 이라 목록에
    없는 값이 애초에 응답에 담기지 못하고, 기본값이 없어 반드시 하나를 골라야 한다.

    `Literal` 안의 글자는 위 표의 키와 같아야 한다. 타입 검사기가 Literal 안에서 변수를 읽지
    못해 글자로 적는다. 둘이 갈리지 않는지는 `JUDGE_CHOICES` 아래의 검사가 본다.
    """

    # 판정하는 칸. 오공고 enum 이름으로 고른다
    employment_type: Literal["FULL_TIME", "CONTRACT", "INTERN", "PART_TIME", "ETC"]
    employment_type_evidence: str = ""

    experience_type: Literal["NEWCOMER", "EXPERIENCED", "BOTH", "IRRELEVANT"]
    experience_type_evidence: str = ""

    # 0033. 경력 공고에서 원문이 최소 연수를 말할 때만 숫자다. 근거 문장이 없으면 버린다
    experience_min_years: str = ""
    experience_min_years_evidence: str = ""

    # 근무지. 큰 지역 목록(`app/regions.py`)에서 여러 개를 고른다 (2026-09-21 결정). 원문 글자를
    # 옮기던 칸이었는데 같은 곳이 제각각 쌓였다. 목록은 응답 모델을 만들 때 enum 으로 건다
    # (`build_classification_model`) — 여기서 `Literal` 로 적으면 씨앗 파일과 두 벌이 된다
    region: list[str] = Field(default_factory=list)
    region_evidence: str = ""

    # 0043. 공고에 적힌 이메일 주소를 그대로 옮긴다. 원문에 그 주소가 있을 때만 남는다 — 주소 자체가
    # 근거라 근거 문장을 따로 받지 않는다 (`app/classify/grounding.py`). 오공고가 받는 칸이다
    application_email: str = ""
    inquiry_email: str = ""

    # 0028. 지원 자격이 요구하는 최소 학력. 우대사항에만 있는 학력은 고르지 않는다
    education_level: Literal["ANY", "HIGH_SCHOOL", "ASSOCIATE", "BACHELOR", "MASTER", "DOCTORATE"]
    education_level_evidence: str = ""

    # 0033. 인원이 차면 마감될 수 있다고 적혀 있으면 true 다
    closes_when_filled: Literal["true", "false"]
    closes_when_filled_evidence: str = ""

    # 0033. 이메일로 지원서를 받으면 EMAIL 이다
    application_method: Literal["EXTERNAL_PAGE", "EMAIL"]
    application_method_evidence: str = ""

    # 0041. 이 공고의 제목. 사이트 제목을 바탕으로 짓되 직무 이름이 반드시 들어간다. 옮기는 칸이
    # 아니라 짓는 칸이라 원문에 돌려 보지 않는다 (2026-09-18 결정)
    posting_title: str = ""

    # 뽑는 칸. 모델은 글자를 쓰지 않고 몇 번 줄의 어느 부분인지를 조각으로 답한다. 저장은
    # 원문에서 잘라 온 글자다 (`app/classify/pieces.py`). `position_name` 만 0 번 줄(제목)에서 온다
    position_name: list[LinePiece] = Field(default_factory=list)
    responsibilities: list[LinePiece] = Field(default_factory=list)
    preferred_qualifications: list[LinePiece] = Field(default_factory=list)
    hiring_process: list[LinePiece] = Field(default_factory=list)
    qualifications: list[LinePiece] = Field(default_factory=list)
    recruitment_notice: list[LinePiece] = Field(default_factory=list)
    # 0028. 회사·팀 소개는 공고에 그 소제목 구역이 있을 때만 채운다. 모집인원은 적힌 그대로다 —
    # 숫자로 바꾸는 것은 정규화가 한다 (`app/normalize/engine.py`)
    company_and_team_introduction: list[LinePiece] = Field(default_factory=list)
    compensation: list[LinePiece] = Field(default_factory=list)
    benefits: list[LinePiece] = Field(default_factory=list)
    recruitment_headcount: list[LinePiece] = Field(default_factory=list)


class CommonFields(BaseModel):
    """모든 직무에 똑같이 해당하는 뽑는 칸. 코드가 공고마다 그 공고의 조각 앞에 붙인다.

    `position_name` 은 없다 — 직무 이름은 공고마다 다르다. 판정 칸도 없다. 신입·경력처럼 직무마다
    갈릴 수 있고, 공고마다 되풀이해도 한 단어라 응답이 길어지지 않는다.
    """

    responsibilities: list[LinePiece] = Field(default_factory=list)
    preferred_qualifications: list[LinePiece] = Field(default_factory=list)
    hiring_process: list[LinePiece] = Field(default_factory=list)
    qualifications: list[LinePiece] = Field(default_factory=list)
    recruitment_notice: list[LinePiece] = Field(default_factory=list)
    company_and_team_introduction: list[LinePiece] = Field(default_factory=list)
    compensation: list[LinePiece] = Field(default_factory=list)
    benefits: list[LinePiece] = Field(default_factory=list)
    recruitment_headcount: list[LinePiece] = Field(default_factory=list)


class SuggestionFields(BaseModel):
    """수집이 이미 채운 칸을 원문과 견줘 낸 제안. 공고 한 건 전체의 것이라 응답 맨 위에 온다."""

    # 수집이 이미 채운 칸을 원문과 견줘 다르면 낸다 (Push 11, PRD 6절). 값이 같거나 판단할
    # 근거가 없으면 둘 다 빈 문자열이다 — 이 칸이 채워진다고 그 값이 그대로 저장되지 않는다.
    # 근거 검사(`app/classify/grounding.py`)를 통과한 것만 `job_field_suggestions` 로 가고,
    # 정규화의 어느 경로도 이 제안을 읽지 않는다(`app/normalize/engine.py` 는 그대로 둔다).
    company_name_suggestion: str = ""
    company_name_suggestion_reason: str = ""
    recruitment_end_at_suggestion: str = ""
    recruitment_end_at_suggestion_reason: str = ""
    recruitment_start_at_suggestion: str = ""
    recruitment_start_at_suggestion_reason: str = ""


class Classification(SuggestionFields):
    """응답 전체. 직무마다 하나인 `postings` 와 모든 직무에 공통인 `common`.

    직무가 하나인 공고는 `postings` 가 하나다. 제안 칸은 공고 한 건 전체에 대한 것이라 맨
    위에 한 번만 온다.
    """

    common: CommonFields = Field(default_factory=CommonFields)
    postings: list[Posting] = Field(default_factory=list)


class LineRange(BaseModel):
    """이어진 줄 범위. 끝 줄도 들어간다."""

    start: int
    end: int


class OutlineRole(BaseModel):
    """긴 공고에서 직무 하나가 차지하는 줄들."""

    lines: list[LineRange] = Field(default_factory=list)


class Outline(SuggestionFields):
    """긴 공고의 짜임. 직무마다 그 직무의 줄 범위와, 모든 직무에 공통인 줄 범위.

    칸은 여기서 뽑지 않는다. 직무마다 공통 줄과 그 직무의 줄만 다시 보내 `Classification` 으로
    받는다 — 직무가 서른 개인 공고를 한 번에 나누게 하면 응답이 잘린다 (2026-09-11 결정).
    제안 칸은 공고 전체를 보는 이 호출에서만 받는다.
    """

    roles: list[OutlineRole] = Field(default_factory=list)
    common_lines: list[LineRange] = Field(default_factory=list)


# 본문을 읽고 정해진 값 중에서 고르는 칸
JUDGE_FIELDS: tuple[str, ...] = (
    "employment_type",
    "experience_type",
    "education_level",
    "closes_when_filled",
    "application_method",
)

# 원문에 근거가 있을 때만 숫자를 적는 칸. 판정 칸처럼 근거 문장이 따라오지만 목록이 없다
NUMBER_FIELDS: tuple[str, ...] = ("experience_min_years",)

# 근무지. 목록에서 여러 개를 고르는 칸이라 판정 칸(하나를 고른다)과 따로 둔다 (`app/regions.py`)
REGION: Final = "region"

# 0043. 지원 접수·채용 문의 이메일. 원문의 주소를 그대로 옮기고, 원문에 있을 때만 남는다
EMAIL_FIELDS: tuple[str, ...] = ("application_email", "inquiry_email")

# 칸마다 저장하는 이름과 화면 이름. 모델이 고르는 칸과 정규화가 정하는 칸이 함께 있다
VALUE_LABELS: Final[dict[str, dict[str, str]]] = {
    "employment_type": EMPLOYMENT_TYPES,
    "experience_type": EXPERIENCE_TYPES,
    "education_level": EDUCATION_LEVELS,
    "closes_when_filled": CLOSES_WHEN_FILLED,
    "application_method": APPLICATION_METHODS,
    "recruitment_type": RECRUITMENT_TYPES,
    "auto_close_enabled": AUTO_CLOSE,
}

# 수집이 채우는 여섯 칸 중, 원문을 읽어 다른 값을 낼 수 있는 셋. `title` 은 이미 `position_name` 의
# 출처로 프롬프트에 그대로 들어가 있어 다시 비교할 이유가 없고, `body` 는 모델에게 보내는
# 원문 그 자체라 비교할 대상이 없다. `source_url` 은 공고의 신원이라 애초에 후보가 아니다.
#
# `recruitment_end_at` 은 마감 지난 공고를 거르는 데 쓰이고 `company_name` 는 계열사를 가르는
# 값이라, 이 셋은 값이 있으면 아무리 근거가 있어도 자동으로 덮지 않고 제안으로만 낸다
# (`.claude/tasks/todo/prd-side-workflows.md` 6절).
COLLECTED_REVIEW_FIELDS: tuple[str, ...] = (
    "company_name",
    "recruitment_end_at",
    "recruitment_start_at",
)

# 화면에 보일 이름. 프롬프트에 값을 적을 때도 같은 이름을 쓴다
COLLECTED_REVIEW_LABELS: dict[str, str] = {
    "company_name": "회사명",
    "recruitment_end_at": "마감일",
    "recruitment_start_at": "모집 시작일",
}


def suggestion_field(name: str) -> str:
    """그 칸의 제안 값이 담기는 응답 필드 이름."""
    return f"{name}_suggestion"


def suggestion_reason_field(name: str) -> str:
    """그 칸의 제안 이유가 담기는 응답 필드 이름."""
    return f"{name}_suggestion_reason"


# 판정 칸과 숫자 칸마다 따라오는 근거 문장. 컬럼이 아니라 검증과 보고를 위한 값이다
EVIDENCE_FIELDS: tuple[str, ...] = tuple(
    f"{name}_evidence" for name in (*JUDGE_FIELDS, *NUMBER_FIELDS, REGION)
)

# 원문에 있는 글자를 그대로 가져오는 칸. `position_name` 은 제목에서, 나머지는 본문에서 온다
EXTRACT_FIELDS: tuple[str, ...] = (
    "position_name",
    "responsibilities",
    "preferred_qualifications",
    "hiring_process",
    "qualifications",
    "recruitment_notice",
    "company_and_team_introduction",
    "compensation",
    "benefits",
    "recruitment_headcount",
)

# 수집이 사이트에서 읽는 칸인데, 못 읽었으면 AI 가 줄 번호로 짚은 부분으로 채운다
# (2026-09-17 결정). 사이트마다 셀렉터로 회사·모집 기간을 잡는 것은 키워드 찾기라 자주 빈다 —
# 공고를 읽는 AI 가 뜻으로 찾는다. 긴 분류 응답 안에서 물으면 DeepSeek 가 거의 짚지 않아 세 칸만
# 따로 묻는다 (`app/classify/basics.py`). 사이트에서 읽은 값이 있으면 그 값이 먼저다
# (`app/normalize/engine.py` 의 `fill_fallbacks`). 날짜 두 칸은 원문 글자라 정규화가 날짜로 읽는다
FALLBACK_FIELDS: tuple[str, ...] = ("company_name", "recruitment_start_at", "recruitment_end_at")

# 분류가 채우는 칸. `normalized_jobs` 의 같은 이름 컬럼으로 간다
CLASSIFY_FIELDS: tuple[str, ...] = (
    *JUDGE_FIELDS,
    *NUMBER_FIELDS,
    REGION,
    *EMAIL_FIELDS,
    *EXTRACT_FIELDS,
)

# 응답 맨 위에 올 수 있는 이름 전부
RESPONSE_FIELDS: tuple[str, ...] = tuple(Classification.model_fields)
# 나눈 공고 하나에 올 수 있는 이름과, 공통 묶음에 올 수 있는 이름
POSTING_FIELDS: tuple[str, ...] = tuple(Posting.model_fields)
COMMON_FIELDS: tuple[str, ...] = tuple(CommonFields.model_fields)
# 제안 칸과, 긴 공고의 짜임 응답에 올 수 있는 이름
SUGGESTION_FIELDS: tuple[str, ...] = tuple(SuggestionFields.model_fields)
OUTLINE_FIELDS: tuple[str, ...] = tuple(Outline.model_fields)
# 응답 맨 위의 두 묶음. 나머지 맨 위 칸은 제안이다
COMMON: Final = "common"
POSTINGS: Final = "postings"
assert set(COMMON_FIELDS) == set(EXTRACT_FIELDS) - {"position_name"}

# 직무 분류. `job_taxonomy`(운영 DB 표)에서 고르는 판정 칸 둘이라 `Classification`(정적
# pydantic 모델)에도, 위 `CLASSIFY_FIELDS`/`RESPONSE_FIELDS`(둘 다 그 정적 모델에서 뽑는다)
# 에도 없다 — 목록이 배포 없이 바뀌어야 해서 호출 시점에 `build_classification_model()` 이
# 이 두 칸을 가진 모델을 새로 만든다. `CLASSIFY_FIELDS` 를 그대로 넓히지 않는 이유는
# "이 칸들은 전부 정적 모델의 필드다" 를 지키는 불변식이기 때문이다 (`tests/test_classify_body.py`)
JOB_FIELD: Final = "job_field"
JOB_ROLE: Final = "job_role"
TAXONOMY_FIELDS: tuple[str, ...] = (JOB_FIELD, JOB_ROLE)

# AI 가 지은 공고 제목 (`migrations/0041_classification_posting_title.sql`). `normalized_jobs` 에는
# 이 이름의 칸이 없고 정규화가 `title` 로 옮긴다 — 그래서 `STORED_CLASSIFY_FIELDS` 에 넣지 않는다
POSTING_TITLE: Final = "posting_title"

# 산업. `industries`(운영 DB 표)에서 공고마다 고르는 판정 칸이다 (`migrations/0034_industries.sql`).
# 직무 분류와 같은 이유로 정적 모델에 없고 `build_classification_model()` 이 더한다
INDUSTRY: Final = "industry"

# 저장 경로(분류 결과 표, 정규화)가 옮기는 칸 전부. `CLASSIFY_FIELDS` 에 직무 분류 둘을 더한
# 것이다 — `job_classifications`/`normalized_jobs` 양쪽 다 이 두 칸의 컬럼을 갖는다
# (`migrations/0025_job_major_minor.sql`)
STORED_CLASSIFY_FIELDS: tuple[str, ...] = (
    *CLASSIFY_FIELDS,
    *TAXONOMY_FIELDS,
    INDUSTRY,
    *FALLBACK_FIELDS,
)


def _choices(name: str) -> tuple[str, ...]:
    annotation = Posting.model_fields[name].annotation
    return tuple(get_args(annotation))


# 판정 칸이 고를 수 있는 값. 모델에 보내는 목록과 받은 뒤 거르는 목록이 같아야 해서 스키마
# 하나에서 뽑는다 — 두 벌을 두면 목록을 넓힐 때 한쪽만 넓어진다
JUDGE_CHOICES: dict[str, tuple[str, ...]] = {name: _choices(name) for name in JUDGE_FIELDS}

# 스키마에 적은 글자와 화면 이름 표가 갈리면 저장된 값이 화면에서 이름 없이 나온다. 임포트
# 시점에 걸린다 — 640건을 돌린 뒤에 알게 될 일이 아니다
for _name in JUDGE_FIELDS:
    assert set(JUDGE_CHOICES[_name]) == set(VALUE_LABELS[_name]), _name


def build_classification_model(conn: sqlite3.Connection) -> type[Classification]:
    """`job_taxonomy` 의 켜진 값으로 `job_field`/`job_role` 를 더한 모델을 만든다.

    두 칸은 공고마다 고르는 칸이라 공고 모델(`Posting`)에 더하고, 그 공고 모델을 담는 응답
    모델을 돌려준다. 다른 판정 칸처럼 기본값이 없어 반드시 하나를 고른다 (2026-09-14 결정).

    `Classification` 은 고치지 않는다 — 그 클래스는 배포 시점에 고정된 칸의 모양이고,
    직무 분류는 운영 중에 표가 바뀌면 다음 호출부터 목록이 따라와야 한다. 그래서 매 호출
    시점에 이 함수로 새 모델을 만든다.

    켜진 산업이 있으면 `industry` 도 같은 방법으로 더한다 (`migrations/0034_industries.sql`).

    **켜진 대분류도 켜진 산업도 없으면(표가 비었거나 전부 껐으면) 직무 분류와 산업 칸은 더하지
    않는다.** 고를 것이 없는 판정 칸을 모델에 보내면 그 자리를 채우라고 강요하는 것과 같다.
    대분류는 있는데 켜진 소분류가 하나도 없으면 `job_role` 없이 `job_field` 만 더한다.

    근무지(`region`)는 표와 상관없이 늘 큰 지역 목록을 enum 으로 건다 (`app/regions.py`).
    """
    majors = taxonomy.list_majors(conn, enabled_only=True)
    industry_names = industries.enabled_names(conn)

    # 근무지는 표와 상관없이 늘 목록에서 고른다. 목록이 코드 밖(씨앗 파일)에 있어 여기서 건다
    region_names = regions.names()
    region_type: Any = list[Literal[region_names]]  # type: ignore[valid-type]
    fields: dict[str, Any] = {REGION: (region_type, Field(default_factory=list))}
    if majors:
        major_names = tuple(major.name for major in majors)
        minor_names = tuple(
            minor.name
            for major in majors
            for minor in taxonomy.list_minors(conn, major.id, enabled_only=True)
        )
        fields[JOB_FIELD] = (Literal[major_names], ...)
        fields[f"{JOB_FIELD}_evidence"] = (str, "")
        if minor_names:
            fields[JOB_ROLE] = (Literal[minor_names], ...)
            fields[f"{JOB_ROLE}_evidence"] = (str, "")
    # 0034. 켜진 산업이 있으면 공고마다 그중 하나를 고른다
    if industry_names:
        fields[INDUSTRY] = (Literal[industry_names], ...)
        fields[f"{INDUSTRY}_evidence"] = (str, "")

    posting: Any = create_model("PostingWithTaxonomy", __base__=Posting, **fields)
    return create_model(
        "ClassificationWithTaxonomy",
        __base__=Classification,
        postings=(list[posting], Field(default_factory=list)),
    )


def posting_model_of(response_model: type[Classification]) -> type[Posting]:
    """응답 모델이 담는 공고 모델. 직무 분류가 있으면 그 두 칸을 더한 모델이다."""
    (item,) = get_args(response_model.model_fields[POSTINGS].annotation)
    return cast(type[Posting], item)


class ClassifySchemaError(ValueError):
    """검증 실패. `reason` 은 위 표의 값 중 하나다."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ParsedPosting:
    """나눈 공고 하나. 글자 칸(판정·근거·직무 분류)과 뽑는 칸의 조각을 따로 담는다."""

    fields: dict[str, str]
    pieces: dict[str, list[Piece]]


@dataclass(frozen=True)
class ParsedClassification:
    """응답 하나. 맨 위의 제안 칸, 공통 조각, 나눈 공고들."""

    fields: dict[str, str]
    common: dict[str, list[Piece]]
    postings: list[ParsedPosting]


def validate_classification(
    data: Any, response_model: type[Classification] = Classification
) -> ParsedClassification:
    """파싱된 응답을 검증한다. 없는 키는 빈 값이다.

    스키마에 없는 칸 이름이 오면 무엇을 말하려던 것인지 추측해서 고치지 않는다. 조용히 고친
    값은 나중에 왜 그 칸에 그 값이 들어갔는지 아무도 설명하지 못한다. 맨 위든 `common` 안이든
    공고 안이든 같다.

    예외는 **이름은 스키마에 있고 자리만 틀린 칸**이다. posting 에만 오는 칸(`position_name`,
    판정 칸, 직무 분류, 산업)이 `common` 이나 맨 위에 오면 각 공고로 옮겨 받는다 — 짐작할 뜻이
    없다 (`_split_misplaced`, 2026-09-18).

    `response_model` 은 기본값이 정적 모델(`Classification`)이지만, 직무 분류가 있는 호출은
    `build_classification_model()` 이 만든 모델을 넘긴다 — 그 두 칸은 호출마다 있을 수도 없을
    수도 있어 고정 튜플에 넣을 수 없다(`TAXONOMY_FIELDS` 설명).

    판정 칸의 값이 목록 밖이면 여기서 버리지 않고 그대로 넘긴다. 무엇을 왜 버렸는지 한자리에서
    세려고 판정은 `app/classify/grounding.py` 가 한다.
    """
    if not isinstance(data, Mapping):
        raise ClassifySchemaError("unparsable", f"응답이 객체가 아니다: {type(data).__name__}")
    response_fields = tuple(response_model.model_fields)
    posting_fields = tuple(posting_model_of(response_model).model_fields)
    # posting 에만 오는 글자 칸이 공통 자리(common, 응답 맨 위)에 왔으면 떼어 둔다
    posting_only = tuple(
        name
        for name in posting_fields
        if name not in EXTRACT_FIELDS and name not in response_fields
    )
    top_misplaced, data = _split_misplaced(data, posting_only)
    _reject_unknown(data, response_fields, "")

    common_misplaced, common_raw = _split_misplaced(_object(COMMON, data.get(COMMON)), posting_only)
    misplaced = {**top_misplaced, **common_misplaced}
    # 뽑는 칸 중 posting 에만 오는 것(`position_name`)도 common 에 오면 조각째 옮긴다
    misplaced_pieces = {
        name: _pieces(f"{COMMON}.{name}", common_raw.pop(name))
        for name in [
            key for key in common_raw if key in EXTRACT_FIELDS and key not in COMMON_FIELDS
        ]
    }
    _reject_unknown(common_raw, COMMON_FIELDS, f"{COMMON} ")
    common = {name: _pieces(f"{COMMON}.{name}", common_raw.get(name)) for name in COMMON_FIELDS}

    postings_raw = data.get(POSTINGS)
    if postings_raw is None or postings_raw == "":
        postings_raw = []
    if not isinstance(postings_raw, list):
        raise ClassifySchemaError(
            "unparsable", f"`{POSTINGS}` 가 목록이 아니다: {type(postings_raw).__name__}"
        )
    postings: list[ParsedPosting] = []
    for index, raw in enumerate(postings_raw):
        where = f"{POSTINGS}[{index}]"
        if not isinstance(raw, Mapping):
            raise ClassifySchemaError(
                "unparsable", f"`{where}` 가 객체가 아니다: {type(raw).__name__}"
            )
        _reject_unknown(raw, posting_fields, f"{where} ")
        fields: dict[str, str] = {}
        pieces: dict[str, list[Piece]] = {}
        for name in posting_fields:
            if name in EXTRACT_FIELDS:
                pieces[name] = _pieces(f"{where}.{name}", raw.get(name))
            else:
                fields[name] = _text(f"{where}.{name}", raw.get(name))
        postings.append(ParsedPosting(fields=fields, pieces=pieces))

    if misplaced or any(misplaced_pieces.values()):
        # 공고가 하나도 없으면 옮겨 붙일 곳이 없다. 빈 공고 하나를 만들어 거기 담는다 — 나중에
        # 빈 공고 하나를 만드는 자리(`app/classify/classifier.py` 의 `_result_of`)와 같은 모양이다
        if not postings:
            postings.append(ParsedPosting(fields={name: "" for name in posting_only}, pieces={}))
        # 공고 안에 값이 있으면 그 값이 먼저다. 직무마다 다를 수 있는 칸이다
        for posting in postings:
            for name, value in misplaced.items():
                if not posting.fields.get(name, ""):
                    posting.fields[name] = value
            for name, found in misplaced_pieces.items():
                if found and not posting.pieces.get(name):
                    posting.pieces[name] = list(found)

    top = {
        name: _text(name, data.get(name))
        for name in response_fields
        if name not in (COMMON, POSTINGS)
    }
    return ParsedClassification(fields=top, common=common, postings=postings)


def _split_misplaced(
    data: Mapping[str, Any], posting_only: tuple[str, ...]
) -> tuple[dict[str, str], dict[str, Any]]:
    """공통 자리에 온 posting 전용 칸을 떼어 낸다. (떼어 낸 값, 나머지) 를 돌려준다.

    DeepSeek 는 스키마를 강제해도 판정 칸·직무 분류·산업을 common 에 넣는다. 거절하고 다시
    물어도 두 번 다 그러면 분류가 통째로 저장되지 않아 산업·직군·직무가 빈다 (2026-09-18 실측,
    픽스처 6건 중 2건). 칸 이름은 맞고 자리만 틀린 것이라 뜻을 짐작할 것이 없다 — 각 공고로 옮겨
    받는다. 스키마에 없는 이름(`other`, `org_name`)은 여기서 떼지 않아 그대로 거절된다.
    """
    moved = {str(key): _text(str(key), value) for key, value in data.items() if key in posting_only}
    rest = {key: value for key, value in data.items() if key not in posting_only}
    return {name: value for name, value in moved.items() if value}, rest


def _reject_unknown(data: Mapping[str, Any], allowed: tuple[str, ...], where: str) -> None:
    # 뽑는 칸에 근거 문장(`position_name_evidence`)을 덧붙이는 것은 모델이 칸을 지어낸 것이 아니라
    # 쓸데없이 더 적은 것이다. 읽지 않고 넘긴다 — DeepSeek 가 자주 그런다 (2026-09-17)
    unknown = sorted(
        str(key) for key in data if key not in allowed and not str(key).endswith("_evidence")
    )
    if unknown:
        raise ClassifySchemaError(
            "unknown_field", f"{where}스키마에 없는 칸이 있다: {', '.join(unknown)}"
        )


def _object(name: str, raw: Any) -> Mapping[str, Any]:
    """묶음 하나. 빈 문자열과 None 은 빈 묶음으로 읽는다."""
    if raw is None or raw == "":
        return {}
    if not isinstance(raw, Mapping):
        raise ClassifySchemaError("unparsable", f"`{name}` 이 객체가 아니다: {type(raw).__name__}")
    return raw


def _text(name: str, raw: Any) -> str:
    """글자 칸 하나. 참·거짓과 정수는 글자로 읽는다.

    스키마는 채용 시 마감을 `"true"`/`"false"` 글자로, 경력 연수를 숫자 글자로 적게 하지만, 모델이
    JSON 의 참·거짓이나 숫자로 답해도 뜻은 같다. 그 한 가지 때문에 공고를 다시 묻지 않는다.
    """
    if raw is None:
        return ""
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, list):
        # 여러 개를 고르는 칸(근무지)이다. 예전처럼 조각(`{"line", "text"}`)으로 와도 그 글자를
        # 쓴다. 목록 밖 값은 근거 검사가 거른다 (`app/classify/grounding.py`)
        items = [item.get("text", "") if isinstance(item, Mapping) else item for item in raw]
        return regions.SEPARATOR.join(_text(name, item) for item in items if item not in (None, ""))
    if not isinstance(raw, str):
        raise ClassifySchemaError(
            "unparsable", f"`{name}` 이 문자열이 아니다: {type(raw).__name__}"
        )
    return raw.strip()


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


@dataclass(frozen=True)
class ParsedOutline:
    """긴 공고의 짜임. 줄 번호는 범위를 풀어 정렬한 것이다."""

    fields: dict[str, str]
    roles: list[list[int]]
    common: list[int]


def parse_outline(text: str, line_count: int) -> ParsedOutline:
    """짜임 응답을 파싱하고 검증한다. 글에 없는 줄 번호는 버린다.

    0 번(제목)은 남기지 않는다 — 직무마다 보낼 때 늘 붙는다. 줄이 하나도 남지 않은 직무는
    버린다. 번호가 틀린 범위 하나 때문에 공고 전체를 실패로 만들지 않는다.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClassifySchemaError("unparsable", f"JSON 으로 읽을 수 없다: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ClassifySchemaError("unparsable", f"응답이 객체가 아니다: {type(data).__name__}")
    _reject_unknown(data, OUTLINE_FIELDS, "")

    roles_raw = data.get("roles")
    if roles_raw is None or roles_raw == "":
        roles_raw = []
    if not isinstance(roles_raw, list):
        raise ClassifySchemaError(
            "unparsable", f"`roles` 가 목록이 아니다: {type(roles_raw).__name__}"
        )
    roles: list[list[int]] = []
    for index, raw in enumerate(roles_raw):
        where = f"roles[{index}]"
        role = _object(where, raw)
        _reject_unknown(role, ("lines",), f"{where} ")
        numbers = _line_numbers(f"{where}.lines", role.get("lines"), line_count)
        if numbers:
            roles.append(numbers)

    common = _line_numbers("common_lines", data.get("common_lines"), line_count)
    fields = {name: _text(name, data.get(name)) for name in SUGGESTION_FIELDS}
    return ParsedOutline(fields=fields, roles=roles, common=common)


def _line_numbers(name: str, raw: Any, line_count: int) -> list[int]:
    """줄 범위 목록을 줄 번호로 푼다. 1 번부터 `line_count - 1` 번까지만 남는다."""
    if raw is None or raw == "":
        return []
    if not isinstance(raw, list):
        raise ClassifySchemaError("unparsable", f"`{name}` 이 목록이 아니다: {type(raw).__name__}")
    numbers: set[int] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise ClassifySchemaError(
                "unparsable", f"`{name}` 의 범위가 객체가 아니다: {type(item).__name__}"
            )
        bounds = (item.get("start"), item.get("end"))
        for value in bounds:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ClassifySchemaError(
                    "unparsable", f"`{name}` 의 범위에 줄 번호가 없다: {value!r}"
                )
        low, high = sorted(cast(tuple[int, int], bounds))
        numbers.update(range(max(low, 1), min(high, line_count - 1) + 1))
    return sorted(numbers)


def parse_classification(
    text: str, response_model: type[Classification] = Classification
) -> ParsedClassification:
    """모델 응답 문자열을 파싱하고 검증한다."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClassifySchemaError("unparsable", f"JSON 으로 읽을 수 없다: {exc}") from exc
    return validate_classification(data, response_model)
