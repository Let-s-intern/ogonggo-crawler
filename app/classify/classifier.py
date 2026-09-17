"""공고 하나를 아홉 칸으로 나눈다.

수집은 어느 사이트나 확실히 주는 여섯 칸만 한다. 나머지를 나누는 것은 공고를 읽는 이쪽 일이다
(`.claude/tasks/memos/보류/llm-classify/prd-llm-classify.md`).

## 칸이 두 가지다

**뽑는 칸** 일곱은 원문 글자를 그대로 옮긴다. 옮긴 값은 원문에서 그대로 찾을 수 있어야 한다.
여섯은 본문에서 오고 `position_name` 하나만 **제목**에서 온다 — 열한 사이트에서 제목이 직무를
말하는 곳이 아홉이고 그중 본문이 같은 글자를 되풀이하는 곳은 셋뿐이었다
(`tests/test_job_role_source.py`).

**판정하는 칸**(고용형태·경력 구분·학력·채용 시 마감·지원 방법)은 본문을 읽고 오공고 enum
목록에서 반드시 하나를 고른다. 목록은 응답 스키마의 enum 으로 강제하고, 고른 값에는 근거
문장이 따라온다. 근거 문장을 본문에서 찾지 못해도 값은 남기고 검수 화면이 `근거 없음` 으로
보인다 (2026-09-14 결정). 최소 경력 연수만은 사실 값이라 근거 문장이 없으면 버린다.

어느 칸이 어느 쪽인지와 목록이 왜 그 목록인지는 `app/classify/schema.py` 에 있다.

## 지키는 것

**근거가 없는 것은 빈 칸이다.** 모델이 그럴듯하게 채우면 소비 측이 그것을 사실로 노출한다.
받은 값은 `app/classify/grounding.py` 가 그 자리에서 제목과 본문에 돌려 보고, 근거를 못 찾은
칸은 버리고 `dropped` 에 이름을 남긴다 (`.claude/rules/llm.md`).

**보내는 것은 제목과 상세 원문뿐이고 상한이 있다.** 원본 HTML 도 페이지도 보내지 않는다.
원문이 없는 건은 본문으로 떨어진다 (`app/classify/store.py`). 상한을 넘으면 잘라 보내고 그
사실을 `notes` 에 남긴다. 자른 것으로 무엇을 놓쳤는지는 응답을 보는 사람이 알아야 한다.
제목은 자르지 않는다 — 한 줄이고, 그 한 줄이 `position_name` 의 출처다.

**깨진 응답만 1회 다시 묻는다.** 스키마에 없는 칸을 지어낸 응답은 다시 물어도 같은 답이 온다.

**이미 값이 있는 칸은 채우지 않고 제안한다.** 수집이 채우는 여섯 칸 중
`company_name`·`recruitment_end_at`·`recruitment_start_at` 셋은 값이 있으면 아무리 근거가 있어도 그
자리에서 덮지 않는다 — `recruitment_end_at` 은 마감 지난 공고를 거르고 `company_name` 는 계열사를
가르는 값이라 모델 판단 하나로 바뀌면 안 된다 (`.claude/tasks/todo/prd-side-workflows.md` 6절). 대신
`ClassificationResult.suggestions` 로 나가고, 저장은 `job_field_suggestions` 하나뿐이다 — 사람이
검수 화면에서 수락해야 값이 바뀐다.

호출 자체와 비용 기록은 고른 제공자 항목이 한다 (`app/llm/`). 셀렉터 생성과 같은 경로이고,
**이 파일은 어느 제공자인지 모른다** — 그 선택은 설정이 정한다 (`app/llm/providers.py`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from app.classify.grounding import NOT_IN_SOURCE, ground, loose, missing_lines
from app.classify.pieces import (
    Resolved,
    number_lines,
    render,
    render_numbers,
    resolve,
    strip_line_marks,
)
from app.classify.prompt_rules import (
    DEFAULT_RULES,
    EXTRACT,
    INDUSTRIES,
    JUDGE,
    TAXONOMY,
    RuleSet,
    names_of,
    render_common,
    render_fields,
)
from app.classify.schema import (
    CLASSIFY_FIELDS,
    COLLECTED_REVIEW_FIELDS,
    COLLECTED_REVIEW_LABELS,
    EXTRACT_FIELDS,
    INDUSTRY,
    JOB_FIELD,
    JOB_ROLE,
    JUDGE_CHOICES,
    VALUE_LABELS,
    Classification,
    ClassifySchemaError,
    Outline,
    ParsedClassification,
    ParsedPosting,
    Piece,
    parse_classification,
    parse_outline,
    suggestion_field,
    suggestion_reason_field,
)
from app.config import Settings, get_settings
from app.llm.base import LlmCallError, Provider, Usage
from app.llm.log import CLASSIFY
from app.llm.providers import for_feature

logger = logging.getLogger(__name__)

T = TypeVar("T")

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
# 9,791자로 상한 안이다. 그 하나를 위해 상한을 세 배로 올리지 않는다. 상한을 넘는 글은 한 번에
# 나누지 않고 짜임을 먼저 물은 뒤 직무마다 부른다 (`_classify_long`)
MAX_BODY_CHARS = 12000

# 긴 공고의 첫 호출(짜임 묻기)에 보내는 글의 상한 (2026-09-11 결정). LG 33,225자가 여유 있게
# 들어간다. 넘으면 앞부분만 보내고 잘린 사실을 남긴다 — 상한이 없으면 페이지 전체를 담기
# 시작한 사이트가 그대로 나간다
MAX_OUTLINE_CHARS = 50000

# 호출 로그와 `llm_calls` 에서 호출을 가르는 이름
CLASSIFY_KIND = "본문 분류"
OUTLINE_KIND = "긴 공고 짜임"

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
{part_block}
# 공고 나누기 — postings 와 common

- 이 공고가 뽑는 직무(모집 분야)마다 postings 에 하나씩 낸다. 직무가 하나면 postings 는
  하나다. 직무 아래에 더 작은 직무가 나뉘어 있으면 가장 작은 직무마다 하나다.
- 모든 직무에 똑같이 해당하는 내용(공통 자격요건, 복지, 전형 절차, 회사 소개 등)은 common 에
  한 번만 담는다. 공고마다 common 이 붙으므로 같은 내용을 postings 에 되풀이하지 않는다.
- 한 직무에만 해당하는 내용은 그 직무의 posting 에 담는다.
- position_name 과 판정하는 칸은 posting 마다 그 직무를 보고 답한다. common 에는 없다.
- 직무가 하나인 공고는 전부 그 posting 에 담고 common 을 비워 둬도 된다.

# 뽑는 칸 — 어느 줄의 어느 부분인지를 조각으로 답한다

{extract_rules}

답하는 모양:
- 칸마다 조각 목록으로 답한다. 조각 하나는 {{"line": 줄 번호, "text": 그 줄에서 이 칸에
  해당하는 부분}} 이다. 원문에 없는 칸은 빈 목록([])으로 둔다. 짐작해서 채우지 않는다.
- **text 는 그 줄에 적힌 글자 그대로 옮긴다.** 단어를 바꾸거나, 요약하거나, 줄이거나,
  오타를 고치지 않는다. 줄 앞의 [번호] 는 text 에 넣지 않는다.
- 한 줄에 소제목과 내용이 같이 있으면(`주요업무 : 결제 서버 개발`) 내용 부분만 옮긴다
  (`결제 서버 개발`).
- 한 줄에 여러 칸이 섞여 있으면(`근무지: 성남 | 고용형태: 정규직`) 그 칸에 해당하는
  부분만 옮긴다(근무지라면 `성남`).
- 줄이 영어 키 이름으로 시작하면(`ruWorkpl: 본사(서울 63빌딩)`) 그 이름은 무엇에 대한
  값인지 알려 주는 표시다. 키 이름은 옮기지 않고 값 부분만 옮긴다(`본사(서울 63빌딩)`).
- 내용이 여러 줄이면 줄마다 조각을 하나씩 낸다. 본문 여러 곳에 흩어져 있으면 그 줄들을
  모두 조각으로 낸다.
{common_rules}
# 판정하는 칸 — 본문을 읽고 목록에서 고른다

{judge_rules}

규칙:
- **이 칸들은 글자가 본문에 그대로 없어도 된다.** 본문을 읽고 판단해서 고른다. 칸마다 적힌 예는
  원문 문장과 그때 고를 값이다.
- **목록이 있는 칸은 반드시 목록의 값 하나를 고른다. 비워 두지 않는다.** 괄호 앞의 이름을
  그대로 적는다(`FULL_TIME`). 목록에 없는 값을 새로 만들지 않는다. 본문이 분명히 말하지
  않으면 공고 전체를 읽고 가장 그럴듯한 값을 고른다.
- 칸마다 `employment_type_evidence` 처럼 `_evidence` 가 붙은 자리에 **그렇게 판단한 근거가
  되는 본문 문장을 그대로 옮겨 적는다.** 한 문장이면 된다. 본문에 없는 문장을 적지 않는다.
  줄 앞의 [번호] 는 넣지 않는다. 근거가 되는 문장이 없으면 `_evidence` 만 비우고 값은 고른다.
- 목록이 없는 칸(`experience_min_years`)은 원문에 근거가 있을 때만 채우고 근거 문장을 반드시
  적는다. 근거 문장이 없는 값은 버려진다.
- 회사명·모집 시작일·마감일은 위 칸 어디에도 넣지 않는다. 그 셋을 원문과 견주는 자리는
  값이 이미 있을 때만 아래에 따로 나온다. 제목도 `position_name` 말고는 어느 칸에도 넣지 않는다.
{taxonomy_block}{industry_block}{current_values_block}
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
    | `unknown_field` | 1회 재요청까지 하고도 모델이 스키마에 없는 칸을 냈다 |
    | `empty_body` | 나눌 본문이 없다. 모델을 부르지 않는다 |
    | `parts_mismatch` | 다시 분류하는데 나눈 공고 수와 다르게 답했다. 기존 분류를 둔다 |

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
    # 긴 공고에서 이 공고를 나눌 때 보낸 원문 줄 번호. 다시 분류할 때 같은 줄을 보낸다. 한 번에
    # 나눈 공고는 비어 있다
    sent_lines: tuple[int, ...] = ()

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
    # 이미 값이 있는 칸(`company_name`·`recruitment_end_at`·`recruitment_start_at`)에 원문이
    # 다르다고 낸 값. 근거 검사를 통과하고 지금 값과 실제로 다른 것만 남는다 — 저장은
    # `job_field_suggestions` 뿐이고 여기 값이 `normalized_jobs` 를 자동으로 덮는 경로는 없다
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


# 긴 공고에서 직무 하나만 떼어 보낼 때 프롬프트에 더하는 구역
_PART_BLOCK = """
# 긴 공고의 한 직무

