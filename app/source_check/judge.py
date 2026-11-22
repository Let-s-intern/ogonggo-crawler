"""원문 페이지 하나를 열어 마감 / 모집 중 / 모르겠음 으로 판정한다 (2026-11-15 결정, LC-3621).

한 번 판정으로 바로 마감하므로 **모르겠으면 절대 마감으로 읽지 않는다.** 잘못 마감한 공고는
사용자가 지원을 못 하고, 놓친 마감은 다음 날 다시 본다.

| 본 것 | 판정 |
|---|---|
| 404·410 | 마감 |
| 도메인이 없다(DNS 이름 없음) | 마감 |
| 상세 주소가 사이트 첫 화면으로 돌아갔다 | 마감 |
| 200 이고 공고 제목이 보이며 마감 문구가 없다 | 모집 중 |
| 200 인데 제목이 안 보이거나 마감 문구가 있다 | AI 에게 묻는다 |
| 그 밖의 4xx, 5xx, 시간 초과, 연결 실패, 봇 확인, robots 차단, 로그인 화면 | 모르겠음 |

마감 문구만 보고 마감하지 않는 이유는 목록 탭("마감된 공고 보기")이나 "충원 시 마감" 같은 안내가
모집 중인 공고에도 흔히 있어서다. 문구는 AI 에게 물을지를 가르는 데만 쓴다.

본문 글자가 너무 적으면 자바스크립트로 그리는 화면이라 보고 브라우저로 한 번 더 연다.
"""

from __future__ import annotations

import logging
import re
import socket
import sqlite3
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.crawler.fetcher import (
    BotChallengeError,
    FetchError,
    FetchResult,
    PageSource,
    ResponseStatusError,
    TransportError,
)
from app.llm.base import LlmCallError
from app.llm.log import CLASSIFY, record_call
from app.llm.providers import for_feature

logger = logging.getLogger(__name__)

CLOSED = "closed"
OPEN = "open"
UNKNOWN = "unknown"

# `llm_calls.feature` 에 남는 이름. 공고 분석처럼 제공자는 '공고 분류' 설정을 따른다
SOURCE_CHECK = "source_check"

# 이보다 글자가 적으면 자바스크립트로 그리는 화면이라 보고 브라우저로 다시 연다
MIN_TEXT = 300
# AI 에게 보내는 본문 글자 수 상한
MAX_PROMPT_TEXT = 6000

_GONE_STATUS = (404, 410)

# 마감·삭제를 알리는 문구. AI 에게 물을지를 가를 뿐 이것만으로 마감하지 않는다
_CLOSED_PHRASES = re.compile(
    r"(모집|채용|접수|공고|지원)\s*[이가은는]?\s*(마감|종료)\s*(되었|됐|된)"
    r"|(마감|종료|삭제|만료)\s*된\s*(공고|채용|포지션|모집)"
    r"|존재하지\s*않는\s*(공고|페이지|채용|포지션)"
    r"|(페이지|공고|채용\s*정보|포지션)\s*[을를]\s*찾을\s*수\s*없"
    r"|no\s+longer\s+(available|accepting|open|active)"
    r"|(position|job|posting)\s+(has\s+been\s+)?(closed|filled|expired|removed)"
    r"|(job|page|posting)\s+not\s+found",
    re.IGNORECASE,
)

# 이런 주소로 돌아가면 마감이 아니라 로그인이 필요한 화면이다
_LOGIN_PATH = re.compile(r"login|signin|sign-in|sign_in|auth", re.IGNORECASE)

_SYSTEM = (
    "너는 채용공고 페이지를 읽고 그 공고가 지금도 지원을 받는지 가리는 검사기다. "
    "페이지에 적힌 것만 근거로 삼고, 확실하지 않으면 unknown 으로 답한다."
)

_PROMPT = """# 할 일

아래는 채용공고 원문 주소를 열었을 때 받은 페이지의 글자다. 이 페이지가 아래 공고를 지금도 모집
중으로 보여 주는지 가린다.

# 답

- open: 이 공고가 페이지에 있고 마감·종료 표시가 없다
- closed: 이 공고가 마감·종료·삭제됐다고 페이지가 분명히 말하거나, 공고가 없다는 오류 화면이다
- unknown: 로그인·봇 확인·빈 화면·목록 화면처럼 이 공고의 상태를 알 수 없다

다음은 closed 의 근거가 아니다.
- "충원 시 조기 마감", "채용 시 마감" 처럼 앞으로 마감될 수 있다는 안내
- 다른 공고의 마감 표시, "마감된 공고 보기" 같은 메뉴·탭 이름

reason 에는 판단한 근거가 된 페이지 문구를 짧게 적는다.

# 공고

- 회사: {company}
- 제목: {title}
- 원문 주소: {url}
- 열린 주소: {final_url}

# 페이지 글자

{text}
"""


class SourceCheckAnswer(BaseModel):
    status: Literal["open", "closed", "unknown"]
    reason: str


@dataclass(frozen=True)
class Verdict:
    status: str
    reason: str


def fetch_url(source_url: str) -> str:
    """요청할 주소. 직무마다 나뉜 공고는 `원문#번호` 로 등록돼 있어 `#` 뒤를 뗀다."""
    return source_url.split("#", 1)[0]


