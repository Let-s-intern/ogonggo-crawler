"""받은 값에 원문의 근거가 있는지 그 자리에서 본다.

`.claude/rules/llm.md` 는 모델이 낸 것을 그 자리에서 돌려 보라고 한다. 셀렉터는 HTML 에
돌려 보면 되지만 분류에는 돌릴 것이 없다 — 대신 **원문에 근거가 있는지** 를 본다.

근거가 없는 사실 값은 버린다. 지어낸 값은 소비 측이 그대로 사실로 노출하고, 빈 칸보다 나쁘다
(`.claude/tasks/memos/보류/llm-classify/prd-llm-classify.md`).

## 돌려 보는 곳은 제목과 모델에게 보낸 글이다

**분류에 보낸 그 값에 그대로 돌려 본다.** 보낸 것은 상세 원문이고, 원문이 없는 건에서만
본문이다 (`app/classify/store.py`). 둘이 어긋나면 — 원문을 보내 놓고 본문에 돌려 보면 —
본문 밖의 이름표 값에서 옳게 뽑은 칸이 통째로 버려진다. 근무지와 고용형태가 그 자리다
(`.claude/site-recipes/source-text-container.md`).

제목은 별도로 더한다. 본문만이었다가 직무가 들어오면서 더해졌다 — `position_name` 은 제목에서
옮기는 값이라 본문에만 돌려 보면 **맞게 뽑은 값이 통째로 버려진다.** 2026-08-28 에 열한
사이트 픽스처로 쟀더니 제목이 직무를 말하는 곳 아홉 중 본문이 같은 글자를 되풀이하는 곳은
셋뿐이었고, 나머지 여섯이 전부 버려졌다 (`tests/test_job_role_source.py`).

칸마다 볼 곳을 가르지 않고 **한 덩어리로 본다.** 가르면 칸이 늘 때마다 어느 칸을 어디에 돌려
보는지가 늘고, 그 표가 프롬프트의 칸 설명과 갈린다. 대신 제목 한 줄이 넓어진 만큼 느슨해진다
— 제목을 그대로 옮겨 적은 한 줄짜리 값이 다른 칸에서도 살아남는다. 프롬프트가 제목을
`position_name` 말고 어느 칸에도 넣지 말라고 적는 자리가 거기다 (`app/classify/classifier.py`).

## 칸에 따라 근거가 다르다

**뽑는 칸**은 값 자체가 원문에 있어야 한다. 원문 글자를 그대로 옮기는 칸이라 그렇다.

**판정하는 칸**은 값이 원문에 글자로 있을 필요가 없다. `백엔드 개발자 채용` 어디에도
"개발·IT" 라고 적혀 있지 않다. 고른 값이 **닫힌 목록 안**인지만 보고, 목록 밖이면 버린다.

판정 칸의 근거 문장은 원문에서 찾으면 남기고, 찾지 못해도 **값은 남긴다** (2026-09-14 결정).
판정 칸은 오공고가 반드시 받는 값이라, 비워 두면 오공고가 기본값을 지어내게 된다 — 그보다 공고를
읽은 AI 의 판단이 낫다. 대신 근거가 없는 판정은 검수 화면이 `근거 없음` 으로 보여 사람이 먼저
보게 한다. 제목이 근거일 수 있다 — `[채용연계형 인턴]` 은 고용형태의 근거이다.

**숫자 칸**(최소 경력 연수)은 사실 값이라 근거 문장이 원문에 없으면 버린다. 숫자가 아닌 답도
버린다.

## 무엇을 같다고 보는가

비교 전에 양쪽에서 공백과 글머리표·구두점을 걷어내고 소문자로 맞춘다. 원문은 줄바꿈과
글머리표가 사이트마다 다르고, 같은 문장을 옮겨 적기만 해도 `- ` 가 `• ` 로 바뀐다. 거기서
어긋난 것을 지어냈다고 하면 멀쩡한 값이 통째로 버려진다.

**느슨한 것은 비교뿐이고 판정은 엄격하다.** 값이 여러 줄이면 줄마다 따로 보고, 한 줄이라도
원문에서 찾지 못하면 그 칸을 통째로 버린다. 절반만 사실인 값은 읽는 쪽이 어디까지 믿어야
할지 알 수 없다.

글자를 걷어낸 뒤 남는 것이 한 글자 이하인 줄은 세지 않는다. `-` 하나짜리 줄이나 `1.` 같은
번호는 원문 어디에나 있어서 검사가 되지 않는다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from app import regions, taxonomy
from app.classify.schema import (
    EXTRACT_FIELDS,
    INDUSTRY,
    JOB_FIELD,
    JOB_ROLE,
    JUDGE_CHOICES,
    JUDGE_FIELDS,
    NUMBER_FIELDS,
    POSTING_TITLE,
    REGION,
)

# 비교에서 지우는 글자. 공백, 글머리표, 구두점, 괄호, 따옴표다. 뜻을 나르는 글자는 남는다
_NOISE = re.compile(r"[\s·•·◦○●□■▪▶▷–—\-*_.,;:!?()\[\]{}<>\"'`~/\\|]+")

# 이보다 짧아지는 줄은 검사 대상이 아니다
_MIN_LENGTH = 2

# 숫자 칸이 받는 모양. 경력 연수는 두 자리를 넘지 않는다
_NUMBER = re.compile(r"\d{1,2}")


# 버린 이유. 가르는 것은 고칠 자리가 다르기 때문이다 — 첫째는 프롬프트의 "그대로 옮겨라" 가 안
# 먹은 것이고, 둘째는 목록이 좁은 것이고, 셋째는 읽지 않고 적은 것이고, 넷째는 답의 모양이 틀린
# 것이다.
#
# 문장이 두 번 바뀌었다. "본문에 없다" 에서 검사가 제목까지 보게 되어 "제목에도 본문에도"
# 가 됐고, 보내는 값이 상세 원문이 되면서 "본문" 이 틀린 말이 됐다 — 원문으로 돌린 건에서
# 본문에 없다고 적으면 운영자가 본문만 들여다보다 값이 어디서 왔는지를 놓친다. `보낸 글` 은
# 원문이거나, 원문이 없는 건에서 본문이다 (`app/classify/store.py`)
NOT_IN_SOURCE = "제목에도 보낸 글에도 없다"
NOT_IN_LIST = "목록 밖이다"
# 직군·직무를 버린 뒤 `기타` 로 채웠을 때 사유 뒤에 붙인다
ETC_FILLED = " — 기타로 뒀다"
NO_EVIDENCE = "근거 문장이 제목에도 보낸 글에도 없다"
NOT_A_NUMBER = "숫자가 아니다"


@dataclass(frozen=True)
class Grounded:
    """근거 검사를 지난 결과.

    `dropped` 는 버린 칸 이름이고 `reasons` 는 그 이유다. 비어 있는 것이 정상이고, 값이
    있으면 실행 기록에 남는다 — 모델이 무엇을 지어냈는지는 세어 봐야 알 수 있다.

    `evidence` 는 원문에서 찾은 근거 문장이다. 판정 칸인데 여기 없으면 근거 없이 고른 값이고,
    검수 화면이 그것을 `근거 없음` 으로 보인다.
    """

    fields: dict[str, str]
    evidence: dict[str, str] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)


def loose(text: str) -> str:
    """비교용 모양. 공백·글머리표·구두점을 걷어내고 소문자로 맞춘다."""
    return _NOISE.sub("", text).lower()


def loose_with_positions(text: str) -> tuple[str, list[int]]:
    """`loose()` 와 같은 비교용 모양에, 남은 글자마다 원문에서의 위치를 붙인다.

    찾기는 느슨하게 하고 저장은 원문 그대로 하려고 쓴다 (`app/classify/pieces.py`). 느슨한
    모양에서 찾은 구간을 원문 구간으로 되돌리려면 글자마다 어디서 왔는지 알아야 한다.
    """
    chars: list[str] = []
    positions: list[int] = []
    for index, char in enumerate(text):
        if _NOISE.fullmatch(char):
            continue
        for lowered in char.lower():
            chars.append(lowered)
            positions.append(index)
    return "".join(chars), positions


def drop_exact_repeat(value: str) -> str:
    """모델이 옮긴 문단 전체를 한 칸 안에서 통째로 두 번 반복해 낸 것을 한 번으로 접는다.

    관찰된 실패 패턴이다(2026-08-29, 토스뱅크 공고): 원문에는 한 번만 있는 문단이
    `responsibilities`·`recruitment_notice` 같은 칸에서 줄 단위로 완전히 똑같은 절반 두 개로 나온다
    — 입력이 두 번 들어간 것이 아니라 응답 자체가 반복된 것이다(작고 빠른 모델에서 흔한 디코딩
    반복). 줄 목록을 정확히 반으로 나눴을 때 앞뒤가 완전히 같을 때만 뒤를 버린다 — 요약도 재작성도
    아니고, 정확히 같은 반복만 걷어낸다. 어긋나면 손대지 않는다.
    """
    lines = value.split("\n")
    if len(lines) < 2 or len(lines) % 2 != 0:
        return value
    half = len(lines) // 2
    if lines[:half] == lines[half:]:
        return "\n".join(lines[:half])
    return value


def in_body(value: str, body: str) -> bool:
    """값의 모든 줄이 그 글 안에 있는지. 빈 값은 볼 것이 없어 True 다."""
    return not missing_lines(value, body)


def missing_lines(value: str, body: str) -> list[str]:
    """그 글에서 찾지 못한 줄. 빈 목록이면 그 값은 전부 안에 있다."""
    haystack = loose(body)
    missing: list[str] = []
    for line in value.splitlines():
        needle = loose(line)
        if len(needle) < _MIN_LENGTH:
            continue
        if needle not in haystack:
            missing.append(line.strip())
    return missing


def _quote_in(fields: Mapping[str, str], name: str, source: str) -> str:
    """그 칸의 근거 문장이 원문에 있으면 그 문장, 없거나 비었으면 빈 문자열."""
    quote = fields.get(f"{name}_evidence", "").strip()
    if not quote or missing_lines(quote, source):
        return ""
    return quote


def _ground_judged_field(
    name: str,
    choices: tuple[str, ...],
    fields: Mapping[str, str],
    source: str,
    *,
    kept: dict[str, str],
    dropped: list[str],
    reasons: dict[str, str],
    evidence: dict[str, str],
) -> None:
    """판정 칸 하나. 목록 안이면 남기고, 근거 문장은 원문에서 찾았을 때만 함께 남긴다.

    직무 분류(`job_field`/`job_role`)도 이 경로를 탄다 — 다른 점은 `choices` 가
    `JUDGE_CHOICES` 처럼 고정 상수가 아니라 호출 시점의 `job_taxonomy` 표에서 온다는
    것뿐이다.
    """
    value = fields.get(name, "").strip()
    if not value:
        # 스키마가 반드시 고르게 하지만, 스키마를 통과하지 않는 경로가 있다. 고르지 않은 것은
        # 버린 것이 아니라 셀 것이 없다
        kept[name] = ""
        return
    if value not in choices:
        # 스키마의 enum 이 이미 막지만, 스키마를 통과하지 않는 경로(손으로 넣은 응답,
        # 모델이 enum 을 무시한 경우)가 남아 있다. 목록 밖 값이 한 번 들어오면 그
        # 칸으로 거르는 소비 측이 조용히 그 건을 놓친다
        kept[name] = ""
        dropped.append(name)
        reasons[name] = NOT_IN_LIST
        return
    kept[name] = value
    quote = _quote_in(fields, name, source)
    if quote:
        evidence[name] = quote


def _ground_regions(
    fields: Mapping[str, str],
    source: str,
    *,
    kept: dict[str, str],
    dropped: list[str],
    reasons: dict[str, str],
    evidence: dict[str, str],
) -> None:
    """근무지. 큰 지역 목록 안의 이름만 남기고 목록 순서로 잇는다 (2026-09-21 결정).

    스키마의 enum 이 이미 막지만, 스키마를 통과하지 않는 경로(예전처럼 원문 조각으로 답한 응답,
    손으로 넣은 응답)가 남아 있다. 목록 밖 이름이 하나라도 있으면 버린 기록을 남기고, 목록 안
    이름은 그대로 둔다 — 여러 곳 중 하나가 틀렸다고 나머지를 버리지 않는다.
    """
    picked = regions.split(fields.get(REGION, ""))
    kept[REGION] = regions.join(picked)
    if any(name not in regions.names() for name in picked):
        dropped.append(REGION)
        reasons[REGION] = NOT_IN_LIST
    if kept[REGION]:
        quote = _quote_in(fields, REGION, source)
        if quote:
            evidence[REGION] = quote


def _ground_number_field(
    name: str,
    fields: Mapping[str, str],
    source: str,
    *,
    kept: dict[str, str],
    dropped: list[str],
    reasons: dict[str, str],
    evidence: dict[str, str],
) -> None:
    """숫자 칸 하나. 숫자이고 근거 문장이 원문에 있을 때만 남긴다. 앞의 0 은 뗀다."""
    value = fields.get(name, "").strip()
    if not value:
        kept[name] = ""
        return
    if not _NUMBER.fullmatch(value):
        kept[name] = ""
        dropped.append(name)
        reasons[name] = NOT_A_NUMBER
        return
    quote = _quote_in(fields, name, source)
    if not quote:
        # 사실 값이다. 읽고 적은 것이 아니면 지어낸 것이다
        kept[name] = ""
        dropped.append(name)
        reasons[name] = NO_EVIDENCE
        return
    kept[name] = str(int(value))
    evidence[name] = quote


def ground(
    fields: Mapping[str, str],
    body: str,
    title: str = "",
    *,
    taxonomy_choices: Mapping[str, tuple[str, ...]] | None = None,
) -> Grounded:
    """근거가 없는 칸을 버린다. 버린 칸 이름과 이유를 함께 돌려준다.

    받는 것은 응답 전체(뽑는 칸, 판정 칸, 숫자 칸, 근거 문장)이고, 돌려주는 `fields` 는
    `normalized_jobs` 로 갈 칸이다. 근거 문장은 컬럼이 아니라 `evidence` 로 따로 나간다.

    `body` 는 **모델에게 보낸 그 글이다.** 원문이거나, 원문이 없는 건에서 본문이다. 부르는
    쪽이 보낸 것과 다른 값을 여기 넘기면 멀쩡한 칸이 버려진다 (`app/classify/classifier.py`).

    `title` 을 주지 않으면 보낸 글만 본다. 그것이 옛 동작이고, `position_name` 만 그 상태에서
    거의 전부 버려진다 — 부르는 쪽은 제목을 같이 넘긴다.

    `taxonomy_choices` 는 `{"job_field": (...), "job_role": (...), "industry": (...)}`
    모양이다. 그 호출이
    직무 분류를 물었을 때만 준다 — 주지 않으면 이 둘은 아예 보지 않는다(호출이 그 두 필드를
    묻지 않았으면 응답에도 없다). **대분류가 버려지면 소분류도 함께 비운다** — 대분류 없이
    소분류만 있는 상태는 만들지 않는다(PRD `job-taxonomy` 2절).
    """
    # 돌려 보는 곳. 제목과 보낸 글을 한 덩어리로 본다
    source = f"{title}\n{body}"
    kept: dict[str, str] = {}
    dropped: list[str] = []
    reasons: dict[str, str] = {}
    evidence: dict[str, str] = {}

    for name in EXTRACT_FIELDS:
        value = drop_exact_repeat(fields.get(name, "").strip())
        if not value:
            kept[name] = ""
            continue
        if missing_lines(value, source):
            kept[name] = ""
            dropped.append(name)
            reasons[name] = NOT_IN_SOURCE
            continue
        kept[name] = value

    # 공고 제목은 짓는 칸이라 원문에 돌려 보지 않는다. 한 줄로만 맞춘다 (2026-09-18 결정)
    kept[POSTING_TITLE] = " ".join(fields.get(POSTING_TITLE, "").split())

    for name in JUDGE_FIELDS:
        _ground_judged_field(
            name,
            JUDGE_CHOICES[name],
            fields,
            source,
            kept=kept,
            dropped=dropped,
            reasons=reasons,
            evidence=evidence,
        )

    for name in NUMBER_FIELDS:
        _ground_number_field(
            name, fields, source, kept=kept, dropped=dropped, reasons=reasons, evidence=evidence
        )

    _ground_regions(fields, source, kept=kept, dropped=dropped, reasons=reasons, evidence=evidence)

    if taxonomy_choices:
        for name in (JOB_FIELD, JOB_ROLE, INDUSTRY):
            if name not in taxonomy_choices:
                kept[name] = ""
                continue
            _ground_judged_field(
                name,
                taxonomy_choices[name],
                fields,
                source,
                kept=kept,
                dropped=dropped,
                reasons=reasons,
                evidence=evidence,
            )
        if not kept.get(JOB_FIELD) and kept.get(JOB_ROLE):
            kept[JOB_ROLE] = ""
        # 직무는 고른 직군 아래 목록 안에서만 받는다 (2026-09-17 결정). 다른 직군의 직무도 전체
        # 소분류 목록에는 있어서 위 검사를 지난다
        under = taxonomy_choices.get(f"{JOB_ROLE}:{kept.get(JOB_FIELD, '')}")
        if kept.get(JOB_ROLE) and under is not None and kept[JOB_ROLE] not in under:
            kept[JOB_ROLE] = ""
            if JOB_ROLE not in dropped:
                dropped.append(JOB_ROLE)
            reasons[JOB_ROLE] = NOT_IN_LIST
        _fill_etc(kept, taxonomy_choices, reasons)

    return Grounded(fields=kept, evidence=evidence, dropped=dropped, reasons=reasons)


def _fill_etc(
    kept: dict[str, str], choices: Mapping[str, tuple[str, ...]], reasons: dict[str, str]
) -> None:
    """목록에서 고르지 못해 빈 직군·직무를 `기타` 로 채운다 (2026-09-21 결정).

    빈 칸으로 보내면 오공고에서 그 공고가 어느 직군으로도 걸러지지 않는다. 버린 기록은 그대로
    남는다 — 모델이 무엇을 냈고 왜 버렸는지는 `dropped_fields` 로 알 수 있어야 한다.

    | 무엇이 비었나 | 채우는 값 |
    |---|---|
    | 직군 | 직군 `기타`, 직무 `기타` |
    | 직무만 (목록 밖이거나 다른 직군의 직무) | 그 직군의 `기타` 직무 (`기타IT·개발` 등) |

    직군을 묻지 않은 호출(표가 비었다)은 건드리지 않는다. 직무 목록이 없는 표면 직무도 건드리지
    않는다 — 고를 목록이 없던 칸이다.
    """
    if JOB_FIELD not in choices:
        return
    asks_role = JOB_ROLE in choices
    if not kept.get(JOB_FIELD):
        kept[JOB_FIELD] = taxonomy.ETC
        if asks_role:
            kept[JOB_ROLE] = taxonomy.ETC
        _mark_etc(reasons, (JOB_FIELD, JOB_ROLE))
        return
    if asks_role and not kept.get(JOB_ROLE):
        under = choices.get(f"{JOB_ROLE}:{kept[JOB_FIELD]}", ())
        kept[JOB_ROLE] = taxonomy.etc_role(under)
        _mark_etc(reasons, (JOB_ROLE,))


def _mark_etc(reasons: dict[str, str], names: tuple[str, ...]) -> None:
    """버린 칸의 사유 뒤에 기타로 채웠다고 적는다. 버리지 않고 빈 칸이었으면 적을 것이 없다."""
    for name in names:
        if name in reasons and not reasons[name].endswith(ETC_FILLED):
            reasons[name] = f"{reasons[name]}{ETC_FILLED}"
