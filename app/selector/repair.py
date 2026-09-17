"""실패한 필드만 다시 골라 달라고 모델에게 요청한다.

2026-08-22 QA 에서 롯데 등록 때 모델이 계열사 링크 목록(`ul.family-group li`)을 `list.item`
으로 잡았다. 사람이 브라우저로 HTML 을 열어 `ul.job-card-list` 를 찾아 손으로 넣었다. 그 일을
모델에게 맡기는 것이 이 모듈이다.

`.claude/rules/llm.md` 가 손 편집을 첫 수단으로 두고 **요청 없이 재생성하지 말라**고 못박고
있다. 그래서 이 경로는 자동이 아니라 운영자가 누르는 버튼으로만 들어온다. 저장도 하지 않는다 —
고치기 전과 후를 나란히 돌려줄 뿐이고, `crawlers.selectors_json` 을 바꾸는 것은 지금까지처럼
`PUT /api/crawlers/{id}/selectors` 하나뿐이다.

## 고치는 범위

**실패한 필드만이다.** 잘 되는 필드는 응답에 무엇이 오든 원래 값을 그대로 쓴다. 모델이 한 번
맞춘 것을 두 번째 호출에서 잃는 일이 없어야 하고, 그것을 프롬프트의 부탁이 아니라 코드로
보장한다(`_overlay`).

`건너뜀` 은 대상이 아니다. 셀렉터가 비어 있는 선택 필드는 "사이트에 그 항목이 없다"는 응답이라
고칠 셀렉터가 없다. 억지로 채우면 잘못된 값이 공고마다 붙는다.

돌려볼 HTML 이 없는 필드도 대상이 아니다. 상세 URL 없이 등록한 크롤러의 상세 필드가 그렇다 —
0개 매칭이지만 판정하지 못한 것이지 틀린 것이 아니다.

모델이 못 고친 필드는 원래 값이 남는다. 빈 문자열로 덮지 않는다.

## 운영자 힌트

모델이 HTML 만 보고는 못 고치는 자리가 있다. 2026-08-23 LG 의 `list.link` 가 그랬다 — 항목
안에 `a` 태그가 없어 모델이 고를 것이 없었다. 사람은 브라우저에서 그 자리를 볼 수 있으므로,
본 것을 글로 실어 보내는 통로가 `hint` 다.

힌트는 자유 입력이다. F12 의 `Copy selector` 가 뱉은 경로일 수도 있고 "마감일은 목록 두 번째
줄에 있다" 같은 문장일 수도 있다. 둘 다 그냥 사람이 준 단서로 프롬프트에 싣는다.

**받은 경로를 그대로 쓰게 하지 않는다.** `css-jj9lbc` 는 빌드마다 바뀌는 자동 생성 클래스고
`div:nth-child(2)` 는 항목이 하나 늘면 어긋난다. 그것을 저장하면 다음 배포에 깨질 셀렉터를
심는 것이다. 프롬프트는 힌트를 **위치 단서**로 쓰라고 적고, 그 자리 근처의 안정적인 것(의미
있는 클래스, `data-` 속성, 태그 구조)을 찾게 한다. 그런 것이 없어 결국 위치로 잡은 셀렉터가
나오면 `_fragile_notes` 가 그 사실을 결과에 적는다.

힌트를 받았다고 검증을 건너뛰지 않는다. 고친 셀렉터는 힌트가 있든 없든 같은 HTML 에 다시
돌려 매칭 개수를 센다.

## 두 번째 API 경로를 만들지 않는다

정제(`app/selector/cleaner.py`), 호출과 로그(`generator.call_model`), 스키마 검증
(`app/selector/schema.py`)을 생성과 그대로 공유한다. 모델 ID·토큰·지연 로그와 "깨진 응답만
1회 재시도" 규칙이 여기에도 같이 적용된다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings, get_settings
from app.crawler.fetcher import PageSource, get_fetcher
from app.llm.base import LlmCallError, Provider
from app.llm.log import SELECTOR_REPAIR
from app.llm.providers import for_feature
from app.selector.cleaner import DEFAULT_MAX_CHARS, CleanedHtml, clean_html
from app.selector.generator import (
    MAX_ATTEMPTS,
    SelectorGenerationError,
    Usage,
    build_client_for,
    call_model,
)
from app.selector.schema import (
    SelectorSchemaError,
    SelectorSet,
    parse_generated,
    parse_generated_allowing_empty,
)
from app.selector.verify import FieldMatch, VerificationReport, verify_selectors

logger = logging.getLogger(__name__)

_PROMPT = """{intro}

