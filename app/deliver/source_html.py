"""오공고 공고 상세에 그릴 원문 HTML 을 만든다 (2026-10-13, LC-3456).

원문 글자(`source_text`)는 AI 가 근거로 읽으라고 본문의 부모 영역까지 편 글자다. 옆에 붙은 다른
공고 목록이 섞이고 제목·목록·표가 사라져, 사용자에게 그대로 보여 줄 수 없었다. 그래서 본문 셀렉터가
잡은 영역의 HTML(`raw_jobs.raw_data_json` 의 `body_html`)을 보내기 직전에 정리한다.

- 스크립트·스타일·폼·영상 같은 것은 통째로 뺀다
- 남기는 태그는 `KEPT_TAGS` 뿐이다. 그 밖의 블록 태그(`section` 등)는 `div` 로, 글자 태그(`span`·
  `font` 등)는 벗겨 글만 남긴다
- 속성은 링크 주소·이미지 주소·이미지 대체 글·표 칸 합치기만 남긴다. 색·글꼴 같은 꾸밈은 빠진다
- 상대 주소는 원문 주소 기준 절대 주소로 바꾼다. 지연 로딩 이미지는 `data-src` 를 주소로 쓴다
- 공백은 줄이고 글도 이미지도 없는 빈 블록은 지운다

오공고도 같은 HTML 을 한 번 더 거른다 — 사용자 화면이 그대로 그리므로 보안은 서버가 책임진다.
여기서는 모양과 크기를 다듬는다.

HTML 이 없는 공고(이 결정 전에 모은 공고, 상세가 API 인 사이트)는 본문 글자를 문단으로 감싼다
(`text_html`).
"""

from __future__ import annotations

import html
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from app.crawler.parser import PAGE_FURNITURE

# 남기는 태그
KEPT_TAGS: frozenset[str] = frozenset(
    {
        "p", "br", "div", "hr", "blockquote", "pre",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "ul", "ol", "li", "dl", "dt", "dd",
        "strong", "b", "em", "i", "u",
        "table", "caption", "thead", "tbody", "tfoot", "tr", "th", "td",
        "a", "img",
    }
)  # fmt: skip
# 안에 든 것까지 통째로 빼는 태그
DROPPED_TAGS: tuple[str, ...] = (
    "script", "style", "noscript", "template", "iframe", "object", "embed", "svg", "canvas",
    "video", "audio", "form", "input", "button", "select", "textarea", "head", "meta", "link",
)  # fmt: skip
# 남기지 않지만 블록으로 보여야 하는 태그. `div` 로 바꾼다
_BLOCKISH: frozenset[str] = frozenset(
    {"section", "article", "main", "header", "figure", "figcaption", "center", "details",
     "summary", "address", "aside", "nav", "footer", "fieldset", "legend"}
)  # fmt: skip
_KEPT_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    "a": ("href",),
    "img": ("src", "alt"),
    "td": ("colspan", "rowspan"),
    "th": ("colspan", "rowspan"),
}
# 지연 로딩 이미지가 진짜 주소를 두는 속성. 앞의 것이 먼저다
_LAZY_SOURCES: tuple[str, ...] = ("data-src", "data-original", "data-lazy-src", "data-url")
# 블록으로 그려지는 태그. 이 태그 사이의 공백 글자는 화면에 보이지 않아 지운다
_BLOCKS: frozenset[str] = KEPT_TAGS - {"strong", "b", "em", "i", "u", "a", "img"}
# 안이 비어도 지우지 않는 태그
_EMPTY_OK: frozenset[str] = frozenset({"br", "hr", "img", "td", "th"})
_SPACES = re.compile(r"\s+")
_LINE_SPACES = re.compile(r"[ \t 　​]+")