async def judge(
    conn: sqlite3.Connection,
    url: str,
    *,
    title: str,
    company: str,
    fetcher: PageSource,
    renderer: PageSource | None = None,
    settings: Settings | None = None,
) -> Verdict:
    """원문 하나를 판정한다. 예외를 올리지 않는다."""
    try:
        result = await fetcher.fetch(url)
    except FetchError as exc:
        return _from_error(url, exc)

    redirected = _redirect_verdict(url, result.url)
    if redirected is not None:
        return redirected

    text = visible_text(result.text)
    if len(text) < MIN_TEXT and renderer is not None:
        try:
            rendered = await renderer.fetch(url)
        except FetchError as exc:
            logger.info("원문을 브라우저로 열지 못했다 %s: %s", url, exc)
        else:
            redirected = _redirect_verdict(url, rendered.url)
            if redirected is not None:
                return redirected
            result = rendered
            text = visible_text(rendered.text)

    if not text.strip():
        return Verdict(UNKNOWN, "페이지에 글자가 없다")
    if shows_title(text, title) and not _CLOSED_PHRASES.search(text):
        return Verdict(OPEN, "제목이 보이고 마감 문구가 없다")
    return await _ask(conn, url, result, text, title=title, company=company, settings=settings)


def visible_text(html: str) -> str:
    """`<body>` 에서 화면에 보이는 글자. 스크립트와 스타일은 빼고 공백을 하나로 줄인다."""
    soup = BeautifulSoup(html, "html.parser")
    body = soup.body or soup
    for node in body.find_all(("script", "style", "noscript", "template")):
        node.decompose()
    return " ".join(body.get_text(" ").split())


def shows_title(text: str, title: str) -> bool:
    """공고 제목이 페이지에 보이는가. 공백과 대소문자는 가리지 않는다.

    수집하며 정리한 제목이 원문과 조금 다를 수 있어, 통째로 없으면 두 글자 이상 낱말의 6할 이상이
    보이는지 본다.
    """
    squeezed = "".join(text.split()).lower()
    whole = "".join(title.split()).lower()
    if not whole:
        return False
    if whole in squeezed:
        return True
    words = [word.lower() for word in re.split(r"[\s\[\]()<>{}/|·,:\-]+", title) if len(word) >= 2]
    if not words:
        return False
    return sum(word in squeezed for word in words) / len(words) >= 0.6


def _from_error(url: str, exc: FetchError) -> Verdict:
    if isinstance(exc, BotChallengeError):
        return Verdict(UNKNOWN, f"봇 확인에 막혔다({exc.status_code})")
    if isinstance(exc, ResponseStatusError):
        if exc.status_code in _GONE_STATUS:
            return Verdict(CLOSED, f"응답 {exc.status_code}")
        return Verdict(UNKNOWN, f"응답 {exc.status_code}")
    if isinstance(exc, TransportError) and _no_such_host(url):
        return Verdict(CLOSED, "도메인이 없다")
    return Verdict(UNKNOWN, str(exc)[:200])


def _no_such_host(url: str) -> bool:
    """DNS 가 이 이름을 모른다고 답했는가. 시간 초과나 일시 실패는 모른다고 보지 않는다."""
    host = urlsplit(url).hostname
    if not host:
        return False
    try:
        socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return exc.errno == socket.EAI_NONAME
    except OSError:
        return False
    return False


def _redirect_verdict(url: str, final_url: str) -> Verdict | None:
    """상세 주소가 다른 곳으로 돌아갔으면 판정한다. 돌아가지 않았거나 판단이 안 서면 None."""
    before = urlsplit(url)
    after = urlsplit(final_url)
    if (before.netloc, before.path, before.query) == (after.netloc, after.path, after.query):
        return None
    if _LOGIN_PATH.search(after.path) or _LOGIN_PATH.search(after.query):
        return Verdict(UNKNOWN, f"로그인 화면으로 돌아갔다: {final_url}")
    if before.netloc.lower().removeprefix("www.") != after.netloc.lower().removeprefix("www."):
        return None
    after_root = after.path.strip("/") == "" and not after.query
    before_root = before.path.strip("/") == "" and not before.query
    if after_root and not before_root:
        return Verdict(CLOSED, f"사이트 첫 화면으로 돌아갔다: {final_url}")
    return None


async def _ask(
    conn: sqlite3.Connection,
    url: str,
    result: FetchResult,
    text: str,
    *,
    title: str,
    company: str,
    settings: Settings | None,
) -> Verdict:
    resolved = settings or get_settings()
    prompt = _PROMPT.format(
        company=company,
        title=title,
        url=url,
        final_url=result.url,
        text=text[:MAX_PROMPT_TEXT],
    )
    try:
        provider, model = for_feature(CLASSIFY, resolved)
        client = provider.build_client(resolved)
        answer_text, usage = await provider.call_model(
            client,
            model,
            prompt,
            1,
            "상시 채용 마감 여부 확인",
            response_schema=SourceCheckAnswer,
            system_instruction=_SYSTEM,
        )
    except LlmCallError as exc:
        return Verdict(UNKNOWN, f"AI 를 부르지 못했다({exc.reason}): {exc}"[:200])
    except Exception as exc:  # 제공자 설정이 비었거나 SDK 가 예상 밖의 예외를 낸다
        return Verdict(UNKNOWN, f"AI 를 부르지 못했다: {type(exc).__name__}: {exc}"[:200])
    record_call(conn, feature=SOURCE_CHECK, usage=usage)
    try:
        answer = SourceCheckAnswer.model_validate_json(answer_text)
    except ValueError:
        return Verdict(UNKNOWN, "AI 답을 읽지 못했다")
    reason = " ".join(answer.reason.split())[:200]
    return Verdict(answer.status, f"AI: {reason}")