규칙:
- 고칠 필드는 "고쳐야 하는 필드" 에 적힌 것뿐이다. 나머지 필드는 "현재 셀렉터" 의 값을
  그대로 옮겨 적는다. 다른 필드를 바꿔도 반영되지 않는다.
- 이미 잘 잡고 있는 필드는 손대지 않는다. 바꿀 이유가 단서에 적혀 있을 때만 바꾼다.
- 주어진 HTML 에 실제로 있는 것만 쓴다. 본 적 없는 클래스명을 지어내지 않는다.
- 이미 틀린 것으로 판정된 셀렉터를 그대로 다시 내지 않는다. 같은 답이면 고친 것이 아니다.
- list.item 은 공고 하나에 해당하는 반복 요소다. 목록 전체를 감싸는 컨테이너가 아니고,
  계열사 링크 목록이나 배너처럼 공고가 아닌 반복 요소도 아니다. 그 안에 공고 제목이 있는
  반복 요소를 고른다.
- list.title, list.link, list.date, list.company_name 는 list.item 안에서 찾을 수 있는
  셀렉터로 쓴다.
- list.link 는 상세 페이지로 가는 a 태그를 가리켜야 한다. 그 a 의 href 가 실제 주소여야 한다.
  항목 안에 그런 a 가 없거나 href 가 javascript: 나 # 뿐이면 빈 문자열로 둔다.
- link_template 은 항상 빈 문자열로 둔다. 상세 URL 형식은 이 HTML 만으로 알 수 없다.
- 고칠 방법이 이 HTML 에 없으면 그 필드를 빈 문자열로 둔다. 억지로 아무 요소나 고르지 않는다.
  잘못 고른 셀렉터는 못 고친 것보다 나쁘다 — 조용히 틀린 값이 공고마다 붙는다.

[현재 셀렉터]
{current}

[고쳐야 하는 필드]
{failures}


[목록 페이지 {list_url}]
{list_html}

[상세 페이지 {detail_url}]
{detail_html}
{hint}"""


# 여는 문장. 실패한 필드를 고칠 때와, 실패는 없는데 운영자가 특정 자리를 지적할 때가 다르다
_INTRO_FAILED = (
    "아래 채용 사이트의 셀렉터 중 몇 개가 지금 HTML 에서 아무것도 잡지 못한다.\n"
    "못 잡는 필드만 다시 골라라."
)
_INTRO_HINTED = (
    "아래 채용 사이트의 셀렉터는 지금 HTML 에서 전부 무언가를 잡고 있다. 그런데 운영자가"
    " 잡히는 값이 틀렸다고 지적했다.\n"
    "운영자가 준 단서를 읽고, 그 단서가 가리키는 필드만 다시 골라라. 단서와 상관없는 필드는"
    " 지금 값을 그대로 옮겨 적는다."
)

# 힌트가 있을 때만 프롬프트에 붙는 블록. 없으면 프롬프트는 힌트가 생기기 전과 글자 하나까지
# 같다 — 힌트를 안 준 실행이 준 실행과 다른 답을 내는 일이 없어야 한다
_HINT = """
[운영자가 준 단서]
운영자가 브라우저에서 그 자리를 직접 보고 적어 준 것이다. F12 의 `Copy selector` 가 뱉은
경로일 수도 있고, 값이 어디 있는지 설명하는 문장일 수도 있다.

{hint}

이 단서를 쓰는 법:
- **위치를 알려 주는 것**이지 답이 아니다. 받은 경로를 그대로 베껴 쓰지 않는다.
- `css-1a2b3c`, `sc-a1b2c3` 같은 자동 생성 클래스는 사이트를 다시 배포할 때마다 바뀐다.
  `:nth-child(2)` 같은 위치 선택자는 항목이 하나 늘거나 줄면 어긋난다. 둘 다 지금 이 HTML
  에서는 맞지만 다음 배포에 깨진다.
