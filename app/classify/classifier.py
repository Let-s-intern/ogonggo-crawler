"""공고 하나를 아홉 칸으로 나눈다.

수집은 어느 사이트나 확실히 주는 여섯 칸만 한다. 나머지를 나누는 것은 공고를 읽는 이쪽 일이다
(`.claude/tasks/memos/보류/llm-classify/prd-llm-classify.md`).

## 칸이 두 가지다

**뽑는 칸** 일곱은 원문 글자를 그대로 옮긴다. 옮긴 값은 원문에서 그대로 찾을 수 있어야 한다.
여섯은 본문에서 오고 `job_role` 하나만 **제목**에서 온다 — 열한 사이트에서 제목이 직무를
말하는 곳이 아홉이고 그중 본문이 같은 글자를 되풀이하는 곳은 셋뿐이었다
(`tests/test_job_role_source.py`).

**판정하는 칸** 둘(고용형태·경력 구분)은 본문을 읽고 닫힌 목록에서 고른다. 목록은
응답 스키마의 enum 으로 강제하고, 고른 값에는 근거 문장이 따라온다. 그 문장이 본문에 없으면
판정을 버린다 — 읽고 고른 것인지 지어낸 것인지 가를 방법이 그것뿐이다.

어느 칸이 어느 쪽인지와 목록이 왜 그 목록인지는 `app/classify/schema.py` 에 있다.

## 지키는 것

**근거가 없는 것은 빈 칸이다.** 모델이 그럴듯하게 채우면 소비 측이 그것을 사실로 노출한다.
받은 값은 `app/classify/grounding.py` 가 그 자리에서 제목과 본문에 돌려 보고, 근거를 못 찾은
칸은 버리고 `dropped` 에 이름을 남긴다 (`.claude/rules/llm.md`).

**보내는 것은 제목과 상세 원문뿐이고 상한이 있다.** 원본 HTML 도 페이지도 보내지 않는다.
원문이 없는 건은 본문으로 떨어진다 (`app/classify/store.py`). 상한을 넘으면 잘라 보내고 그
사실을 `notes` 에 남긴다. 자른 것으로 무엇을 놓쳤는지는 응답을 보는 사람이 알아야 한다.
제목은 자르지 않는다 — 한 줄이고, 그 한 줄이 `job_role` 의 출처다.

**깨진 응답만 1회 다시 묻는다.** 스키마에 없는 칸을 지어낸 응답은 다시 물어도 같은 답이 온다.

**이미 값이 있는 칸은 채우지 않고 제안한다.** 수집이 채우는 여섯 칸 중 `company`·`deadline`·
`start_date` 셋은 값이 있으면 아무리 근거가 있어도 그 자리에서 덮지 않는다 — `deadline` 은
마감 지난 공고를 거르고 `company` 는 계열사를 가르는 값이라 모델 판단 하나로 바뀌면 안 된다
(`.claude/tasks/todo/prd-side-workflows.md` 6절). 대신 `ClassificationResult.suggestions` 로
나가고, 저장은 `job_field_suggestions` 하나뿐이다 — 사람이 검수 화면에서 수락해야 값이
바뀐다.

호출 자체와 비용 기록은 고른 제공자 항목이 한다 (`app/llm/`). 셀렉터 생성과 같은 경로이고,
**이 파일은 어느 제공자인지 모른다** — 그 선택은 설정이 정한다 (`app/llm/providers.py`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.classify.grounding import NOT_IN_SOURCE, ground, loose, missing_lines
from app.classify.pieces import Resolved, number_lines, render, resolve, strip_line_marks
from app.classify.schema import (
    CLASSIFY_FIELDS,
    COLLECTED_REVIEW_FIELDS,
    COLLECTED_REVIEW_LABELS,
    EXTRACT_FIELDS,
    JOB_MAJOR,
    JOB_MINOR,
    JUDGE_CHOICES,
    UNDECIDED,
    Classification,
    ClassifySchemaError,
    ParsedPosting,
    Piece,
    parse_classification,
    suggestion_field,
    suggestion_reason_field,
)
from app.config import Settings, get_settings
from app.llm.base import LlmCallError, Provider, Usage
from app.llm.log import CLASSIFY
from app.llm.providers import for_feature

logger = logging.getLogger(__name__)

# `llm_calls.feature` 와 로그에 같이 쓰는 이름
FEATURE = "classify"

# 깨진 응답에 한해 한 번 더. 2회를 넘기지 않는다 (`.claude/rules/llm.md`).
MAX_ATTEMPTS = 2

# 한 번에 보내는 글의 상한. 상한이 없으면 사이트 하나가 페이지 전체를 담기 시작한 날 그것이
# 그대로 나간다.
#
# 보내는 값이 본문에서 상세 원문으로 바뀌어(Push 9) 2026-08-28 에 다시 쟀고, **그대로 둔다.**
# 열한 픽스처에서 원문이 가장 긴 곳이 토스 10,312자이고 그다음이 네이버 3,872자다. 일곱 곳
# 전부 지금 상한 안이라 원문 때문에 잘리는 건이 없다 — 올릴 근거가 측정에 없다
# (`.claude/site-recipes/source-text-container.md`).
#
# 상한을 넘는 것은 LG 다. 상세 API 응답 전체를 편 원문이 33,225자이고(2026-09-11), 삼성은
# 9,791자로 상한 안이다. 그 하나를 위해 상한을 세 배로 올리지 않는다. 지금은 잘리고 잘린
# 사실이 `notes` 에 남는다
MAX_BODY_CHARS = 12000

_SYSTEM_INSTRUCTION = (
    "너는 채용공고를 정해진 칸으로 나눈다. 공고가 여러 직무를 뽑으면 직무마다 공고 하나로 "
    "나눈다. 제목과 본문의 줄마다 앞에 [번호] 가 붙어 있다. "
    "뽑는 칸은 그 내용이 몇 번 줄의 어느 부분인지를 조각으로 답하고, 조각의 글자는 그 줄에 "
    "적힌 그대로 옮긴다. 판정하는 칸은 본문을 읽고 주어진 목록에서 고른 뒤 "
    "그렇게 고른 근거 문장을 본문에서 그대로 옮겨 적는다. "
    "요약하지 않고, 다듬지 않고, 줄이지 않고, 없는 것을 지어내지 않는다."
)

_PROMPT = """아래는 채용공고의 제목과 본문이다. 줄마다 앞에 [번호] 가 붙어 있다.
[0] 이 제목이고 [1] 부터가 본문이다. 이것을 정해진 칸으로 나눈다.