이 글은 직무가 여럿인 긴 공고에서 **한 직무**의 줄과 모든 직무에 공통인 줄만 떼어 온 것이다.
줄 번호는 원래 공고의 번호라 건너뛴 번호가 있다.
- postings 는 하나만 낸다. 이 직무다.
- position_name 은 제목이 아니라 본문에서 이 직무의 이름이 적힌 줄에서 가져온다. 직무가 사업부·조직
  아래에 있으면 그 조직 이름이 적힌 줄도 조각으로 먼저 낸다.
- 공통 줄의 내용도 이 공고의 내용이다. common 에 담아도 되고 posting 에 담아도 된다.
"""

_OUTLINE_INSTRUCTION = (
    "너는 긴 채용공고의 짜임을 읽는다. 직무마다 그 직무에만 해당하는 줄의 범위와, 모든 직무에 "
    "공통인 줄의 범위를 답한다. 줄 번호는 글에 붙은 [번호] 를 그대로 쓰고, 칸은 뽑지 않는다."
)

_OUTLINE_PROMPT = """아래는 직무가 여럿일 수 있는 긴 채용공고다. 줄마다 앞에 [번호] 가 붙어 있다.
[0] 이 제목이고 [1] 부터가 본문이다. 칸을 나누기 전에 공고의 짜임만 먼저 답한다.

