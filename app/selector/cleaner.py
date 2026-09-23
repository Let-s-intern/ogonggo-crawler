"""LLM 에 보낼 HTML 을 정제하고 좁힌다.

`.claude/rules/llm.md` 가 정한 것을 그대로 담는다.

- 원본 페이지를 그대로 보내지 않는다. `script`, `style`, `svg`, 주석, 인라인 이벤트 핸들러를 뺀다
- 반복 리스트는 형제 몇 개만 남긴다. 60개나 4개나 구조 신호는 같고 비용만 다르다
- 상한을 넘으면 무작정 자르지 않고 반복 영역으로 먼저 좁힌다. 좁혔다는 사실은 결과에 남는다

여기서 하는 일은 "무엇을 보낼지 줄이는 것"뿐이다. 셀렉터 판단은 하지 않는다.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from bs4 import BeautifulSoup, Comment, Tag

# 한 번의 생성 요청에 실어 보낼 정제 HTML 의 상한(문자 수). 3만에서 6만으로 올렸다 — 긴 클래스
# 이름을 쓰는 사이트(나인하이어)는 반복을 줄여도 4만 5천 자였고, 넘치면 가장 많이 반복하는 영역으로
# 좁히는데 그 영역이 공고가 아닌 안내 문단이라 목록이 통째로 빠졌다 (슈피겐, 2026-09-22)
DEFAULT_MAX_CHARS = 60_000

# 반복 영역에서 남기는 형제 수. 3~4개면 구조가 드러난다.
DEFAULT_KEEP_SIBLINGS = 4

# 반복으로 볼 최소 형제 수. 이보다 적으면 리스트가 아니라 그냥 마크업이다.
_MIN_REPEAT = 3
# 반복 항목끼리 안에 든 요소 수가 이 비율 안에서 비슷해야 한다 (`_alike`)
_SIZE_TOLERANCE = 0.4

_DROP_TAGS = ("script", "style", "svg", "noscript", "iframe", "template")
_ON_ATTR = re.compile(r"^on", re.IGNORECASE)
_BLANK_LINES = re.compile(r"\n\s*\n+")
# 속성 값이 이보다 길면 앞부분만 남긴다. 셀렉터가 보는 것은 속성이 있는지와 앞부분이고, 긴 값은
# 붙여 넣은 편집기 데이터나 base64 이미지다. 이노션 상세 한 건의 `data-buffer`(피그마 데이터)가
# 14만 자라 입력 상한을 혼자 다 쓰고 본문이 잘렸다 (2026-09-22)
_MAX_ATTR_CHARS = 500
_KEEP_ATTR_CHARS = 100
_NARROW_FALLBACKS = ("main", "article", "[role=main]", "body")


@dataclass(frozen=True)
class CleanedHtml:
    """정제 결과와, 원본에서 무엇을 덜어냈는지.

    `narrowed` 와 `truncated` 는 생성 응답까지 그대로 올라가야 한다. 좁힌 입력으로 만든 셀렉터를
    페이지 전체를 보고 만든 것처럼 다루면, 잘라낸 쪽에서 깨졌을 때 원인을 찾을 수 없다.
    """

    html: str
    original_chars: int
    removed_siblings: int
    narrowed: bool
    truncated: bool

    @property
    def chars(self) -> int:
        return len(self.html)

    def notes(self) -> list[str]:
        """응답에 실을 한국어 설명. 덜어낸 것이 없으면 빈 목록."""
        messages: list[str] = []
        if self.removed_siblings:
            messages.append(f"반복 영역에서 형제 {self.removed_siblings}개를 덜어냈다")
        if self.narrowed:
            messages.append("상한을 넘어 반복 영역으로 입력을 좁혔다")
        if self.truncated:
            messages.append("좁힌 뒤에도 상한을 넘어 뒷부분을 잘랐다")
        return messages


def clean_html(
    html: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    keep_siblings: int = DEFAULT_KEEP_SIBLINGS,
) -> CleanedHtml:
    """페이지 하나를 LLM 입력용으로 줄인다."""
    if keep_siblings < 1:
        raise ValueError("keep_siblings 는 1 이상이어야 한다")

    soup = BeautifulSoup(html, "html.parser")
    _strip_noise(soup)
    region, removed = _sample_repeats(soup, keep_siblings)

    text = _compact(str(soup))
    narrowed = False
    truncated = False

    if len(text) > max_chars:
        target = region if region is not None else _fallback_region(soup)
        if target is not None:
            narrowed_text = _compact(str(target))
            # 좁힌 결과가 더 크지 않을 때만 바꾼다
            if len(narrowed_text) < len(text):
                text = narrowed_text
                narrowed = True

    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    return CleanedHtml(
        html=text,
        original_chars=len(html),
        removed_siblings=removed,
        narrowed=narrowed,
        truncated=truncated,
    )


def _strip_noise(soup: BeautifulSoup) -> None:
    """실행 코드와 표현용 마크업, 주석, 인라인 핸들러를 뺀다."""
    for comment in soup.find_all(string=lambda node: isinstance(node, Comment)):
        comment.extract()

    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()

    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        for name in [name for name in tag.attrs if _ON_ATTR.match(name)]:
            del tag[name]
        if tag.has_attr("style"):
            del tag["style"]
        for name, value in list(tag.attrs.items()):
            if isinstance(value, str) and len(value) > _MAX_ATTR_CHARS:
                tag[name] = value[:_KEEP_ATTR_CHARS]


def _sample_repeats(soup: BeautifulSoup, keep: int) -> tuple[Tag | None, int]:
    """같은 태그 형제가 `keep` 개를 넘으면 앞의 `keep` 개만 남긴다.

    가장 크게 반복하던 영역의 루트와, 덜어낸 형제 수를 돌려준다. 루트는 나중에 입력을 좁힐 때
    쓴다 — 페이지에서 공고 목록일 가능성이 가장 높은 자리다.
    """
    removed = 0
    best_count = 0
    best_region: Tag | None = None

    # 문서 순서(위에서 아래)로 돌아 큰 영역을 먼저 줄인다.
    for parent in list(soup.find_all(True)):
        if not isinstance(parent, Tag) or parent.decomposed:
            continue

        # 태그와 클래스가 같아야 반복이다. 태그만 보면 페이지 빌더의 섹션 블록(div 여러 개)이
        # 반복으로 잡혀 다섯 번째 이후 섹션이 통째로 지워진다 — recruiter.co.kr 은 공고 목록이
        # 다섯 번째 섹션이라 목록이 사라졌다 (2026-09-22)
        groups: dict[tuple[str, tuple[str, ...]], list[Tag]] = {}
        for child in parent.find_all(recursive=False):
            groups.setdefault((child.name, tuple(child.get("class") or ())), []).append(child)

        for (name, _), children in groups.items():
            if len(children) < _MIN_REPEAT or not _alike(children):
                continue
            if len(children) > best_count and name not in ("option", "meta", "link"):
                best_count = len(children)
                best_region = parent
            for extra in children[keep:]:
                extra.decompose()
                removed += 1

    return best_region, removed


def _alike(children: list[Tag]) -> bool:
    """형제들이 서로 닮았는가. 닮아야 반복 목록이다.

    겉의 태그와 클래스가 같아도 안이 제각각이면 목록이 아니라 페이지 빌더의 섹션이다. greetinghr
    실측(2026-09-22): 같은 클래스의 섹션 여덟 개 중 일곱 번째가 공고 목록이라, 다섯 번째부터
    지우면 목록이 사라졌다. 섹션은 안에 든 요소 수가 9개에서 143개까지 들쭉날쭉했고, 공고
    항목은 22~25개로 고르다. 안의 칸 구성과 요소 수가 절반 이상 비슷해야 반복으로 본다.
    """
    shapes = Counter(_shape(node) for node in children)
    if shapes.most_common(1)[0][1] * 2 < len(children):
        return False
    sizes = sorted(len(node.find_all(True)) for node in children)
    middle = sizes[len(sizes) // 2]
    near = [size for size in sizes if abs(size - middle) <= middle * _SIZE_TOLERANCE]
    return len(near) * 2 >= len(children)


def _shape(node: Tag) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple(
        (child.name, tuple(child.get("class") or ())) for child in node.find_all(recursive=False)
    )


def _fallback_region(soup: BeautifulSoup) -> Tag | None:
    """반복 영역을 못 찾았을 때 좁힐 자리. 상세 페이지가 여기로 온다."""
    for selector in _NARROW_FALLBACKS:
        found = soup.select_one(selector)
        if found is not None:
            return found
    return None


def _compact(html: str) -> str:
    """빈 줄과 줄 끝 공백을 걷어낸다. 구조는 그대로 두고 토큰만 줄인다."""
    lines = (line.rstrip() for line in html.splitlines())
    return _BLANK_LINES.sub("\n", "\n".join(lines)).strip()