- 그 자리와 그 주변을 HTML 에서 찾아, **거기서 안정적인 것**을 골라라 — 뜻이 있는 클래스명,
  `data-` 로 시작하는 속성, `article > h3 > a` 같은 태그 구조.
- 그런 것이 그 자리에 정말 하나도 없을 때만 받은 경로를 쓴다.
- 단서가 가리키는 자리에 찾는 값이 없으면 억지로 맞추지 않는다. 그 필드는 빈 문자열로 둔다.
"""

# 프롬프트에 실어 보낼 힌트의 상한(문자 수). 자유 입력이라 페이지를 통째로 붙여 넣는 일이
# 생기고, 그러면 정제해서 줄여 둔 HTML 옆에서 힌트가 입력의 대부분을 차지한다
# (`.claude/rules/llm.md`).
MAX_HINT_CHARS = 800

# 프롬프트 전체의 상한. 정제 HTML 두 벌에 지시문과 힌트를 더한 값이고, 위의 두 상한이
# 지켜지는 한 프롬프트가 이 값을 넘지 않는다
MAX_PROMPT_CHARS = 2 * DEFAULT_MAX_CHARS + 8_000

# 지금 이 HTML 에서는 맞지만 다음 배포에 깨지는 모양. 고쳐진 셀렉터에 이것이 남으면 결과에
# 적는다 — 고쳤다고만 적고 넘기면 다음 실패 때 같은 자리를 처음부터 다시 뒤진다
_FRAGILE: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r":nth-(?:child|of-type|last-child)\b"), "위치 선택자(:nth-child 등)"),
    (re.compile(r"\b(?:css|sc|jsx|styles?)-[0-9a-z]{5,}\b", re.IGNORECASE), "자동 생성 클래스"),
)


class SelectorRepairError(RuntimeError):
    """고치기 실패. `reason` 으로 다음 행동이 갈린다.

    | reason | 다음 행동 |
    |---|---|
    | `nothing_to_repair` | 실패한 필드가 없다. 고칠 것이 없으니 호출하지 않는다 |
    | 그 외 | `SelectorGenerationError` 와 같다. 생성 쪽 표를 본다 |
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class SelectorChange:
    """필드 하나가 어떻게 바뀌었는지. 화면이 전/후를 나란히 적는 데 쓴다."""

    name: str
    before: str
    after: str


@dataclass(frozen=True)
class RepairOutcome:
    """고치기 결과. 저장하지 않는다 — 저장은 운영자가 버튼으로 한다.

    `before` 와 `after` 는 **같은 HTML** 에 돌린 판정이다. 그래야 매칭 개수의 차이가 셀렉터
    변화 때문이라고 말할 수 있다.
    """

    selectors: SelectorSet
    before: VerificationReport
    after: VerificationReport
    usage: Usage
    attempts: int
    targets: list[str]
    changes: list[SelectorChange]
    # 고친 뒤에도 남은 실패. `after` 를 실패 판정으로 다시 돌린 결과다 — 억지로 성공으로
    # 만들지 않는다
    unresolved: list[str]
    notes: list[str] = field(default_factory=list)
    # 이 중 실제로 실패였던 것. 힌트가 들어오면 `targets` 는 그보다 넓어진다 — 잘 되는
    # 필드도 "바꿔도 되는 필드" 가 되기 때문이다. 넓힌 자리는 실제로 바뀐 것만 센다
    failed_targets: list[str] = field(default_factory=list)

    @property
    def watched(self) -> list[str]:
        """이번 호출의 결과를 판정할 필드. 실패였던 것과 실제로 바뀐 것."""
        names = list(self.failed_targets)
        return names + [c.name for c in self.changes if c.name not in names]

    @property
    def repaired(self) -> list[str]:
        """고쳐진 필드. 지금은 실패가 아니고, 이번 호출이 실제로 손댄 것이다."""
        remaining = set(self.unresolved)
        return [name for name in self.watched if name not in remaining]

    @property
    def hinted_only(self) -> bool:
        """실패는 없었고 힌트가 가리킨 자리만 고친 호출인가."""
        return not self.failed_targets

    @property
    def ok(self) -> bool:
        return not self.unresolved