- roles: 이 공고가 뽑는 직무(모집 분야)마다 하나씩 낸다. 직무 아래에 더 작은 직무가 나뉘어
  있으면 가장 작은 직무마다 하나다. 직무가 하나면 하나다.
- roles 의 lines: 그 직무에만 해당하는 줄의 범위를 {{"start": 첫 줄 번호, "end": 끝 줄 번호}}
  로 적는다. 끝 줄도 들어간다. 직무 이름이 적힌 줄도 넣는다. 직무가 사업부·조직 아래에
  있으면 그 조직 이름이 적힌 줄도 넣는다. 흩어져 있으면 범위를 여럿 적는다.
- common_lines: 모든 직무에 똑같이 해당하는 줄의 범위(공통 자격요건, 복지, 전형 절차, 접수
  기간, 회사 소개 등).
- 어느 직무의 내용도 공통 안내도 아닌 줄(다른 글, 인터뷰, 기사, 화면 문구)은 어디에도 넣지
  않는다.
{current_values_block}
[제목]
{title}

[본문]
{body}
"""


def _known_roles_block(roles: Sequence[str]) -> str:
    """이미 나눈 직무 구역. 다시 분류할 때 나눈 목록을 고정한다. 없으면 빈 문자열이다.

    번호에 사람 보정과 전달된 공고 주소가 붙어 있어, 개수나 순서가 바뀌면 그 값이 다른 직무로
    옮겨 붙는다 (2026-09-11 결정).
    """
    if not roles:
        return ""
    items = "\n".join(
        # 조직 이름이 붙은 직무는 이름이 두 줄이라 한 줄로 잇는다
        f"{number}. {' '.join(role.split()) or '(이름 없음)'}"
        for number, role in enumerate(roles, start=1)
    )
    return (
        "\n# 이미 나눈 직무\n\n"
        f"이 공고는 이미 아래 직무로 나눴다. postings 를 이 순서대로 정확히 {len(roles)}개 낸다.\n"
        "직무를 더하거나 빼거나 합치지 않는다.\n\n"
        f"{items}\n"
    )


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


def _taxonomy_block(
    tree: Sequence[tuple[str, tuple[str, ...]]], rules: RuleSet = DEFAULT_RULES
) -> str:
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
        "job_field 는 대분류(직군), job_role 는 그 대분류 밑의 소분류(직무)다. 목록에 없는 이름을\n"
        "새로 만들지 않고, 비워 두지 않는다.\n"
        "직무마다 나눈 공고는 posting 마다 그 직무를 보고 고른다.\n"
        "job_role 는 반드시 그 job_field 줄에 적힌 소분류 중에서 고른다 — 다른 대분류의 소분류를\n"
        "고르지 않는다.\n\n"
        f"{render_fields(rules, names_of(TAXONOMY))}\n\n"
        "고른 값마다 job_field_evidence / job_role_evidence 에 그렇게 판단한 본문 근거\n"
        "문장을 그대로 옮겨 적는다. `기타` 소분류를 골랐을 때도 이 공고가 그 대분류의 일을\n"
        "한다고 볼 수 있는 본문 문장을 그대로 옮겨 적는다 — 근거 문장은 항상 원문에 있는\n"
        "그대로여야 하고, 소분류 이름 자체를 짐작해 지어내지 않는다.\n\n"
        f"{lines}\n"
    )


def _industry_block(industries: Sequence[str], rules: RuleSet = DEFAULT_RULES) -> str:
    """산업 구역. 표가 비어 있으면(씨앗 전이거나 전부 껐으면) 빈 문자열이다 (2026-09-14 결정).

    공고마다 고른다. 같은 회사의 공고는 대개 같은 산업이지만 회사 단위로 묻지 않는다 — 오공고가
    공고마다 산업을 받는다 (`app/industries.py`).
    """
    if not industries:
        return ""
    names = "\n".join(f"- {name}" for name in industries)
    return (
        "\n# 산업 — 아래 목록에서만 고른다\n\n"
        "industry 는 이 공고를 낸 회사가 속한 산업이다. posting 마다 고른다. 목록에 없는 이름을\n"
        "새로 만들지 않고, 비워 두지 않는다.\n\n"
        f"{render_fields(rules, names_of(INDUSTRIES))}\n\n"
        "industry_evidence 에 그렇게 판단한 원문 문장을 그대로 옮겨 적는다. 그런 문장이 없으면\n"
        "비워 두고 값은 고른다.\n\n"
        f"{names}\n"
    )


def build_prompt(
    body: str,
    title: str = "",
    current_values: Mapping[str, str] | None = None,
    taxonomy_tree: Sequence[tuple[str, tuple[str, ...]]] = (),
    *,
    known_roles: Sequence[str] = (),
    rules: RuleSet | None = None,
    industries: Sequence[str] = (),
) -> tuple[str, list[str]]:
    """보낼 프롬프트와 남길 메모. 상한을 넘긴 글은 자르고 그 사실을 적는다.

    `known_roles` 는 다시 분류할 때 이미 나눈 직무 이름들이다. 주면 그 순서·개수로 답하게 한다.

    `body` 는 상세 원문이거나, 원문이 없는 건에서 본문이다 (`app/classify/store.py`).

    `title` 이 `position_name` 의 출처다. 제목을 보내지 않으면 그 칸은 영원히 빈다. 제목이 없는
    공고는 빈 줄이 들어가고, 모델은 옮길 것이 없어 빈 문자열을 낸다 — 수집이 제목을 못 뽑는
    것은 수집의 실패이지 여기서 메울 일이 아니다 (`app/crawler/parser.py`).

    판정 칸의 목록을 프롬프트에도 적는다. 스키마의 enum 이 이미 강제하지만, 무엇 중에서
    고르는지 모르는 채로 고르면 목록에서 가장 가까운 값이 아니라 첫 값이 나온다. 목록의
    출처는 스키마 하나다 — 여기에 손으로 적으면 두 목록이 갈린다.

    `current_values` 는 `company_name`·`recruitment_end_at`·`recruitment_start_at` 중 이미 채워진
    값이다 (`app/classify/store.py` 의 `read_current_values`). 무엇이 이미 채워져 있는지 모르면
    "원문과 다르다" 를 모델이 말할 수 없다 — 값이 없으면 그 칸은 프롬프트에 아예 나오지 않고, 나오지
    않은 칸을 모델이 지어내 제안하면 근거 검사가 버린다.

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
    return (
        _classification_prompt(
            title=render(lines[:1]),
            body=render(lines[1:], start=1),
            current_values_block=_current_values_block(current_values or {}),
            taxonomy_tree=taxonomy_tree,
            part_block=_known_roles_block(known_roles),
            rules=rules or DEFAULT_RULES,
            industries=industries,
        ),
        notes,
    )