# 공고 나누기 — postings 와 common

- 이 공고가 뽑는 직무(모집 분야)마다 postings 에 하나씩 낸다. 직무가 하나면 postings 는
  하나다. 직무 아래에 더 작은 직무가 나뉘어 있으면 가장 작은 직무마다 하나다.
- 모든 직무에 똑같이 해당하는 내용(공통 자격요건, 복지, 전형 절차, 회사 소개 등)은 common 에
  한 번만 담는다. 공고마다 common 이 붙으므로 같은 내용을 postings 에 되풀이하지 않는다.
- 한 직무에만 해당하는 내용은 그 직무의 posting 에 담는다.
- job_role 과 판정하는 칸은 posting 마다 그 직무를 보고 답한다. common 에는 없다.
- 직무가 하나인 공고는 전부 그 posting 에 담고 common 을 비워 둬도 된다.

# 뽑는 칸 — 어느 줄의 어느 부분인지를 조각으로 답한다

- job_role: 직무. **직무가 하나인 공고는 제목([0])에서만 가져온다.** 그 공고가 어떤 일을
  할 사람을 뽑는지 제목이 말하는 부분이다. 회사명·연도·`경력사원 채용`·`영입` 같은 말은
  빼고 직무를 가리키는 부분만 남긴다. 제목이 직무를 말하지 않으면(`전 직군 채용`,
  `신입사원 채용`) 빈 목록으로 둔다. **직무마다 나눈 공고는 본문에서 그 직무의 이름이 적힌
  줄에서 가져온다**(`[Finance]` 이면 `Finance`)
- duties: 주요 업무·담당 업무
- requirements: 자격요건·지원자격
- preferred: 우대사항
- hiring_process: 전형 절차
- work_location: 근무지
- company_and_team_introduction: 회사·팀 소개. **공고에 `회사 소개`·`팀 소개`·`회사 및
  팀 소개` 같은 소제목으로 된 구역이 있을 때만** 그 구역의 내용을 가져온다. 그런 구역이
  없으면 빈 목록으로 둔다. 다른 곳에 흩어진 회사 소개 문장은 모아 오지 않는다
- compensation: 급여·처우·연봉
- benefits: 복지·혜택
- recruitment_headcount: 모집 인원. 적힌 그대로 옮긴다(`0명`, `O명`, `00명` 도 그대로)
- etc_info: 위 어디에도 맞지 않는, **이 공고만의** 안내(전형 유의사항, 제출 서류, 보훈·장애인
  우대 문구 등)