async def repair_for_urls(
    list_url: str,
    detail_url: str,
    selectors: SelectorSet,
    *,
    source: PageSource | None = None,
    settings: Settings | None = None,
    client: Any | None = None,
    hint: str = "",
) -> RepairOutcome:
    """저장된 URL 을 다시 가져와 고친다.

    HTML 은 어디에도 보관하지 않으므로(`.claude/rules/data-safety.md`) 고칠 때 다시 가져온다.
    가져오는 것은 공용 fetch 클라이언트이거나 렌더러다 — 어느 쪽인지는 `crawlers.render_mode`
    를 읽는 호출부가 정한다 (`.claude/rules/crawling.md`).
    """
    resolved_source = source or get_fetcher()
    list_html = (await resolved_source.fetch(list_url)).text
    detail_html = ""
    if detail_url.strip():
        detail_html = (await resolved_source.fetch(detail_url)).text
    return await repair_from_html(
        list_html,
        detail_html,
        selectors,
        list_url=list_url,
        detail_url=detail_url,
        settings=settings,
        client=client,
        hint=hint,
    )


async def repair_from_html(
    list_html: str,
    detail_html: str,
    selectors: SelectorSet,
    *,
    list_url: str = "",
    detail_url: str = "",
    settings: Settings | None = None,
    client: Any | None = None,
    hint: str = "",
) -> RepairOutcome:
    """이미 가져온 HTML 로 고친다. 저장된 픽스처로 돌릴 수 있는 경로다.

    `hint` 는 운영자가 브라우저에서 보고 준 단서다. 비워 두면 프롬프트는 힌트가 생기기 전과
    같다. 있어도 검증은 그대로다 — 고친 셀렉터는 같은 HTML 에 다시 돌린다.
    """
    resolved = settings or get_settings()
    has_detail = bool(detail_html.strip())
    before = verify_selectors(selectors, list_html, detail_html)
    trimmed_hint, hint_notes = normalize_hint(hint)

    failed_targets = repair_targets(before, has_detail_html=has_detail)
    # 힌트가 들어오면 대상이 넓어진다. 실행이 성공이어도 잡히는 값이 틀릴 수 있고, 고칠 길이
    # 없으면 운영자는 화면에서 막힌다. 넓혀도 바뀌는 것은 단서가 가리킨 자리뿐이다 —
    # 나머지는 모델이 지금 값을 그대로 옮겨 적고 `_overlay` 가 같은 값을 변경으로 세지 않는다
    targets = (
        repair_targets(before, has_detail_html=has_detail, hinted=True)
        if trimmed_hint
        else failed_targets
    )
    if not targets:
        raise SelectorRepairError(
            "nothing_to_repair",
            "실패한 필드가 없다. 어느 필드가 무엇을 잘못 잡는지 힌트에 적으면 "
            "그 필드를 고친다. 건너뛴 필드는 사이트에 그 항목이 없다는 뜻이라 "
            "고칠 셀렉터가 없다",
        )

    provider, model = _chosen(resolved)
    resolved_client = client or build_client_for(SELECTOR_REPAIR, resolved)
    cleaned_list = clean_html(list_html)
    cleaned_detail = clean_html(detail_html) if has_detail else None
    prompt = build_prompt(
        selectors,
        before,
        targets,
        cleaned_list,
        cleaned_detail,
        list_url,
        detail_url,
        hint=trimmed_hint,
        failed_targets=failed_targets,
    )

    proposal, attempts, usage, extra_notes = await _ask(resolved_client, model, prompt, provider)

    repaired, changes = _overlay(selectors, proposal, targets)
    after = verify_selectors(repaired, list_html, detail_html)
    # 고친 뒤 판정을 대상 고르기와 같은 함수로 다시 돌린다. 두 벌로 판정하면 "고쳤다"고
    # 적힌 필드가 다음 실행에서 다시 실패로 나온다. `hinted` 는 주지 않는다 — 여기서 묻는
    # 것은 "지금 실패인가" 하나다
    remaining = set(repair_targets(after, has_detail_html=has_detail))
    watched = failed_targets + [
        change.name for change in changes if change.name not in failed_targets
    ]
    outcome = RepairOutcome(
        selectors=repaired,
        before=before,
        after=after,
        usage=usage,
        attempts=attempts,
        targets=targets,
        changes=changes,
        unresolved=[name for name in watched if name in remaining],
        failed_targets=failed_targets,
        notes=(
            _notes(cleaned_list, cleaned_detail)
            + hint_notes
            + extra_notes
            + _fragile_notes(changes)
        ),
    )
    logger.info(
        "셀렉터 고치기 model=%s 대상=%s 고쳐짐=%s 남은실패=%s 힌트=%s",
        model,
        ", ".join(targets),
        ", ".join(outcome.repaired) or "없음",
        ", ".join(outcome.unresolved) or "없음",
        f"{len(trimmed_hint)}자" if trimmed_hint else "없음",
    )
    return outcome


