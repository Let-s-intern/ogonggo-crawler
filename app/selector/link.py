"""목록 항목에서 상세 URL 을 뽑는다. 뽑히지 않으면 왜 안 되는지를 같이 돌려준다.

파서와 생성 시점 자체 검증이 같은 판정을 쓰기 위한 모듈이다. 두 곳이 각자 판정하면
"검증은 통과했는데 실행은 실패한다"가 생긴다 — 실제로 그렇게 됐다. 한화에서 모델이
`list.link` 로 `h4.recruit-title` 을 골랐고, 검증이 노드 수만 세어 20/20 으로 통과시켰다.

그래서 `list.link` 의 성공 기준은 **노드를 잡았는가**가 아니라 **따라갈 수 있는 URL 이
나오는가**다. `href` 가 없는 요소를 잡았거나 `javascript:`, `#` 뿐이면 노드가 스무 개여도
실패다.

## 두 가지 방식

| `link_template` | 어디서 URL 이 나오는가 |
|---|---|
| 비어 있음 | `link` 셀렉터가 잡은 노드의 `href`. 기존 셀렉터가 전부 이쪽이다 |
| 값이 있음 | 노드의 속성값을 `{속성이름}` 자리에 끼워 만든다 |

두 번째는 `href` 가 `javascript:void(0)` 이고 상세 파라미터가 데이터 속성에 들어 있는
사이트를 위한 것이다. 예를 들어 `data-recuyy`, `data-recutype`, `data-recucls` 를 가진
항목에 이런 템플릿을 준다.

    https://talent.hyundai.com/apply/applyView.hc?recuYy={data-recuyy}&recuType={data-recutype}&recuCls={data-recucls}

템플릿 방식에서 `link` 가 비어 있으면 항목 노드 자신의 속성을 읽는다. 값이 있으면 항목 안에서
그 셀렉터가 잡은 첫 노드의 속성을 읽는다.

속성값은 공백처럼 URL 을 깨는 문자만 인코딩하고 `/`, `?`, `=`, `&` 는 그대로 둔다. 파라미터
자리에 들어갈 짧은 값이 주 용도지만, 경로 조각이 통째로 들어 있는 속성도 있기 때문이다.

**항목에 없는 속성은 만들어 내지 않는다.** 속성 하나라도 비면 그 항목은 실패이고, 어느 속성이
없었는지를 사유에 적는다. 상세 파라미터가 DOM 에 아예 없는 사이트는 이 방식으로도 풀리지
않는다 — 그것은 셀렉터를 더 넓혀서 풀 문제가 아니라 사이트를 등록할 수 없다는 사실이다.

절대 URL 로 만드는 것은 여기서 하지 않는다. 상대경로는 그대로 돌려주고, 목록 URL 과 합치는
것은 파서의 몫이다. 조립된 절대 URL 이든 상대경로든 마지막에는 공용 fetch 클라이언트가 다시
http(s) 인지 본다 (`.claude/rules/crawling.md`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from bs4.element import Tag

from app.selector.schema import ListSelectors

# 크롤러가 따라갈 수 있는 스킴. 이 밖은 링크가 아니라 페이지 안에서 도는 동작이다.
FOLLOWABLE_SCHEMES: tuple[str, ...] = ("http", "https")

# `{data-recuyy}` 처럼 속성 이름을 그대로 적는다. HTML 속성 이름에 쓰이는 문자만 받는다.
# `{onclick|arg1}` 은 그 속성 안의 첫 번째 따옴표 인자다 — 아래 `ARG_MARK` 를 본다
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_:.-]*)(\|arg\d+)?\}")

# 속성값이 JS 호출인 사이트를 위한 표기. 두산은 `onclick="goDetail(\'1000361539\', ...)"` 이고
# 네이버는 `onclick="show(\'30005276\')"` 라, 공고 번호가 속성값 전체가 아니라 그 안의 인자다.
# `{onclick|arg1}` 은 그중 첫 번째를 뜻한다
ARG_MARK = "|arg"
_JS_ARG = re.compile(r"'([^']*)'|\"([^\"]*)\"")
# 따옴표 없는 숫자 인자. 첫 호출의 괄호 안만 본다 — 카페스 `onclick="view(83); return false;"`
_BARE_CALL = re.compile(r"\(([^()]*)\)")
_BARE_NUMBER = re.compile(r"^\d+$")

# 속성값을 URL 에 끼울 때 그대로 두는 문자. 나머지는 퍼센트 인코딩한다
_KEEP_IN_URL = "/?:=&%+#"


@dataclass(frozen=True)
class LinkResult:
    """항목 하나의 상세 링크. `reason` 이 비어 있을 때만 `url` 이 값이다."""

    url: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.reason


def resolve_link(node: Tag, selectors: ListSelectors) -> LinkResult:
    """항목 노드 하나에서 상세 URL 을 뽑는다.

    셀렉터 문법 오류는 `SelectorSyntaxError` 로 그대로 올라간다. 호출부가 그 실패를 필드
    문법 오류로 따로 적기 때문에 여기서 사유 문자열로 바꾸지 않는다.
    """
    if selectors.link_template:
        return _from_attributes(node, selectors)

    if not selectors.link:
        return LinkResult(reason="list.link 셀렉터가 비어 있다. 목록에 상세 링크가 없다는 응답이다")

    target = _find(node, selectors.link)
    if target is None:
        return LinkResult(reason="list.link 셀렉터가 항목의 안에서도 밖에서도 노드를 찾지 못했다")

    href = target.get("href")
    if not isinstance(href, str) or not href.strip():
        return LinkResult(
            reason=f"`{selectors.link}` 이 잡은 <{target.name}> 에 href 가 없다. "
            "a 태그가 아닌 요소를 골랐을 수 있다"
        )

    href = href.strip()
    if not followable(href):
        return LinkResult(reason=f"따라갈 수 있는 링크가 아니다: {href}")
    return LinkResult(url=href)


# 항목 밖으로 몇 대까지 올라가 볼 것인가. 카드를 감싸는 `a` 는 바로 위이거나 한두 대 위다.
# 무한정 올라가면 목록 전체를 감싼 링크나 헤더의 링크를 잡아 모든 항목이 같은 주소가 된다
_ANCESTOR_LIMIT = 3


def _find(node: Tag, selector: str) -> Tag | None:
    """항목에서 링크 노드를 찾는다. 안, 자기 자신, 그다음 바깥 순서다.

    **링크가 항목을 감싸고 있는 사이트가 있다.** 뤼튼이 그렇다 — `a[data-testid="공고_아이템"]`
    가 `li` 를 감싸고, 항목으로 잡히는 것은 안쪽 `li` 다. `node.select()` 는 자손만 뒤지므로
    이 구조에서 링크를 영원히 찾지 못한다. React 로 만든 목록에서 흔한 모양이다.

    가까운 쪽부터 본다. 안에 있으면 그것이 그 항목의 링크이고, 밖에 있으면 가장 가까운 조상이
    그 항목을 감싼 것이다. `_ANCESTOR_LIMIT` 를 두는 것은 끝까지 올라가면 목록 전체를 감싼
    링크를 잡아 스물여덟 항목이 같은 주소를 갖게 되기 때문이다.
    """
    found = node.select(selector)
    if found:
        return found[0]

    if node.css.match(selector):
        return node

    for depth, parent in enumerate(node.parents):
        if depth >= _ANCESTOR_LIMIT:
            break
        if parent.name and parent.css.match(selector):
            return parent
    return None


def _from_attributes(node: Tag, selectors: ListSelectors) -> LinkResult:
    """`link_template` 의 `{속성이름}` 자리를 노드의 속성값으로 채운다."""
    template = selectors.link_template
    names = [f"{name}{mark}" for name, mark in PLACEHOLDER.findall(template)]
    if not names:
        return LinkResult(
            reason=f"link_template 에 `{{속성이름}}` 자리가 없다: {template}",
        )

    if selectors.link:
        found = _find(node, selectors.link)
        if found is None:
            return LinkResult(
                reason="list.link 셀렉터가 항목의 안에서도 밖에서도 노드를 찾지 못했다"
            )
        target = found
    else:
        # 셀렉터가 없으면 항목 노드 자신의 속성을 읽는다
        target = node

    values: dict[str, str] = {}
    for name in names:
        value = _attribute(target, name)
        if not value:
            return LinkResult(
                reason=f"<{target.name}> 에 `{name}` 속성이 없다. "
                "상세 파라미터가 이 항목에 들어 있지 않다"
            )
        values[name] = quote(value, safe=_KEEP_IN_URL)

    url = PLACEHOLDER.sub(lambda match: values[f"{match.group(1)}{match.group(2) or ''}"], template)
    if not followable(url):
        return LinkResult(reason=f"조립한 URL 이 http(s) 가 아니다: {url}")
    return LinkResult(url=url)


def _attribute(node: Tag, spec: str) -> str:
    """속성값을 문자열로. `<이름>|arg<n>` 이면 그 속성 안의 n 번째 인자를 꺼낸다.

    `class` 처럼 목록으로 오는 속성은 공백으로 잇는다.
    """
    name, mark, index = spec.partition(ARG_MARK)
    raw = node.get(name)
    if isinstance(raw, list):
        value = " ".join(str(part) for part in raw).strip()
    elif isinstance(raw, str):
        value = raw.strip()
    else:
        return ""
    if not mark:
        return value
    return js_argument(value, int(index)) if index.isdigit() else ""


def js_argument(value: str, index: int) -> str:
    """`goDetail(\'1000361539\', \'C_REC_MGT_04\')` 의 n 번째 따옴표 인자. 없으면 빈 문자열.

    JS 를 실행하지 않는다. 따옴표로 묶인 값만 읽는 것이라 계산식이나 변수는 잡히지 않고,
    그때는 빈 값이 되어 그 항목이 실패로 남는다 — 틀린 주소를 만들지 않는다.
    """
    args = [first or second for first, second in _JS_ARG.findall(value)]
    if not args:
        # 따옴표 인자가 하나도 없을 때만 숫자 인자를 센다. 따옴표 인자가 있는 호출에서 숫자까지
        # 세면 이미 저장된 `{onclick|arg2}` 의 번호가 밀린다
        call = _BARE_CALL.search(value)
        if call is not None:
            parts = [part.strip() for part in call.group(1).split(",")]
            args = [part for part in parts if _BARE_NUMBER.match(part)]
    return args[index - 1].strip() if 0 < index <= len(args) else ""


def followable(url: str) -> bool:
    """크롤러가 따라갈 수 있는 URL 인가.

    스킴이 없으면 상대경로이고, 목록 URL 과 합치면 http(s) 가 되므로 통과시킨다.
    `#` 로 시작하는 것은 같은 페이지 안의 앵커라 상세로 가지 않는다.
    """
    value = url.strip()
    if not value or value.startswith("#"):
        return False
    scheme = urlsplit(value).scheme
    return not scheme or scheme in FOLLOWABLE_SCHEMES