답하는 모양:
- 칸마다 조각 목록으로 답한다. 조각 하나는 {{"line": 줄 번호, "text": 그 줄에서 이 칸에
  해당하는 부분}} 이다. 원문에 없는 칸은 빈 목록([])으로 둔다. 짐작해서 채우지 않는다.
- **text 는 그 줄에 적힌 글자 그대로 옮긴다.** 단어를 바꾸거나, 요약하거나, 줄이거나,
  오타를 고치지 않는다. 줄 앞의 [번호] 는 text 에 넣지 않는다.
- 공고의 소제목이 칸 이름과 달라도 된다. `지원자격`·`필수요건`·`이런 분을 찾아요` 아래
  내용은 requirements 다. 소제목 자체는 조각에 넣지 않는다.
- 한 줄에 소제목과 내용이 같이 있으면(`주요업무 : 결제 서버 개발`) 내용 부분만 옮긴다
  (`결제 서버 개발`).
- 한 줄에 여러 칸이 섞여 있으면(`근무지: 성남 | 고용형태: 정규직`) 그 칸에 해당하는
  부분만 옮긴다(근무지라면 `성남`).
- 줄이 영어 키 이름으로 시작하면(`ruWorkpl: 본사(서울 63빌딩)`) 그 이름은 무엇에 대한
  값인지 알려 주는 표시다. 키 이름은 옮기지 않고 값 부분만 옮긴다(`본사(서울 63빌딩)`).
- 내용이 여러 줄이면 줄마다 조각을 하나씩 낸다. 본문 여러 곳에 흩어져 있으면 그 줄들을
  모두 조각으로 낸다.
- 어느 칸에도 맞지 않는 내용만 etc_info 에 모은다. 본문 전체를 etc_info 에 넣지 않는다.
- **슬로건·화면 UI 문구는 어느 칸에도 옮기지 않는다.** "간편하면서도 안전한 금융을
  만든다" 같은 한 줄 슬로건, "N개 계열사·N개의 포지션이 열려 있어요"·"1개 포지션" 같은
  화면 카운트 문구는 이 공고 하나만 말하는 정보가 아니다. 회사·팀 소개는 소제목 구역이
  있을 때 company_and_team_introduction 에만 담고, 다른 칸에는 옮기지 않는다. 공고 자체에
  대한 안내만 etc_info 에 담는다.

# 판정하는 칸 — 본문을 읽고 목록에서 고른다

- employment_type: 고용형태. {employment_type}
- career_level: 경력 구분. {career_level}
- education_level: 요구 학력. {education_level}

규칙:
- **이 칸들은 글자가 본문에 그대로 없어도 된다.** 본문을 읽고 판단해서 고른다. `채용 후
  정규직 전환` 이면 employment_type 은 인턴이다. `5년 이상 경험` 이면 career_level 은
  경력이다.
- education_level 은 지원 자격이 요구하는 **최소 학력**을 고른다. `학사 이상`·`대졸`
  이면 학사, `고졸 이상` 이면 고졸, `학력 무관` 이면 무관이다. 우대사항에만 있는
  학력(`석사 우대`)은 요구 조건이 아니라 고르지 않는다. 학력을 말하지 않으면 판단불가 다.
- **반드시 위 목록에 있는 값만 쓴다.** 목록에 없는 값을 새로 만들지 않는다. 어디에도 맞지
  않으면 목록의 기타를, 본문만으로는 판단할 수 없으면 판단불가 를 쓴다.
- 고른 칸마다 `employment_type_evidence` 처럼 `_evidence` 가 붙은 자리에 **그렇게 판단한
  근거가 되는 본문 문장을 그대로 옮겨 적는다.** 한 문장이면 된다. 본문에 없는 문장을 적지
  않는다. 줄 앞의 [번호] 는 넣지 않는다. 근거를 적을 수 없으면 그 칸을 판단불가 로 둔다.
- 회사명·모집 시작일·마감일은 위 칸 어디에도 넣지 않는다. 그 셋을 원문과 견주는 자리는
  값이 이미 있을 때만 아래에 따로 나온다. 제목도 `job_role` 말고는 어느 칸에도 넣지 않는다.
{taxonomy_block}{current_values_block}
[제목]
{title}