def clean_html(raw: str | None, page_url: str) -> str | None:
    """본문 영역 HTML 을 오공고에 보낼 모양으로 정리한다. 글도 이미지도 남지 않으면 null."""
    if not raw or not raw.strip():
        return None
    soup = BeautifulSoup(raw, "html.parser")
    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()
    for node in soup.select(", ".join((*DROPPED_TAGS, PAGE_FURNITURE))):
        node.decompose()
    for tag in list(soup.find_all(True)):
        _reshape(tag, page_url)
    _drop_empty(soup)
    soup.smooth()
    for text in list(soup.find_all(string=True)):
        if isinstance(text, NavigableString) and not _inside_pre(text):
            _squeeze(text)
    cleaned = soup.decode(formatter="html5").strip()
    has_text = bool(soup.get_text(strip=True))
    return cleaned if has_text or soup.find("img") is not None else None


def text_html(text: str | None) -> str | None:
    """HTML 이 없는 공고의 본문 글자를 문단으로 감싼다. 빈 줄이 문단 경계, 줄바꿈은 `<br>`."""
    if not text:
        return None
    paragraphs: list[list[str]] = [[]]
    for line in str(text).splitlines():
        line = _LINE_SPACES.sub(" ", line).strip()
        if line:
            paragraphs[-1].append(html.escape(line))
        elif paragraphs[-1]:
            paragraphs.append([])
    blocks = [f"<p>{'<br>'.join(lines)}</p>" for lines in paragraphs if lines]
    return "".join(blocks) or None


def _reshape(tag: Tag, page_url: str) -> None:
    if tag.decomposed:
        return
    name = tag.name.lower()
    if name in _BLOCKISH:
        tag.name = name = "div"
    if name not in KEPT_TAGS:
        tag.unwrap()
        return
    if name == "img":
        _image_source(tag)
    kept = _KEPT_ATTRIBUTES.get(name, ())
    tag.attrs = {key: value for key, value in tag.attrs.items() if key in kept}
    for key in ("href", "src"):
        if key in tag.attrs:
            absolute = _absolute(tag.attrs[key], page_url, allow_mail=key == "href")
            if absolute:
                tag.attrs[key] = absolute
            else:
                del tag.attrs[key]
    if name == "img" and "src" not in tag.attrs:
        tag.decompose()
    elif name == "a" and "href" not in tag.attrs:
        tag.unwrap()


def _image_source(tag: Tag) -> None:
    """지연 로딩 이미지는 `src` 가 빈 칸이거나 `data:` 자리표시다. 진짜 주소로 바꾼다."""
    current = str(tag.get("src") or "").strip()
    if current and not current.startswith("data:"):
        return
    for attribute in _LAZY_SOURCES:
        value = str(tag.get(attribute) or "").strip()
        if value and not value.startswith("data:"):
            tag["src"] = value
            return


def _absolute(value: object, page_url: str, *, allow_mail: bool) -> str:
    raw = (" ".join(value) if isinstance(value, list) else str(value or "")).strip()
    if not raw or raw.startswith("#"):
        return ""
    if allow_mail and raw.lower().startswith("mailto:"):
        return raw
    absolute = urljoin(page_url, raw)
    return absolute if urlsplit(absolute).scheme in ("http", "https") else ""


def _squeeze(text: NavigableString) -> None:
    """공백을 한 칸으로 줄인다. 블록 사이나 블록 끝에 놓인 공백뿐인 글자는 지운다."""
    squeezed = _SPACES.sub(" ", str(text))
    if squeezed.strip():
        text.replace_with(squeezed)
        return
    before, after = text.previous_sibling, text.next_sibling
    if _is_edge(before) or _is_edge(after):
        text.extract()
    else:
        text.replace_with(" ")


def _is_edge(node: object) -> bool:
    return node is None or (isinstance(node, Tag) and node.name in _BLOCKS)


def _inside_pre(text: NavigableString) -> bool:
    return any(parent.name == "pre" for parent in text.parents if isinstance(parent, Tag))


def _drop_empty(soup: BeautifulSoup) -> None:
    """글도 이미지도 없는 블록을 지운다. 안쪽부터 지우면 감싸던 블록도 비어 다음 차례에 지워진다."""
    for tag in reversed(soup.find_all(True)):
        if tag.decomposed or tag.name in _EMPTY_OK:
            continue
        if not tag.get_text(strip=True) and tag.find(["img", "br", "hr"]) is None:
            tag.decompose()