def repair_targets(
    report: VerificationReport, *, has_detail_html: bool, hinted: bool = False
) -> list[str]:
    """고칠 필드 이름. 순서는 화면의 표와 같다.

    기본은 실패한 필드뿐이다. `hinted` 면 판정된 필드 전부가 대상이 된다 — 운영자가 힌트로
    "이 필드가 잡는 값이 틀렸다" 고 지적한 경우고, 실패가 아닌 필드를 바꾸는 것이 바로 그
    요청이다. 힌트가 가리키지 않는 필드는 모델이 지금 값을 그대로 옮겨 적고, 같은 값은
    `_overlay` 가 변경으로 세지 않는다.

    `건너뜀` 은 어느 쪽에서도 빠진다. 사이트에 그 항목이 없다는 응답이라 고칠 셀렉터가 없고,
    억지로 채우면 잘못된 값이 공고마다 붙는다.

    상세 HTML 이 없을 때 상세 필드도 빠진다. 볼 페이지가 없어 0개 매칭인 것이지 셀렉터가
    틀린 것이 아니라, 고쳐 봐야 맞는지 확인할 방법이 없다.

    예외가 하나 있다. `list.item` 이 노드를 잡았는데 그 안의 제목·링크·날짜가 **전부** 실패면
    항목 셀렉터도 대상이다(`VerificationReport.list_fields_missing`). 2026-08-22 롯데가 그
    모양이었다 — 계열사 링크 목록을 항목으로 잡아서 항목은 4건 잡히고 그 안은 비어 있었다.
    항목을 그대로 두면 그 안의 셀렉터를 무엇으로 바꿔도 잡을 것이 없다. 셋 중 하나만 실패한
    경우에는 건드리지 않는다 — 그때는 항목이 맞고 그 필드 하나가 틀린 것이다.
    """

    def judged(name: str) -> bool:
        return has_detail_html or not name.startswith("detail.")

    if hinted:
        skipped = set(report.skipped)
        return [
            item.name for item in report.fields if item.name not in skipped and judged(item.name)
        ]

    failed = [name for name in report.failed if judged(name)]
    if failed and report.list_fields_missing and "list.item" not in failed:
        return ["list.item", *failed]
    return failed


def build_prompt(
    selectors: SelectorSet,
    report: VerificationReport,
    targets: list[str],
    cleaned_list: CleanedHtml,
    cleaned_detail: CleanedHtml | None,
    list_url: str = "",
    detail_url: str = "",
    *,
    hint: str = "",
    failed_targets: list[str] | None = None,
) -> str:
    """지금 셀렉터 전부와 실패한 필드의 사유를 함께 넣는다.

    지금 셀렉터를 통째로 주는 것은 모델이 **무엇이 이미 맞는지 알아야 그것을 피해 고르기**
    때문이다. 실패한 필드만 주면 이미 맞는 `list.title` 과 겹치는 셀렉터를 내놓는다.

    `hint` 는 이미 `normalize_hint` 를 지나온 값이다. 비어 있으면 힌트 블록 자체가 붙지
    않는다.
    """
    return _PROMPT.format(
        intro=_INTRO_FAILED if failed_targets else _INTRO_HINTED,
        current=selectors.to_json(),
        failures=_failure_lines(report, targets, failed=set(failed_targets or ())),
        list_url=list_url,
        list_html=cleaned_list.html,
        detail_url=detail_url or "(없음)",
        detail_html=cleaned_detail.html if cleaned_detail else "(상세 페이지를 가져오지 않았다)",
        hint=_HINT.format(hint=hint) if hint else "",
    )