[본문]
{body}
"""


class ClassifyError(RuntimeError):
    """분류 실패. `reason` 으로 무엇을 해야 할지가 갈린다.

    | reason | 다음 행동 |
    |---|---|
    | `no_api_key` | 환경변수를 채운다. 서버 문제가 아니다 |
    | `api_error` | Gemini 응답 자체가 실패했다. 잠시 뒤 다시 |
    | `unparsable` | 1회 재요청까지 하고도 JSON 이 아니었다 |
    | `unknown_field` | 모델이 스키마에 없는 칸을 냈다 |
    | `empty_body` | 나눌 본문이 없다. 모델을 부르지 않는다 |

    어느 것도 수집을 실패로 만들지 않는다. 본문은 `raw_jobs` 에 그대로 있고 나중에 다시
    돌릴 수 있다 (`.claude/tasks/memos/보류/llm-classify/prd-llm-classify.md`).
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class PostingResult:
    """나눈 공고 하나의 결과.

    `fields` 는 분류가 채우는 칸 전부를 갖는다. 채우지 못한 칸은 빈 문자열이고, 근거를 찾지
    못해 버린 칸도 빈 문자열이다 — 버린 칸의 이름은 `dropped` 에, 이유는 `reasons` 에 있다.
    """

    fields: dict[str, str]
    # 살아남은 판정 칸의 근거 문장. 사람이 그 판정을 읽고 검사할 수 있는 유일한 자리다
    evidence: dict[str, str] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)
    # 버린 칸마다 왜 버렸는지. 고칠 자리가 이유마다 다르다
    reasons: dict[str, str] = field(default_factory=dict)

    @property
    def filled(self) -> list[str]:
        """값이 들어간 칸 이름."""
        return [name for name in CLASSIFY_FIELDS if self.fields.get(name, "").strip()]


@dataclass(frozen=True)
class ClassificationResult:
    """분류 한 번의 결과. 공고 한 건이 직무마다 나뉘면 `postings` 가 여럿이다.

    직무가 하나인 공고는 `postings` 가 하나다. 제안은 공고 한 건 전체에 대한 것이라 여기 한
    번만 있다.
    """

    postings: list[PostingResult]
    usage: Usage
    attempts: int
    notes: list[str] = field(default_factory=list)
    # 이미 값이 있는 칸(`company`·`deadline`·`start_date`)에 원문이 다르다고 낸 값. 근거
    # 검사를 통과하고 지금 값과 실제로 다른 것만 남는다 — 저장은 `job_field_suggestions` 뿐이고
    # 여기 값이 `normalized_jobs` 를 자동으로 덮는 경로는 없다
    suggestions: dict[str, str] = field(default_factory=dict)
    suggestion_reasons: dict[str, str] = field(default_factory=dict)

    @property
    def split(self) -> bool:
        """직무마다 나뉘었는가. 공고가 하나면 나누지 않은 것이다."""
        return len(self.postings) > 1


def build_client(settings: Settings | None = None) -> Any:
    """분류가 고른 제공자의 클라이언트. API 키는 설정에서만 온다.

    **키가 없으면 여기서 선다.** 다른 제공자로 넘어가지 않는다 — 조용히 넘어가면 비용
    기록이 거짓말이 된다 (`.claude/rules/llm.md`).
    """
    resolved = settings or get_settings()
    try:
        provider, _ = for_feature(CLASSIFY, resolved)
        return provider.build_client(resolved)
    except LlmCallError as exc:
        raise ClassifyError(exc.reason, f"{exc}. 서버 문제가 아니라 분류만 막힌다") from exc


def chosen(settings: Settings) -> tuple[Provider, str]:
    """분류가 쓰는 제공자와 모델. 실패한 호출을 기록할 때도 모델 이름이 필요하다."""
    try:
        return for_feature(CLASSIFY, settings)
    except LlmCallError as exc:
        raise ClassifyError(exc.reason, str(exc)) from exc