def build_part_prompt(
    lines: Sequence[str],
    numbers: Sequence[int],
    taxonomy_tree: Sequence[tuple[str, tuple[str, ...]]] = (),
    rules: RuleSet | None = None,
    industries: Sequence[str] = (),
) -> str:
    """긴 공고에서 직무 하나를 나눌 프롬프트. 제목과 고른 줄만 원래 번호로 보낸다.

    이미 있는 값은 적지 않는다. 제안은 공고 전체를 본 짜임 호출이 이미 받았다.
    """
    return _classification_prompt(
        title=render(lines[:1]),
        body=render_numbers(lines, numbers),
        current_values_block="",
        taxonomy_tree=taxonomy_tree,
        part_block=_PART_BLOCK,
        rules=rules or DEFAULT_RULES,
        industries=industries,
    )


def build_outline_prompt(
    body: str, title: str = "", current_values: Mapping[str, str] | None = None
) -> tuple[str, list[str]]:
    """긴 공고의 짜임을 물을 프롬프트와 남길 메모. `MAX_OUTLINE_CHARS` 를 넘는 글은 자른다."""
    notes: list[str] = []
    text = body
    if len(text) > MAX_OUTLINE_CHARS:
        notes.append(f"긴 공고가 {len(text)}자라 짜임을 물을 때 앞 {MAX_OUTLINE_CHARS}자만 보냈다")
        text = text[:MAX_OUTLINE_CHARS]
    lines = number_lines(title, text)
    prompt = _OUTLINE_PROMPT.format(
        title=render(lines[:1]),
        body=render(lines[1:], start=1),
        current_values_block=_current_values_block(current_values or {}),
    )
    return prompt, notes