def normalize_hint(hint: str) -> tuple[str, list[str]]:
    """힌트를 상한 안으로 줄이고, 무엇을 했는지 설명과 함께 돌려준다.

    자유 입력이라 페이지를 통째로 붙여 넣는 일이 생긴다. 그러면 정제해서 줄여 둔 HTML 옆에서
    힌트가 입력의 대부분을 차지한다 (`.claude/rules/llm.md`). 앞부분을 남기는 것은 `Copy
    selector` 경로도 설명 문장도 앞이 본론이기 때문이다.

    자른 사실은 결과에 남긴다. 조용히 자르면 운영자는 자기가 준 단서가 다 갔다고 여긴다.
    """
    text = hint.strip()
    if not text:
        return "", []
    if len(text) <= MAX_HINT_CHARS:
        return text, [f"운영자 힌트 {len(text)}자를 함께 보냈다"]
    return (
        text[:MAX_HINT_CHARS],
        [
            f"운영자 힌트가 {len(text)}자라 상한 {MAX_HINT_CHARS}자까지만 보냈다. "
            "뒷부분은 모델이 보지 못했다"
        ],
    )


def _fragile_notes(changes: list[SelectorChange]) -> list[str]:
    """고쳐진 셀렉터가 다음 배포에 깨질 모양이면 그 사실을 결과에 적는다.

    막지는 않는다. 그 자리에 안정적인 것이 정말 없어서 위치로 잡을 수밖에 없는 사이트가 있고,
    깨질 셀렉터라도 지금 값을 가져오는 편이 아무것도 못 가져오는 것보다 낫다. 대신 왜 그것
    밖에 없었는지가 결과에 남아, 다음에 그 필드가 실패하면 여기부터 본다.
    """
    notes: list[str] = []
    for change in changes:
        reasons = [label for pattern, label in _FRAGILE if pattern.search(change.after)]
        if not reasons:
            continue
        notes.append(
            f"{change.name} 의 새 셀렉터가 {'와 '.join(reasons)}에 기대고 있다: "
            f"{change.after} — 그 자리에 뜻이 있는 클래스나 data- 속성이 없었다는 뜻이다. "
            "사이트를 다시 배포하면 깨질 수 있으니 다음에 이 필드가 실패하면 여기부터 본다"
        )
    return notes


def _failure_lines(
    report: VerificationReport, targets: list[str], *, failed: set[str] | None = None
) -> str:
    """필드 이름 + 지금 셀렉터 + 왜 실패했는지. 사유가 있어야 무엇을 바꿀지 정해진다.

    힌트가 들어오면 대상에 **잘 되는 필드도 섞인다**. 그 자리에 적을 것은 실패 사유가 아니라
    지금 무엇을 몇 건 잡고 있는지다 — 잡히는 값이 틀렸다고 지적한 것이 어느 필드인지는
    단서가 말한다. 잘 되는 필드에 실패 사유를 적으면 모델은 멀쩡한 셀렉터를 전부 바꾼다.
    """
    broken = failed if failed is not None else set(targets)
    by_name: dict[str, FieldMatch] = {item.name: item for item in report.fields}
    lines: list[str] = []
    for name in targets:
        item = by_name.get(name)
        if item is None:
            continue
        current = item.selector or "(비어 있음)"
        if name not in broken:
            lines.append(
                f"- {name}: 지금 `{current}` — {item.matches}건을 잡고 있다. 실패가 아니다. "
                "단서가 이 필드를 가리키면 고치고, 아니면 지금 값을 그대로 옮겨 적는다"
            )
            continue
        reason = item.message or _no_message_reason(item)
        lines.append(f"- {name}: 지금 `{current}` — {reason}")
    return "\n".join(lines)


def _no_message_reason(item: FieldMatch) -> str:
    """사유가 비어 있는 대상. `list.item` 이 잡기는 했는데 그 안이 빈 경우가 여기다."""
    if item.ok:
        return (
            f"노드 {item.matches}건을 잡았지만 그 안에서 제목·링크·날짜를 하나도 뽑지 못했다. "
            "공고 목록이 아닌 다른 반복 요소를 잡았을 수 있다"
        )
    return "매칭 0개"