def _current_values_block(current_values: Mapping[str, str]) -> str:
    """ "이미 있는 값" 구역. 값이 하나도 없으면 빈 문자열이라 프롬프트에 아무것도 남지 않는다.

    보낸 칸만 적는다. `COLLECTED_REVIEW_FIELDS` 셋 중 값이 없는 칸까지 나열하면 "빈 칸도
    비교 대상이다" 로 읽혀 모델이 근거 없이 값을 지어낼 자리가 생긴다.
    """
    present = {
        name: current_values[name].strip()
        for name in COLLECTED_REVIEW_FIELDS
        if current_values.get(name, "").strip()
    }
    if not present:
        return ""
    lines = "\n".join(
        f"- {COLLECTED_REVIEW_LABELS[name]} ({name}): {value}" for name, value in present.items()
    )
    return (
        "\n# 이미 있는 값 — 원문과 다르면 고쳐 제안한다\n\n"
        "아래 칸에는 이미 값이 있다. 제안 칸은 postings 안이 아니라 응답 맨 위에 둔다.\n"
        "원문을 읽고 같은 값이면 그 칸의 `_suggestion` 과\n"
        "`_suggestion_reason` 을 비워 둔다. 값이 다르면 `_suggestion` 에 원문이 말하는 값을,\n"
        "`_suggestion_reason` 에 왜 다른지 원문에 있는 근거를 한 줄로 적는다. 원문에 없는\n"
        "근거로 고치지 않는다 — 짐작이 아니라 읽고 판단해야 한다.\n\n"
        f"{lines}\n"
    )


def _taxonomy_block(tree: Sequence[tuple[str, tuple[str, ...]]]) -> str:
    """직무 분류 구역. 표가 비어 있으면(씨앗 전이거나 전부 껐으면) 빈 문자열이다.

    대분류·소분류를 두 단계로 나눠 묻지 않고 트리를 통째로 한 번에 보낸다(PRD
    `job-taxonomy` 2절 "한 번에 부른다") — 어느 소분류가 어느 대분류 밑인지 모델이 알아야
    엉뚱한 조합(다른 대분류의 소분류)을 고르지 않는다.
    """
    if not tree:
        return ""
    lines = "\n".join(
        f"- {major}: {', '.join(minors)}" if minors else f"- {major}" for major, minors in tree
    )
    return (
        "\n# 직무 분류 — 아래 목록에서만 고른다\n\n"
        "job_major 는 대분류, job_minor 는 그 대분류 밑의 소분류다. 목록에 없는 이름을\n"
        "새로 만들지 않는다. 직무마다 나눈 공고는 posting 마다 그 직무를 보고 고른다.\n\n"
        "**가능하면 항상 채운다.** 정확히 들어맞는 대분류가 없어도, 이 공고가 하는 일과\n"
        "가장 가까운 대분류를 고른다 — 완벽히 맞는 것을 찾는 것이 아니라 다른 후보보다\n"
        "조금이라도 더 가까운 것을 고르는 일이다. job_major 를 판단불가 로 두는 것은 본문에\n"
        "무슨 일을 하는 사람을 뽑는지 알 만한 내용이 전혀 없을 때뿐이다.\n\n"
        "대분류는 골랐는데 그 밑의 소분류 중 맞는 것이 없으면, 그 대분류 목록의 마지막에\n"
        "있는 `기타`로 시작하는 소분류(예: 기타IT·개발)를 고른다 — job_minor 를 판단불가 로\n"
        "두지 않는다. job_minor 는 반드시 그 job_major 줄에 적힌 소분류 중에서 고른다 —\n"
        "다른 대분류의 소분류를 고르지 않는다.\n\n"
        "고른 값마다 job_major_evidence / job_minor_evidence 에 그렇게 판단한 본문 근거\n"
        "문장을 그대로 옮겨 적는다. `기타` 소분류를 골랐을 때도 이 공고가 그 대분류의 일을\n"
        "한다고 볼 수 있는 본문 문장을 그대로 옮겨 적는다 — 근거 문장은 항상 원문에 있는\n"
        "그대로여야 하고, 소분류 이름 자체를 짐작해 지어내지 않는다.\n\n"
        f"{lines}\n"
    )