def _classification_prompt(
    *,
    title: str,
    body: str,
    current_values_block: str,
    taxonomy_tree: Sequence[tuple[str, tuple[str, ...]]],
    part_block: str = "",
    rules: RuleSet = DEFAULT_RULES,
    industries: Sequence[str] = (),
) -> str:
    """칸별·공통 규칙은 `rules` 에서, 나머지 골격은 이 파일에서 온다.

    규칙 한 벌의 모양과 판은 `app/classify/prompt_rules.py` 에 있다.
    """
    # 저장하는 이름 뒤에 화면 이름을 붙여 보낸다. 모델은 괄호 앞의 이름을 고른다
    choices = {
        name: " / ".join(f"{value}({VALUE_LABELS[name][value]})" for value in values)
        for name, values in JUDGE_CHOICES.items()
    }
    return _PROMPT.format(
        body=body,
        title=title,
        current_values_block=current_values_block,
        taxonomy_block=_taxonomy_block(taxonomy_tree, rules),
        industry_block=_industry_block(industries, rules),
        part_block=part_block,
        extract_rules=render_fields(rules, names_of(EXTRACT)),
        common_rules=render_common(rules),
        judge_rules=render_fields(rules, names_of(JUDGE), choices),
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
    known_parts: Sequence[tuple[str, Sequence[int]]] = (),
    settings: Settings | None = None,
    client: Any | None = None,
    on_call: Callable[[Usage], None] | None = None,
    rules: RuleSet | None = None,
    industries: Sequence[str] = (),
) -> ClassificationResult:
    """공고 하나를 나눈다. 받은 값은 원문에 있는지 확인한 뒤에만 남는다.

    `rules` 는 칸별·공통 규칙 한 벌이다. 주지 않으면 코드의 기본 규칙(판 0)이다 — 부르는 쪽이
    저장된 판을 읽어 넘기고, 규칙 시험은 저장하지 않은 규칙을 넘긴다
    (`app/classify/prompt_rules.py`).

    직무가 여럿인 공고는 직무마다 `ClassificationResult.postings` 하나가 된다. 칸마다 공통
    조각을 그 공고의 조각 앞에 붙인 뒤 공고마다 근거를 확인한다 (`app/classify/schema.py`).

    `known_parts` 는 다시 분류할 때 이미 나눈 공고들의 (직무 이름, 보낸 줄) 이다. 주면 나눈
    목록을 그대로 두고 칸만 다시 채운다 — 긴 공고는 짜임을 다시 묻지 않고 번호마다 보냈던 줄을
    보내고, 한 번에 나눈 공고는 직무 목록을 알려 같은 개수로 답하게 한다. 개수가 다르면
    `parts_mismatch` 로 실패하고 부르는 쪽은 기존 분류를 그대로 둔다.

    `title` 은 `position_name` 의 출처다. 비어 있어도 나머지 여덟 칸은 그대로 나오므로 분류가
    실패하지 않는다 — 나눌 것이 없는 것은 본문이 빈 경우뿐이다.

    `current_values` 는 `company_name`·`recruitment_end_at`·`recruitment_start_at` 중 이미 채워진
    값이다. 값이 있는 칸에 원문이 다른 값을 말하면 `ClassificationResult.suggestions` 로 나가고,
    `fields` 의 아홉 칸은 건드리지 않는다 — 이 셋은 애초에 `CLASSIFY_FIELDS` 에 없다.

    `taxonomy_tree` 와 `response_model` 은 함께 온다 — 부르는 쪽(`app/classify/batch.py`)이
    배치 시작 전에 `app.taxonomy.enabled_tree()` 와 `build_classification_model()` 로 한 번만
    만들어 공고마다 그대로 넘긴다. 공고마다 표를 다시 읽을 이유가 없다. 빈 트리(기본값)는
    "표가 비었다" 는 뜻이고, 그때 `response_model` 은 `Classification` 그대로다.
    `industries` 는 같은 방법으로 `app.industries.enabled_names()` 가 만든 켜진 산업 이름이다
    (2026-09-14 결정).

    `on_call` 은 모델을 부를 때마다 그 호출의 비용으로 불린다. 깨진 응답으로 한 번 더 물으면
    두 번 불린다 — 부르는 쪽이 그것을 `llm_calls` 에 그대로 남겨야 토큰 합이 실제와 맞는다
    (`app/llm/log.py`).
    """
    if not body.strip():
        raise ClassifyError("empty_body", "본문이 비어 있어 나눌 것이 없다")

    resolved = settings or get_settings()
    provider, model = chosen(resolved)
    asker = _Asker(client or build_client(resolved), model, provider, on_call)
    taxonomy_choices = _taxonomy_choices(taxonomy_tree, industries)
    rule_set = rules or DEFAULT_RULES

    if known_parts and all(part_lines for _, part_lines in known_parts):
        # 긴 공고를 다시 분류한다. 짜임을 다시 묻지 않고 번호마다 보냈던 줄을 그대로 보낸다.
        # 제안은 공고 전체를 보는 짜임 호출에서만 받으므로 이번에는 없다
        results, notes, usages, attempts = await _classify_parts(
            asker,
            body,
            title,
            number_lines(title, body),
            taxonomy_tree,
            response_model,
            taxonomy_choices,
            [list(part_lines) for _, part_lines in known_parts],
            rule_set,
            industries,
        )
        return ClassificationResult(
            postings=results, usage=_total(usages), attempts=attempts, notes=notes
        )

    if len(body) > MAX_BODY_CHARS and not known_parts:
        return await _classify_long(
            asker,
            body,
            title,
            current_values or {},
            taxonomy_tree,
            response_model,
            taxonomy_choices,
            rule_set,
            industries,
        )

    prompt, notes = build_prompt(
        body,
        title,
        current_values,
        taxonomy_tree,
        known_roles=[role for role, _ in known_parts],
        rules=rule_set,
        industries=industries,
    )
    parsed, usage, attempts = await asker.ask(
        prompt,
        schema=response_model,
        instruction=_SYSTEM_INSTRUCTION,
        kind=CLASSIFY_KIND,
        parse=lambda text: parse_classification(text, response_model),
    )
    if known_parts and len(parsed.postings) != len(known_parts):
        raise ClassifyError(
            "parts_mismatch",
            f"이미 나눈 공고가 {len(known_parts)}개인데 {len(parsed.postings)}개로 답했다. "
            "기존 분류를 그대로 둔다",
        )
    return _result_of(
        parsed,
        body,
        title,
        current_values or {},
        taxonomy_choices,
        model,
        usage=usage,
        attempts=attempts,
        notes=notes,
    )