def _chosen(settings: Settings) -> tuple[Provider, str]:
    """셀렉터 고치기가 쓰는 제공자와 모델. 생성과 다른 제공자를 고를 수 있다."""
    try:
        return for_feature(SELECTOR_REPAIR, settings)
    except LlmCallError as exc:
        raise SelectorRepairError(exc.reason, str(exc)) from exc


async def _ask(
    client: Any, model: str, prompt: str, provider: Provider
) -> tuple[SelectorSet, int, Usage, list[str]]:
    """모델에게 묻고 스키마로 검증한다. 재시도 규칙은 생성과 같다 — 깨진 응답만 1회."""
    last_error: SelectorSchemaError | None = None
    last_text = ""
    usage: Usage | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        last_text, usage = await call_model(
            client, model, prompt, attempt, kind="셀렉터 고치기", provider=provider
        )
        try:
            selectors, moved = parse_generated(last_text)
            return selectors, attempt, usage, moved
        except SelectorSchemaError as exc:
            logger.warning(
                "셀렉터 고치기 응답 거절 model=%s attempt=%d reason=%s message=%s",
                model,
                attempt,
                exc.reason,
                exc,
            )
            # 생성과 같은 규칙이다 — 스키마에 없는 이름도 한 번 더 묻고, 옆 묶음에 잘못 둔 칸은
            # `parse_generated` 가 제자리로 옮긴다 (`app/selector/generator.py`)
            last_error = exc

    assert last_error is not None  # 루프는 최소 한 번 돈다
    assert usage is not None

    if last_error.reason == "missing_field":
        # 모양은 맞는데 어떤 자리가 비었다. 통째로 버리지 않는다 — 비어 있는 자리는 `_overlay`
        # 가 원래 값으로 되돌리므로, 고쳐진 필드만 얹고 나머지는 그대로 남는다
        proposal, empty, moved = parse_generated_allowing_empty(last_text)
        return (
            proposal,
            MAX_ATTEMPTS,
            usage,
            [
                *moved,
                f"모델이 비워 둔 필드: {', '.join(empty)}. 그 자리는 원래 셀렉터가 그대로 남는다",
            ],
        )

    reason = "unknown_field" if last_error.reason == "unknown_field" else "unparsable"
    raise SelectorRepairError(
        reason, f"{MAX_ATTEMPTS}회 모두 스키마에 맞지 않았다: {last_error}"
    ) from last_error


def _overlay(
    original: SelectorSet, proposal: SelectorSet, targets: list[str]
) -> tuple[SelectorSet, list[SelectorChange]]:
    """실패한 필드만 새 값으로 갈아 끼운다.

    이 함수가 "잘 되는 필드를 잃지 않는다" 를 보장하는 자리다. 프롬프트에 부탁해 두는 것으로는
    보장이 되지 않는다 — 모델이 맞던 `list.title` 을 다른 값으로 내놓아도 여기서 버린다.

    빈 값도 버린다. 모델이 못 고쳤다는 뜻이고, 원래 셀렉터가 남아 있어야 운영자가 손으로 고칠
    대상이 된다 (`.claude/rules/llm.md`).
    """
    data = original.model_dump()
    proposed = proposal.model_dump()
    changes: list[SelectorChange] = []
    for name in targets:
        section, _, key = name.partition(".")
        if section not in data or key not in data[section]:
            continue
        current = str(data[section][key])
        candidate = str(proposed.get(section, {}).get(key, "")).strip()
        if not candidate or candidate == current:
            continue
        data[section][key] = candidate
        changes.append(SelectorChange(name=name, before=current, after=candidate))
    return SelectorSet(**data), changes


def _notes(cleaned_list: CleanedHtml, cleaned_detail: CleanedHtml | None) -> list[str]:
    """입력을 좁혔거나 잘랐으면 결과에 남긴다 (`.claude/rules/llm.md`)."""
    notes = [f"목록: {note}" for note in cleaned_list.notes()]
    if cleaned_detail is not None:
        notes.extend(f"상세: {note}" for note in cleaned_detail.notes())
    return notes


__all__ = [
    "MAX_HINT_CHARS",
    "MAX_PROMPT_CHARS",
    "RepairOutcome",
    "SelectorChange",
    "SelectorGenerationError",
    "SelectorRepairError",
    "normalize_hint",
    "repair_for_urls",
    "repair_from_html",
    "repair_targets",
]