def build_prompt(
    body: str,
    title: str = "",
    current_values: Mapping[str, str] | None = None,
    taxonomy_tree: Sequence[tuple[str, tuple[str, ...]]] = (),
) -> tuple[str, list[str]]:
    """보낼 프롬프트와 남길 메모. 상한을 넘긴 글은 자르고 그 사실을 적는다.

    `body` 는 상세 원문이거나, 원문이 없는 건에서 본문이다 (`app/classify/store.py`).

    `title` 이 `job_role` 의 출처다. 제목을 보내지 않으면 그 칸은 영원히 빈다. 제목이 없는
    공고는 빈 줄이 들어가고, 모델은 옮길 것이 없어 빈 문자열을 낸다 — 수집이 제목을 못 뽑는
    것은 수집의 실패이지 여기서 메울 일이 아니다 (`app/crawler/parser.py`).

    판정 칸의 목록을 프롬프트에도 적는다. 스키마의 enum 이 이미 강제하지만, 무엇 중에서
    고르는지 모르는 채로 고르면 목록에서 가장 가까운 값이 아니라 첫 값이 나온다. 목록의
    출처는 스키마 하나다 — 여기에 손으로 적으면 두 목록이 갈린다.

    `current_values` 는 `company`·`deadline`·`start_date` 중 이미 채워진 값이다
    (`app/classify/store.py` 의 `read_current_values`). 무엇이 이미 채워져 있는지 모르면
    "원문과 다르다" 를 모델이 말할 수 없다 — 값이 없으면 그 칸은 프롬프트에 아예 나오지 않고,
    나오지 않은 칸을 모델이 지어내 제안하면 근거 검사가 버린다.

    `taxonomy_tree` 는 `app.taxonomy.enabled_tree()` 가 만든 (대분류, 소분류들) 목록이다.
    빈 목록이면(표가 비었거나 씨앗을 아직 안 넣었으면) 이 구역 자체가 프롬프트에 없다.
    """
    notes: list[str] = []
    text = body
    if len(text) > MAX_BODY_CHARS:
        notes.append(f"보낸 글이 {len(text)}자라 앞 {MAX_BODY_CHARS}자만 보냈다")
        text = text[:MAX_BODY_CHARS]
    # 번호는 자른 글에 붙인다. 자른 자리까지는 자르기 전 글과 줄이 같아서, 받은 번호를 자르기
    # 전 글에서 찾아도 같은 줄이다 (`classify_body`)
    lines = number_lines(title, text)
    choices = {name: " / ".join((*values, UNDECIDED)) for name, values in JUDGE_CHOICES.items()}
    block = _current_values_block(current_values or {})
    taxonomy = _taxonomy_block(taxonomy_tree)
    return (
        _PROMPT.format(
            body=render(lines[1:], start=1),
            title=render(lines[:1]),
            current_values_block=block,
            taxonomy_block=taxonomy,
            **choices,
        ),
        notes,
    )


def _extract_suggestions(
    fields: Mapping[str, str], current_values: Mapping[str, str], source: str
) -> tuple[dict[str, str], dict[str, str]]:
    """이미 값이 있는 칸의 제안만 추린다. 근거가 없거나 지금 값과 다르지 않으면 버린다.

    `source` 는 근거 검사가 도는 것과 같은 값이다 — 제목과 모델에게 보낸 그 글을 합친 것.
    """
    suggestions: dict[str, str] = {}
    reasons: dict[str, str] = {}
    for name in COLLECTED_REVIEW_FIELDS:
        current = current_values.get(name, "").strip()
        if not current:
            # 지금 값이 없으면 제안할 것도 없다. 이 셋은 채우기 대상이 아니다 — 값이 있을
            # 때만 "다르다" 를 말할 수 있다
            continue
        value = fields.get(suggestion_field(name), "").strip()
        if not value or loose(value) == loose(current):
            # 값이 없거나 지금 값과 같다. 모델이 지시를 지켰든 어겼든 바뀐 것이 없다
            continue
        if missing_lines(value, source):
            # 근거 검사를 제안에도 그대로 건다. 원문에서 찾지 못한 값은 제안이 되지 않는다
            # (`.claude/rules/llm.md`)
            continue
        suggestions[name] = value
        reasons[name] = fields.get(suggestion_reason_field(name), "").strip()
    return suggestions, reasons


def _piece_notes(resolved: Mapping[str, Resolved]) -> list[str]:
    """조각을 옮기며 생긴 일. 줄 전체로 대신한 조각과 찾지 못한 조각을 칸마다 센다.

    값이 통째로 빈 칸은 여기 적지 않는다 — 버린 칸으로 `dropped` 에 들어간다.
    """
    whole = [f"{name}({item.whole_lines})" for name, item in resolved.items() if item.whole_lines]
    partial = [
        f"{name}({item.lost})" for name, item in resolved.items() if item.lost and item.value
    ]
    notes: list[str] = []
    if whole:
        notes.append("글자가 달라 짚은 줄 전체를 남긴 칸: " + ", ".join(whole))
    if partial:
        notes.append("찾지 못한 조각을 뺀 칸: " + ", ".join(partial))
    return notes