async def _classify_long(
    asker: _Asker,
    body: str,
    title: str,
    current_values: Mapping[str, str],
    taxonomy_tree: Sequence[tuple[str, tuple[str, ...]]],
    response_model: type[Classification],
    taxonomy_choices: Mapping[str, tuple[str, ...]] | None,
    rules: RuleSet = DEFAULT_RULES,
    industries: Sequence[str] = (),
) -> ClassificationResult:
    """긴 공고. 짜임을 먼저 묻고, 직무마다 공통 줄과 그 직무의 줄만 보내 나눈다 (2026-09-11 결정).

    한 번에 보내면 앞 `MAX_BODY_CHARS` 자 뒤의 직무를 못 보고, 직무가 서른 개인 공고를 한 번에
    나누게 하면 응답이 잘린다. 어느 직무의 줄도 공통 줄도 아닌 글(인터뷰·기사)은 직무 호출에
    실리지 않는다.

    짜임에서 직무를 하나도 읽지 못하면 예전처럼 앞부분만 보내 한 번에 나눈다. 짜임을 물은
    비용은 그대로 남는다.
    """
    lines = number_lines(title, body)
    outline_prompt, notes = build_outline_prompt(body, title, current_values)
    outline, usage, attempts = await asker.ask(
        outline_prompt,
        schema=Outline,
        instruction=_OUTLINE_INSTRUCTION,
        kind=OUTLINE_KIND,
        parse=lambda text: parse_outline(text, len(lines)),
    )
    usages = [usage]
    total_attempts = attempts

    if not outline.roles:
        prompt, cut = build_prompt(
            body, title, current_values, taxonomy_tree, rules=rules, industries=industries
        )
        parsed, usage, attempts = await asker.ask(
            prompt,
            schema=response_model,
            instruction=_SYSTEM_INSTRUCTION,
            kind=CLASSIFY_KIND,
            parse=lambda text: parse_classification(text, response_model),
        )
        return _result_of(
            parsed,
            body,
            title,
            current_values,
            taxonomy_choices,
            asker.model,
            usage=_total([*usages, usage]),
            attempts=total_attempts + attempts,
            notes=[*notes, "긴 공고의 직무를 짜임에서 읽지 못해 한 번에 나눴다", *cut],
        )

    results, posting_notes, part_usages, part_attempts = await _classify_parts(
        asker,
        body,
        title,
        lines,
        taxonomy_tree,
        response_model,
        taxonomy_choices,
        [sorted({*outline.common, *role}) for role in outline.roles],
        rules,
        industries,
    )
    suggestions, suggestion_reasons = _suggestions_of(outline.fields, current_values, body, title)
    return ClassificationResult(
        postings=results,
        usage=_total([*usages, *part_usages]),
        attempts=total_attempts + part_attempts,
        suggestions=suggestions,
        suggestion_reasons=suggestion_reasons,
        notes=[*notes, *posting_notes],
    )