async def classify_body(
    body: str,
    *,
    title: str = "",
    current_values: Mapping[str, str] | None = None,
    taxonomy_tree: Sequence[tuple[str, tuple[str, ...]]] = (),
    response_model: type[Classification] = Classification,
    settings: Settings | None = None,
    client: Any | None = None,
    on_call: Callable[[Usage], None] | None = None,
) -> ClassificationResult:
    """공고 하나를 나눈다. 받은 값은 원문에 있는지 확인한 뒤에만 남는다.

    직무가 여럿인 공고는 직무마다 `ClassificationResult.postings` 하나가 된다. 칸마다 공통
    조각을 그 공고의 조각 앞에 붙인 뒤 공고마다 근거를 확인한다 (`app/classify/schema.py`).

    `title` 은 `job_role` 의 출처다. 비어 있어도 나머지 여덟 칸은 그대로 나오므로 분류가
    실패하지 않는다 — 나눌 것이 없는 것은 본문이 빈 경우뿐이다.

    `current_values` 는 `company`·`deadline`·`start_date` 중 이미 채워진 값이다. 값이 있는
    칸에 원문이 다른 값을 말하면 `ClassificationResult.suggestions` 로 나가고, `fields` 의
    아홉 칸은 건드리지 않는다 — 이 셋은 애초에 `CLASSIFY_FIELDS` 에 없다.

    `taxonomy_tree` 와 `response_model` 은 함께 온다 — 부르는 쪽(`app/classify/batch.py`)이
    배치 시작 전에 `app.taxonomy.enabled_tree()` 와 `build_classification_model()` 로 한 번만
    만들어 공고마다 그대로 넘긴다. 공고마다 표를 다시 읽을 이유가 없다. 빈 트리(기본값)는
    "표가 비었다" 는 뜻이고, 그때 `response_model` 은 `Classification` 그대로다.

    `on_call` 은 모델을 부를 때마다 그 호출의 비용으로 불린다. 깨진 응답으로 한 번 더 물으면
    두 번 불린다 — 부르는 쪽이 그것을 `llm_calls` 에 그대로 남겨야 토큰 합이 실제와 맞는다
    (`app/llm/log.py`).
    """
    if not body.strip():
        raise ClassifyError("empty_body", "본문이 비어 있어 나눌 것이 없다")

    resolved = settings or get_settings()
    provider, model = chosen(resolved)
    resolved_client = client or build_client(resolved)
    prompt, notes = build_prompt(body, title, current_values, taxonomy_tree)

    taxonomy_choices: dict[str, tuple[str, ...]] | None = None
    if taxonomy_tree:
        taxonomy_choices = {JOB_MAJOR: tuple(major for major, _ in taxonomy_tree)}
        minors = tuple(minor for _, minor_list in taxonomy_tree for minor in minor_list)
        if minors:
            taxonomy_choices[JOB_MINOR] = minors

    last_error: ClassifySchemaError | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        text, usage = await _call(resolved_client, model, prompt, attempt, provider, response_model)
        if on_call is not None:
            on_call(usage)
        try:
            parsed = parse_classification(text, response_model)
        except ClassifySchemaError as exc:
            logger.warning(
                "분류 응답 거절 model=%s attempt=%d reason=%s message=%s",
                model,
                attempt,
                exc.reason,
                exc,
            )
            if exc.reason != "unparsable":
                # 모양이 아니라 내용의 문제다. 다시 물어도 같은 답이 온다
                raise ClassifyError(exc.reason, str(exc)) from exc
            last_error = exc
            continue

        # 자르기 전 글의 줄을 쓴다 — 번호는 모델이 본 글과 같고, 번호가 틀렸을 때 찾는 범위만
        # 넓어진다 (`app/classify/pieces.py`)
        lines = number_lines(title, body)
        # 공고를 하나도 내지 않았으면 공통 칸만으로 공고 하나를 만든다. 공통 칸도 비었으면 빈
        # 공고 하나이고, 그것은 지금까지 "아무것도 못 뽑았다" 던 결과와 같다
        postings = parsed.postings or [ParsedPosting(fields={}, pieces={})]
        split = len(postings) > 1
        results: list[PostingResult] = []
        posting_notes: list[str] = []
        for number, posting in enumerate(postings, start=1):
            result, found = _posting_result(
                posting, parsed.common, lines, body, title, taxonomy_choices, model
            )
            results.append(result)
            posting_notes.extend(f"{number}번 공고: {note}" if split else note for note in found)

        # 같은 응답에서 제안도 같이 추린다. 두 번째 호출을 만들면 토큰이 두 배다
        top = {name: strip_line_marks(value).strip() for name, value in parsed.fields.items()}
        suggestions, suggestion_reasons = _extract_suggestions(
            top, current_values or {}, f"{title}\n{body}"
        )
        return ClassificationResult(
            postings=results,
            usage=usage,
            attempts=attempt,
            suggestions=suggestions,
            suggestion_reasons=suggestion_reasons,
            notes=[*notes, *posting_notes],
        )

    assert last_error is not None  # 루프는 최소 한 번 돈다
    raise ClassifyError(
        "unparsable", f"{MAX_ATTEMPTS}회 모두 스키마에 맞지 않았다: {last_error}"
    ) from last_error


def _posting_result(
    posting: ParsedPosting,
    common: Mapping[str, Sequence[Piece]],
    lines: Sequence[str],
    body: str,
    title: str,
    taxonomy_choices: Mapping[str, tuple[str, ...]] | None,
    model: str,
) -> tuple[PostingResult, list[str]]:
    """나눈 공고 하나를 원문에서 옮기고 근거를 확인한다. 남길 메모도 함께 돌려준다.

    칸마다 공통 조각을 그 공고의 조각 앞에 붙인다. 같은 글자가 양쪽에서 나오면 한 번만 남는다
    (`app/classify/pieces.py` 의 `resolve`).
    """
    # 근거 문장에 번호까지 옮겨 왔으면 먼저 뗀다. 그다음 뽑는 칸은 조각을 원문에서 옮겨 글자로
    # 만든다
    fields = {name: strip_line_marks(value).strip() for name, value in posting.fields.items()}
    extracted = {
        name: resolve([*common.get(name, []), *posting.pieces.get(name, [])], lines)
        for name in EXTRACT_FIELDS
    }
    fields.update({name: item.value for name, item in extracted.items()})

    # 받은 값을 그 자리에서 **보낸 그 글**에 돌려 본다. 못 찾은 칸은 버린다. 보낸 것과
    # 다른 값에 돌려 보면 옳게 뽑은 칸이 버려진다 — 원문으로 물어 놓고 본문에 돌려 보면
    # 본문 밖 이름표에서 온 근무지가 통째로 사라진다. 제목까지 보는 것은 `job_role` 이
    # 거기서 오기 때문이다 (`app/classify/grounding.py`).
    #
    # 넘기는 것은 자르기 전 값이다. 모델이 본 것은 앞 `MAX_BODY_CHARS` 자뿐이라, 전체에
    # 돌려 보면 검사가 넓어질 뿐 좁아지지 않는다
    grounded = ground(fields, body, title, taxonomy_choices=taxonomy_choices)
    # 조각을 냈는데 하나도 옮기지 못한 칸은 버린 칸이다. 짚은 줄도 없고 그 글자도 원문
    # 어디에도 없었다 — 원문에 없는 값을 버리던 자리와 같은 이유로 센다
    for name, item in extracted.items():
        if item.lost and not item.value and name not in grounded.dropped:
            grounded.dropped.append(name)
            grounded.reasons[name] = NOT_IN_SOURCE
    if grounded.dropped:
        logger.warning(
            "분류가 근거 없는 값을 냈다 model=%s 버린 칸=%s",
            model,
            ", ".join(f"{name}({grounded.reasons[name]})" for name in grounded.dropped),
        )
    piece_notes = _piece_notes(extracted)
    if piece_notes:
        logger.warning(
            "분류 조각을 원문에서 그대로 찾지 못했다 model=%s %s",
            model,
            "; ".join(piece_notes),
        )
    notes = list(piece_notes)
    if grounded.dropped:
        notes.append(
            "근거가 없어 버린 칸: "
            + ", ".join(f"{name}({grounded.reasons[name]})" for name in grounded.dropped)
        )
    result = PostingResult(
        fields=grounded.fields,
        evidence=grounded.evidence,
        dropped=grounded.dropped,
        reasons=grounded.reasons,
    )
    return result, notes


async def _call(
    client: Any,
    model: str,
    prompt: str,
    attempt: int,
    provider: Provider,
    response_model: type[Classification] = Classification,
) -> tuple[str, Usage]:
    try:
        return await provider.call_model(
            client,
            model,
            prompt,
            attempt,
            "본문 분류",
            response_schema=response_model,
            system_instruction=_SYSTEM_INSTRUCTION,
        )
    except LlmCallError as exc:
        raise ClassifyError(exc.reason, str(exc)) from exc