async def _classify_parts(
    asker: _Asker,
    body: str,
    title: str,
    lines: Sequence[str],
    taxonomy_tree: Sequence[tuple[str, tuple[str, ...]]],
    response_model: type[Classification],
    taxonomy_choices: Mapping[str, tuple[str, ...]] | None,
    parts: Sequence[Sequence[int]],
    rules: RuleSet = DEFAULT_RULES,
    industries: Sequence[str] = (),
) -> tuple[list[PostingResult], list[str], list[Usage], int]:
    """직무마다 제목과 고른 줄만 보내 나눈다. 짜임이 정했거나 전에 보냈던 줄이다.

    돌려주는 것은 공고들, 남길 메모, 호출마다의 비용, 물은 횟수의 합이다.
    """
    split = len(parts) > 1
    results: list[PostingResult] = []
    notes: list[str] = []
    usages: list[Usage] = []
    total_attempts = 0
    for number, numbers in enumerate(parts, start=1):
        parsed, usage, attempts = await asker.ask(
            build_part_prompt(lines, numbers, taxonomy_tree, rules, industries),
            schema=response_model,
            instruction=_SYSTEM_INSTRUCTION,
            kind=CLASSIFY_KIND,
            parse=lambda text: parse_classification(text, response_model),
        )
        usages.append(usage)
        total_attempts += attempts
        prefix = f"{number}번 공고: " if split else ""
        postings = parsed.postings or [ParsedPosting(fields={}, pieces={})]
        if len(postings) > 1:
            # 직무 하나를 보냈는데 더 잘게 나눴다. 짜임이 정한 목록을 따른다
            notes.append(f"{prefix}한 직무를 보냈는데 공고 {len(postings)}개가 와 첫 공고만 남겼다")
        result, found = _posting_result(
            postings[0],
            parsed.common,
            lines,
            body,
            title,
            taxonomy_choices,
            asker.model,
            sent_lines=numbers,
        )
        results.append(result)
        notes.extend(prefix + note for note in found)
    return results, notes, usages, total_attempts


@dataclass(frozen=True)
class _Asker:
    """공고 하나를 나누는 동안 같은 클라이언트·모델·제공자로 묻는다. 긴 공고는 여러 번 묻는다."""

    client: Any
    model: str
    provider: Provider
    on_call: Callable[[Usage], None] | None

    async def ask(
        self,
        prompt: str,
        *,
        schema: Any,
        instruction: str,
        kind: str,
        parse: Callable[[str], T],
    ) -> tuple[T, Usage, int]:
        """한 번 묻고 받은 것을 읽는다. 스키마에 맞지 않으면 한 번 더 묻는다.

        `on_call` 은 호출마다 불린다. 깨진 응답으로 한 번 더 물으면 두 번 불린다 — 부르는 쪽이
        그것을 `llm_calls` 에 그대로 남겨야 토큰 합이 실제와 맞는다 (`app/llm/log.py`).
        """
        last_error: ClassifySchemaError | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            text, usage = await _call(
                self.client, self.model, prompt, attempt, self.provider, schema, instruction, kind
            )
            if self.on_call is not None:
                self.on_call(usage)
            try:
                return parse(text), usage, attempt
            except ClassifySchemaError as exc:
                logger.warning(
                    "분류 응답 거절 model=%s attempt=%d reason=%s message=%s",
                    self.model,
                    attempt,
                    exc.reason,
                    exc,
                )
                # 없는 칸 이름도 한 번 더 묻는다. Gemini 에서는 다시 물어도 같은 답이었지만
                # DeepSeek 는 같은 공고에서 매번 다른 칸을 지어낸다(`org_name`, 판정 칸을 common
                # 에 넣기) — 2026-09-17 APR 20건 중 7건, 다시 물으면 대개 맞게 온다
                last_error = exc

        assert last_error is not None  # 루프는 최소 한 번 돈다
        raise ClassifyError(
            last_error.reason, f"{MAX_ATTEMPTS}회 모두 스키마에 맞지 않았다: {last_error}"
        ) from last_error


def _taxonomy_choices(
    taxonomy_tree: Sequence[tuple[str, tuple[str, ...]]],
    industries: Sequence[str] = (),
) -> dict[str, tuple[str, ...]] | None:
    """근거 검사가 직무 분류와 산업을 볼 목록. 두 표가 다 비었으면 None 이라 보지 않는다."""
    choices: dict[str, tuple[str, ...]] = {}
    if taxonomy_tree:
        choices[JOB_FIELD] = tuple(major for major, _ in taxonomy_tree)
        minors = tuple(minor for _, minor_list in taxonomy_tree for minor in minor_list)
        if minors:
            choices[JOB_ROLE] = minors
        # 직무는 고른 직군 아래의 것이어야 한다. 근거 검사가 직군마다 이 목록으로 다시 본다
        for major, minor_list in taxonomy_tree:
            choices[f"{JOB_ROLE}:{major}"] = tuple(minor_list)
    if industries:
        choices[INDUSTRY] = tuple(industries)
    return choices or None


def _result_of(
    parsed: ParsedClassification,
    body: str,
    title: str,
    current_values: Mapping[str, str],
    taxonomy_choices: Mapping[str, tuple[str, ...]] | None,
    model: str,
    *,
    usage: Usage,
    attempts: int,
    notes: Sequence[str],
) -> ClassificationResult:
    """한 번에 나눈 응답을 결과로 만든다. 공고마다 원문에서 옮기고 근거를 확인한다."""
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

    suggestions, suggestion_reasons = _suggestions_of(parsed.fields, current_values, body, title)
    return ClassificationResult(
        postings=results,
        usage=usage,
        attempts=attempts,
        suggestions=suggestions,
        suggestion_reasons=suggestion_reasons,
        notes=[*notes, *posting_notes],
    )


def _suggestions_of(
    fields: Mapping[str, str], current_values: Mapping[str, str], body: str, title: str
) -> tuple[dict[str, str], dict[str, str]]:
    """맨 위 제안 칸을 추린다. 같은 응답에서 받는다 — 두 번째 호출을 만들면 토큰이 두 배다."""
    top = {name: strip_line_marks(value).strip() for name, value in fields.items()}
    return _extract_suggestions(top, current_values, f"{title}\n{body}")


def _total(usages: Sequence[Usage]) -> Usage:
    """여러 호출의 비용을 하나로 더한다. 호출마다의 기록은 `on_call` 이 이미 남겼다."""
    first = usages[0]
    return Usage(
        provider=first.provider,
        model=first.model,
        input_tokens=sum(usage.input_tokens for usage in usages),
        output_tokens=sum(usage.output_tokens for usage in usages),
        total_tokens=sum(usage.total_tokens for usage in usages),
        latency_ms=sum(usage.latency_ms for usage in usages),
    )


def _posting_result(
    posting: ParsedPosting,
    common: Mapping[str, Sequence[Piece]],
    lines: Sequence[str],
    body: str,
    title: str,
    taxonomy_choices: Mapping[str, tuple[str, ...]] | None,
    model: str,
    *,
    sent_lines: Sequence[int] = (),
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
    # 본문 밖 이름표에서 온 근무지가 통째로 사라진다. 제목까지 보는 것은 `position_name` 이
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
        sent_lines=tuple(sent_lines),
    )
    return result, notes


async def _call(
    client: Any,
    model: str,
    prompt: str,
    attempt: int,
    provider: Provider,
    response_model: Any = Classification,
    instruction: str = _SYSTEM_INSTRUCTION,
    kind: str = CLASSIFY_KIND,
) -> tuple[str, Usage]:
    try:
        return await provider.call_model(
            client,
            model,
            prompt,
            attempt,
            kind,
            response_schema=response_model,
            system_instruction=instruction,
        )
    except LlmCallError as exc:
        raise ClassifyError(exc.reason, str(exc)) from exc
