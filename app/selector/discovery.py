"""등록할 때 상세로 가는 길을 스스로 알아낸다. 운영자는 목록 URL 하나만 넣는다.

순서가 정해져 있고, 앞 단계로 풀리면 뒤 단계는 하지 않는다.

| 순서 | 하는 일 | 다음으로 넘어가는 조건 |
|---|---|---|
| 1 | 목록을 `httpx` 로 받는다 | 항목이 없거나 상세 주소를 만들 값이 없다 |
| 2 | 렌더한다 | 렌더해도 항목에 상세 주소가 없다 |
| 3 | 항목을 클릭하고 그때 나가는 요청을 본다 | 클릭으로 상세에 도달했다 |
| 4 | 알아낸 요청을 `httpx` 로 다시 부른다 | 같은 응답이 오면 채택 |

**정적으로 되면 브라우저를 띄우지 않는다.** 렌더 한 번이 정적 fetch 의 몇십 배를 쓴다
(`.claude/rules/crawling.md`). 그래서 브라우저를 여는 것은 이 함수가 직접 하지 않고, 필요한
순간에 부르는 `open_probe` 로 받는다 — 1번에서 끝나면 그것을 한 번도 부르지 않는다.

## 판정은 제안이다

여기서 나오는 것은 `Discovery` 한 개이고, 어디에도 저장하지 않는다. 무엇을 저장할지는 운영자가
정한다. 여섯 사이트는 사람이 이미 확인해 설정해 뒀고, 그것을 이 판정으로 덮어쓰지 않는다
(`.claude/rules/llm.md` 의 "모델은 제안자이지 권위가 아니다" 와 같은 자리다).

## 근거를 문장으로 남긴다

`evidence` 는 사람이 읽는 한 줄이다. "정적 목록에 항목 0건, 렌더 후 16건, 항목에 상세 주소가
없어 클릭했다" 처럼 세 경로가 각각 다른 문장을 남긴다. 판정만 남기면 다음 사람이 왜 렌더로
정해졌는지 알 수 없어 처음부터 다시 재게 된다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from app.crawler.click_probe import DETAIL_UNREACHABLE, ClickOutcome, probe_click
from app.crawler.collect import API
from app.crawler.failures import LIST_EMPTY
from app.crawler.fetcher import FetchError, FetchPolicy
from app.crawler.parser import (
    CrawlDataError,
    ListItem,
    list_only,
    parse_list,
    select_nodes,
)
from app.crawler.playwright import PLAYWRIGHT, STATIC, ObservedRequest, ProbeSession
from app.llm.base import Usage
from app.selector.detail_path import (
    DetailPath,
    IdSource,
    confirm_api_path,
    confirm_document_path,
    document_path,
    id_candidates,
    pick_detail_request,
    propose_detail_config,
)
from app.selector.link_probe import (
    LinkProposal,
    confirm_link_template,
    propose_link_template,
)
from app.selector.list_api import (
    ListPath,
    confirm_list_path,
    propose_list_config,
    restore_truncated,
)
from app.selector.path_proposal import (
    PathAnswer,
    PathAsker,
    PathEvidence,
    Proposal,
    propose_path,
)
from app.selector.schema import SelectorSet

# 브라우저를 여는 쪽. 필요할 때만 불린다 (`Renderer.open_probe`)
ProbeOpener = Callable[[str], AbstractAsyncContextManager[ProbeSession]]

# AI 제안을 대조할 때 쓰는, 항목이 들고 있던 공고 주소의 수
MAX_KNOWN_LINKS = 5


@dataclass(frozen=True)
class Discovery:
    """등록할 때의 판정 하나. 저장은 운영자가 한다.

    `ok` 가 거짓이면 `failure` 가 `list_empty` 이거나 `detail_unreachable` 이고, 무엇을 보고
    그렇게 판정했는지는 `evidence` 와 `reason` 에 나뉘어 있다.
    """

    list_mode: str = STATIC
    detail_mode: str = ""
    detail: DetailPath | None = None
    # 목록을 JSON 으로 받을 수 있다는 것을 확인했으면 그 설정. 상세 판정이 실패해도 이것은
    # 이미 `httpx` 로 확인된 사실이라 따로 들고 있는다
    list: ListPath | None = None
    # 항목이 `href` 를 들고 있지 않아 클릭으로 알아낸 상세 주소 형식. 값이 있으면 그것이
    # `list.link` 와 `list.link_template` 이 된다 (`app/selector/link_probe.py`)
    link: LinkProposal | None = None
    evidence: str = ""
    reason: str = ""
    failure: str = ""
    list_count: int = 0
    # 판정이 막혀 AI 에게 경로를 물었으면 그 호출의 비용. 기록은 연결을 가진 등록 라우트가 한다
    ai_usage: Usage | None = None

    @property
    def ok(self) -> bool:
        return not self.reason and not self.failure

    @property
    def list_adopted(self) -> bool:
        """목록 API 를 다시 불러 확인까지 마쳤는가."""
        return self.list is not None and self.list.ok


async def discover_detail_path(
    list_url: str,
    selectors: SelectorSet,
    *,
    fetcher: FetchPolicy,
    open_probe: ProbeOpener | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    ask: PathAsker | None = None,
) -> Discovery:
    """목록 URL 하나로 상세로 가는 길을 찾는다. 찾은 것은 제안으로 돌려준다.

    `sleep` 은 클릭 뒤 기다리는 자리다. 시험이 실제로 3초를 기다리지 않게 바꿔 끼운다
    (`app/crawler/fetcher.py` 의 `clock`·`sleep` 과 같은 자리).

    `ask` 가 있으면 규칙으로 막힌 자리에서 AI 에게 목록 경로를 묻는다. 답은 확인을 통과해야만
    쓴다 (`app/selector/path_proposal.py`). 없으면 묻지 않는다.
    """
    static_html, static_items, static_note = await _items_from_static(fetcher, list_url, selectors)
    static_count = len(static_items)

    if static_items and not _needs_more(static_items, selectors):
        return Discovery(
            list_mode=STATIC,
            detail_mode=STATIC,
            detail=document_path(static_items[0].link, "목록 항목의 링크를 그대로 따라간다"),
            evidence=(
                f"정적 목록에서 항목 {static_count}건과 상세 주소를 찾았다. "
                "브라우저를 띄우지 않았다"
            ),
            list_count=static_count,
        )

    if open_probe is None:
        return Discovery(
            list_mode=STATIC,
            evidence=f"정적 목록 판정: {static_note}",
            failure=LIST_EMPTY if not static_items else DETAIL_UNREACHABLE,
            reason=(
                "정적으로는 상세로 갈 길이 없는데 브라우저를 열 수 없다. "
                "렌더가 가능한 배포에서 다시 등록한다"
            ),
            list_count=static_count,
        )

    # 브라우저 안에서는 **모으기만 한다.** 알아낸 것을 `httpx` 로 다시 불러 확인하는 것은
    # 브라우저를 닫은 뒤다 — 렌더는 호스트 잠금을 잡고 있고, 그 안에서 같은 호스트로 정적
    # 요청을 내면 자기 잠금을 기다리며 영영 멈춘다 (`app/crawler/fetcher.py` 의 `guard`)
    async with open_probe(list_url) as session:
        probed = await _probe(session, selectors=selectors, sleep=sleep)

    spent: list[Usage] = []
    found = await _judge(
        probed,
        fetcher=fetcher,
        selectors=selectors,
        static_count=static_count,
        static_html=static_html,
        ask=_counting(ask, spent) if ask is not None else None,
        list_url=list_url,
    )
    return replace(found, ai_usage=spent[0]) if spent else found


@dataclass(frozen=True)
class _Probed:
    """브라우저에서 모아 온 것. 여기부터는 브라우저가 닫혀 있다."""

    html: str
    url: str
    items: list[ListItem]
    note: str
    requests: list[ObservedRequest]
    outcome: ClickOutcome | None = None


async def _probe(
    session: ProbeSession,
    *,
    selectors: SelectorSet,
    sleep: Callable[[float], Awaitable[None]] | None,
) -> _Probed:
    """렌더된 목록을 읽고, 필요하면 항목을 눌러 본다. 여기서 `httpx` 를 부르지 않는다."""
    rendered, rendered_note = _items_from_html(
        session.html, session.url, selectors, linkless_fallback=True
    )
    outcome: ClickOutcome | None = None
    if rendered and _needs_more(rendered, selectors):
        outcome = await probe_click(
            session.page,
            context=session.context,
            log=session.log,
            item_selector=selectors.list.item,
            sleep=sleep,
        )
    return _Probed(
        html=session.html,
        url=session.url,
        items=rendered,
        note=rendered_note,
        # 클릭 전까지 관찰한 요청. 목록을 그린 요청은 이 안에 있다
        requests=list(session.log.requests),
        outcome=outcome,
    )


async def _judge(
    probed: _Probed,
    *,
    fetcher: FetchPolicy,
    selectors: SelectorSet,
    static_count: int,
    static_html: str = "",
    ask: PathAsker | None = None,
    list_url: str = "",
) -> Discovery:
    """모아 온 것으로 판정한다. 알아낸 경로를 `httpx` 로 다시 불러 확인하는 것도 여기서다."""
    prefix = f"정적 목록에 항목 {static_count}건"
    rendered, rendered_note = probed.items, probed.note
    count = len(rendered)

    if not rendered:
        return Discovery(
            list_mode=PLAYWRIGHT,
            evidence=f"{prefix}, 렌더 후에도 0건",
            failure=LIST_EMPTY,
            reason=f"렌더한 목록에서도 항목을 잡지 못했다: {rendered_note}",
            list_count=0,
        )

    # 목록이 JSON 으로 오는 사이트인지 먼저 본다. 목록을 그린 요청은 클릭 전에 이미 나갔고,
    # 여기서 채택되면 이 크롤러는 실행마다 브라우저를 띄우지 않는다
    requests = await restore_truncated(fetcher, probed.requests)
    list_path, list_note = await _adopt_list_api(fetcher, probed, rendered, requests)
    known = _known_links(probed, rendered, selectors)

    async def consult() -> Proposal | None:
        # 대조할 공고 주소가 없거나 관찰한 JSON 응답이 없으면 묻지 않는다. AI 가 설정을 만들
        # 근거도, 그 설정을 확인할 기준도 없는 호출이다
        if ask is None or not known or not any(r.is_json for r in requests if r.status == 200):
            return None
        evidence = PathEvidence(
            list_url=list_url or probed.url,
            items=rendered,
            known_links=known,
            requests=requests,
            robots=await _robots_text(fetcher, list_url or probed.url),
        )
        return await propose_path(ask, fetcher, evidence)

    if list_path is not None and "date" in list_path.missing:
        # 목록 API 는 채택했는데 마감일 칸을 못 읽었다(HD현대). 그대로 두면 끝난 공고까지 매 실행
        # 상세를 연다. AI 제안이 마감일 칸까지 확인을 통과할 때만 바꾼다
        proposal = await consult()
        if proposal is not None:
            if proposal.path is not None and "date" in proposal.path.config().fields:
                list_path = proposal.path
            list_note = f"{list_note}. {proposal.note}"
    if list_path is not None:
        list_note = f"{list_note}{_list_gap(list_path, list_path.count)}"
    list_mode = API if list_path is not None else PLAYWRIGHT

    if not _needs_more(rendered, selectors):
        item = rendered[0]
        confirmation = await confirm_document_path(fetcher, item.link, item.title)
        detail_mode = STATIC if confirmation.adopted else PLAYWRIGHT
        tail = (
            "상세 문서는 정적으로도 열렸다"
            if confirmation.adopted
            else f"상세 문서는 정적으로 열리지 않았다 — {confirmation.reason}"
        )
        return Discovery(
            list_mode=list_mode,
            detail_mode=detail_mode,
            detail=document_path(item.link, tail),
            list=list_path,
            evidence=(
                f"{prefix}, 렌더 후 {count}건. 항목에서 상세 주소를 얻어 클릭하지 않았다. "
                f"{tail}. {list_note}"
            ),
            list_count=count,
        )

    outcome = probed.outcome
    if outcome is None:
        # 항목에 상세 주소가 있는데 여기까지 왔다. 클릭할 이유가 없었다는 뜻이다
        return Discovery(
            list_mode=list_mode,
            list=list_path,
            evidence=f"{prefix}, 렌더 후 {count}건. {list_note}",
            failure=DETAIL_UNREACHABLE,
            reason="상세로 갈 길을 찾지 못했다",
            list_count=count,
        )
    if not outcome.reached:
        return Discovery(
            list_mode=list_mode,
            list=list_path,
            evidence=f"{prefix}, 렌더 후 {count}건, 항목에 상세 주소가 없어 클릭했다. {list_note}",
            failure=outcome.failure or DETAIL_UNREACHABLE,
            reason=outcome.reason,
            list_count=count,
        )

    signals = ", ".join(outcome.signals)
    clicked = (
        f"{prefix}, 렌더 후 {count}건, 항목에 상세 주소가 없어 클릭했다. "
        f"{outcome.target} 를 눌러 {signals}"
    )
    nodes = _item_nodes(probed.html, selectors)
    candidates = id_candidates(nodes[0]) if nodes else []
    picked = pick_detail_request(outcome.requests, candidates) if candidates else None

    if picked is not None:
        return await _from_api_request(
            fetcher, picked, clicked=clicked, count=count, list_path=list_path, list_note=list_note
        )

    # 클릭으로 알아낸 주소를 공고마다 다른 주소 형식으로 옮길 수 있는가. 주소 하나만 저장하면
    # 공고가 몇 건이든 같은 상세를 가져온다 (`app/selector/link_probe.py`)
    link, link_note = await _adopt_link_template(
        fetcher,
        nodes=nodes,
        titles=[item.title for item in rendered],
        reached_url=outcome.url,
        list_url=probed.url,
        requests=outcome.requests,
    )
    if link is not None:
        # 주소 형식을 알고 나면 정적 HTML 로도 목록이 읽히는 사이트가 있다. 두산과 네이버가
        # 그렇다 — 항목은 정적으로 다 있고 상세 주소만 `onclick` 에 있었다. 그때는 목록을
        # 정적으로 둔다. 렌더 한 번이 정적 fetch 의 몇십 배다 (`.claude/rules/crawling.md`)
        if list_path is None and _static_list_works(static_html, selectors, link, probed.url):
            list_mode = STATIC
            link_note = (
                f"{link_note}. 그 형식으로 정적 HTML 에서도 목록이 읽혀 목록을 정적으로 둔다"
            )
        return Discovery(
            list_mode=list_mode,
            detail_mode=STATIC,
            # 확인하면서 실제로 연 공고 주소다. 두산처럼 클릭해도 주소가 그대로인 사이트에서
            # `outcome.url` 을 쓰면 목록 주소가 상세로 적힌다
            detail=document_path(link.sample or outcome.url or probed.url, link_note),
            list=list_path,
            link=link,
            evidence=f"{clicked} — {link_note}. {list_note}",
            list_count=count,
        )

    if outcome.url and outcome.url != probed.url:
        # 클릭이 상세 문서로 데려갔다. 주소가 있으니 API 를 찾을 필요가 없다
        confirmation = await confirm_document_path(fetcher, outcome.url, _title(rendered))
        detail_mode = STATIC if confirmation.adopted else PLAYWRIGHT
        tail = (
            "그 주소는 정적으로도 열렸다"
            if confirmation.adopted
            else f"그 주소는 정적으로 열리지 않았다 — {confirmation.reason}"
        )
        evidence = f"{clicked} — 상세 문서 주소를 알아냈다. {tail}. {link_note}. {list_note}"
        if list_path is None:
            # 규칙으로는 막혔다. 모은 증거로 AI 에게 목록 경로를 묻는다(동원). 확인을 통과한
            # 제안만 쓴다 — 실제로 누른 공고의 주소가 제안한 형식으로 똑같이 나와야 한다
            proposal = await consult()
            if proposal is not None and proposal.path is not None:
                return Discovery(
                    list_mode=API,
                    detail_mode=detail_mode,
                    detail=document_path(outcome.url, tail),
                    list=proposal.path,
                    evidence=(
                        f"{evidence}. {proposal.note}"
                        f"{_list_gap(proposal.path, proposal.path.count)}"
                    ),
                    list_count=count,
                )
            if proposal is not None:
                evidence = f"{evidence}. {proposal.note}"
            # 공고 한 건의 상세는 열었지만 나머지 공고로 갈 길이 없다. 항목에 링크도, 주소를
            # 만들 속성도 없고 목록 API 도 채택되지 않았다. 여기서 `ok` 를 주면 링크 없는
            # 셀렉터가 저장되고 실행마다 전 건이 `detail_unreachable` 로 끝난다 — HD현대와
            # 동원이 그렇게 등록만 성공했다 (2026-09-21).
            #
            # 연 페이지는 싣는다. 운영자가 경로를 손으로 채울 때 상세 셀렉터는 이미 있어야
            # 하고, 그 페이지는 방금 열어 확인한 것이다 (`app/api/crawlers.py`)
            return Discovery(
                list_mode=list_mode,
                detail_mode=detail_mode,
                detail=document_path(outcome.url, tail),
                evidence=evidence,
                failure=DETAIL_UNREACHABLE,
                reason=(
                    f"공고 한 건을 눌러 상세 {outcome.url} 는 열었지만, 공고마다 다른 상세 주소를 "
                    "만들 재료가 없다. 항목에 링크·`data-` 속성·`onclick` 인자가 없고 목록 API 도 "
                    "채택되지 않았다"
                ),
                list_count=count,
            )
        return Discovery(
            list_mode=list_mode,
            detail_mode=detail_mode,
            detail=document_path(outcome.url, tail),
            list=list_path,
            evidence=evidence,
            list_count=count,
        )

    return Discovery(
        list_mode=list_mode,
        list=list_path,
        evidence=f"{clicked}. {link_note}. {list_note}",
        failure=DETAIL_UNREACHABLE,
        reason=(
            f"클릭 뒤 나간 요청 {len(outcome.requests)}건 중 이 공고를 지목한 것이 없고 "
            f"주소로도 형식을 만들지 못했다: {link_note}"
        ),
        list_count=count,
    )


async def _from_api_request(
    fetcher: FetchPolicy,
    picked: tuple[ObservedRequest, IdSource],
    *,
    clicked: str,
    count: int,
    list_path: ListPath | None,
    list_note: str,
) -> Discovery:
    """클릭이 알려 준 요청을 설정으로 만들고 `httpx` 로 다시 불러 확인한다."""
    request, source = picked
    list_mode = API if list_path is not None else PLAYWRIGHT
    path = propose_detail_config(request, source)
    if not path.ok:
        return Discovery(
            list_mode=list_mode,
            list=list_path,
            evidence=f"{clicked} — 요청 {request.url} 을 찾았다. {list_note}",
            failure=DETAIL_UNREACHABLE,
            reason=path.reason,
            list_count=count,
        )

    confirmation = await confirm_api_path(fetcher, path, request)
    if not confirmation.adopted:
        # 브라우저에서만 되는 요청은 채택하지 않는다. 저장하면 이후 실행이 전부 실패한다
        return Discovery(
            list_mode=list_mode,
            list=list_path,
            evidence=f"{clicked} — 상세 요청 {request.url} 을 알아냈다. {list_note}",
            failure=DETAIL_UNREACHABLE,
            reason=f"알아낸 요청을 다시 불러 확인하지 못했다: {confirmation.reason}",
            list_count=count,
        )

    return Discovery(
        list_mode=list_mode,
        detail_mode=API,
        detail=path,
        list=list_path,
        evidence=(
            f"{clicked} — 상세 API 를 알려 줬고, 같은 요청을 httpx 로 다시 불러 "
            f"제목과 본문 {confirmation.body_length}자가 같았다. {list_note}"
        ),
        list_count=count,
    )


async def _adopt_list_api(
    fetcher: FetchPolicy,
    probed: _Probed,
    items: list[ListItem],
    requests: list[ObservedRequest],
) -> tuple[ListPath | None, str]:
    """렌더 중 나간 요청에서 목록 API 를 찾고, 확인된 것만 돌려준다.

    확인은 공용 fetch 클라이언트로 한 번 다시 부르는 것이다. 브라우저에서만 되는 요청을
    저장하면 등록만 성공하고 이후 실행이 전부 실패한다 (`app/selector/list_api.py`).

    `referer` 하나로 갈리는 API 가 있어 한 번은 그것을 넣고 다시 확인한다. 담는 헤더는
    사이트가 요구하는 기능성 헤더뿐이고, 이름은 공용 클라이언트가 정한다
    (`.claude/rules/crawling.md`).

    제안 전에 잘린 응답을 한 번 다시 받는다. 목록을 본문까지 담아 주는 API 는 관찰 상한을
    넘겨 JSON 으로 읽히지 않고, 그대로 두면 API 가 있는 사이트가 없는 것으로 판정된다
    (`app/selector/list_api.py` 의 `restore_truncated`). 다시 받은 요청은 부르는 쪽이 넘긴다 —
    AI 에게 보일 증거도 같은 요청이어야 한다.
    """
    outcome = probed.outcome
    clicked_url = (
        outcome.url
        if outcome is not None and outcome.reached and outcome.url and outcome.url != probed.url
        else ""
    )
    proposed = propose_list_config(requests, items, _links(probed.html, probed.url), clicked_url)
    if not proposed.ok:
        return None, f"목록 API 는 찾지 못했다: {proposed.reason}"

    confirmation = await confirm_list_path(fetcher, proposed, items)
    path = proposed
    if not confirmation.adopted:
        path = proposed.with_referer(probed.url)
        confirmation = await confirm_list_path(fetcher, path, items)
    if not confirmation.adopted:
        return None, (
            f"목록 API 후보 {proposed.url} 는 다시 불러 확인되지 않아 채택하지 않았다: "
            f"{confirmation.reason}"
        )

    return path, (
        f"목록은 {path.url} 의 `{path.items_path}` 로 온다. httpx 로 다시 불러 "
        f"{confirmation.count}건 중 제목 {confirmation.matched}건이 같아 채택했다"
    )


def _known_links(
    probed: _Probed, rendered: list[ListItem], selectors: SelectorSet
) -> list[tuple[str, str]]:
    """실제로 확인한 (제목, 공고 주소). AI 제안을 대조하는 기준이다.

    항목이 주소를 들고 있으면 그것이고, 없으면 첫 항목을 눌러 도착한 주소다. 첫 항목은 누른 것과
    같은 항목이다 (`_item_nodes`). 둘 다 없으면 대조할 것이 없어 묻지 않는다.
    """
    if not _needs_more(rendered, selectors):
        return [(item.title, item.link) for item in rendered[:MAX_KNOWN_LINKS] if item.link]
    outcome = probed.outcome
    if (
        outcome is not None
        and outcome.reached
        and outcome.url
        and outcome.url != probed.url
        and rendered[0].title.strip()
    ):
        return [(rendered[0].title, outcome.url)]
    return []


async def _robots_text(fetcher: FetchPolicy, list_url: str) -> str:
    """AI 에게 보일 robots.txt. 못 받으면 빈 문자열이고, robots 가 아닌 응답도 버린다.

    동원 캠페인 사이트는 robots.txt 를 404 HTML 로 돌려준다. 그것을 규칙으로 보이면 AI 가
    없는 규칙을 읽는다.
    """
    parts = urlsplit(list_url)
    try:
        page = await fetcher.fetch(f"{parts.scheme}://{parts.netloc}/robots.txt")
    except FetchError:
        return ""
    return page.text if "user-agent" in page.text.lower() else ""


def _counting(ask: PathAsker, spent: list[Usage]) -> PathAsker:
    """묻는 함수를 감싸 비용을 모은다. 기록은 연결을 가진 등록 라우트가 한다."""

    async def counted(prompt: str) -> tuple[PathAnswer, Usage | None]:
        answer, usage = await ask(prompt)
        if usage is not None:
            spent.append(usage)
        return answer, usage

    return counted


def _list_gap(path: ListPath, count: int) -> str:
    """채택한 목록 설정에서 못 읽은 칸. 운영자가 채워야 하는 자리라 근거에 함께 적는다.

    `date` 가 비면 마감 여부를 모르는 채로 돌기 때문에, 지난 공고까지 매 실행 상세를 연다.
    HD현대 목록 API 는 끝난 공고를 포함해 556건을 주고 그중 진행 중인 것은 21건이다 —
    적어 두지 않으면 이 차이가 실행 시간으로만 나타나고 이유는 어디에도 남지 않는다.
    """
    if not path.missing:
        return ""
    gap = f". 다만 목록에서 {', '.join(path.missing)} 은 읽지 못했다"
    if "date" not in path.missing:
        return gap
    return (
        f"{gap} — 마감일을 모르면 끝난 공고 {count}건의 상세까지 매 실행 연다. "
        "설정에서 `list.fields.date` 와 `list.date_is_deadline` 을 적는다"
    )


async def _adopt_link_template(
    fetcher: FetchPolicy,
    *,
    nodes: list[Tag],
    titles: list[str],
    reached_url: str,
    list_url: str,
    requests: tuple[ObservedRequest, ...] | list[ObservedRequest],
) -> tuple[LinkProposal | None, str]:
    """클릭으로 알아낸 주소를 항목마다 다른 형식으로 옮기고, 확인된 것만 돌려준다.

    확인은 그 형식으로 만든 주소 두 개를 공용 fetch 클라이언트로 열어 보는 것이다. 확인하지
    않고 저장하면 공고마다 같은 페이지를 가져오는 크롤러가 남는다.
    """
    proposal = propose_link_template(
        nodes, reached_url=reached_url, list_url=list_url, requests=list(requests)
    )
    if not proposal.ok:
        return None, f"공고마다 다른 상세 주소 형식은 만들지 못했다: {proposal.reason}"

    confirmation = await confirm_link_template(fetcher, proposal, nodes, titles)
    if not confirmation.adopted:
        return None, (
            f"상세 주소 형식 {proposal.template} 은 확인되지 않아 채택하지 않았다: "
            f"{confirmation.reason}"
        )

    proposal = replace(proposal, sample=confirmation.urls[0] if confirmation.urls else "")
    return proposal, (
        f"{proposal.source}에서 공고마다 다른 상세 주소 형식을 얻었다: {proposal.template} "
        f"(항목 {proposal.resolved}/{proposal.count}건에서 주소가 나왔고, "
        f"{confirmation.checked}건을 열어 제목을 확인했다)"
    )


def _links(html: str, base_url: str) -> list[str]:
    """렌더된 페이지에 걸려 있던 주소. 목록 API 항목의 id 를 이 안에서 찾는다.

    항목 노드 안에서만 찾지 않는 이유는 항목 자체가 `a` 인 사이트가 있기 때문이다 —
    카카오 목록이 `<a><li>...</li></a>` 다 (`app/selector/list_api.py`).
    """
    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:")):
            continue
        url = urljoin(base_url, href)
        if url.startswith(("http://", "https://")):
            found.append(url)
    return found


async def _items_from_static(
    fetcher: FetchPolicy, list_url: str, selectors: SelectorSet
) -> tuple[str, list[ListItem], str]:
    """1번. 정적으로 받아 항목을 잡아 본다. 실패도 사유 문장으로 돌려준다.

    받은 HTML 을 함께 돌려주는 이유는 뒤에서 한 번 더 보기 때문이다. 상세 주소 형식을 알아낸
    뒤에는 같은 HTML 로 목록이 읽히는지가 달라질 수 있다.
    """
    try:
        page = await fetcher.fetch(list_url)
    except FetchError as exc:
        return "", [], f"정적 fetch 가 실패했다: {exc}"
    items, note = _items_from_html(page.text, page.url, selectors, linkless_fallback=True)
    return page.text, items, note


def _static_list_works(
    static_html: str, selectors: SelectorSet, link: LinkProposal, base_url: str
) -> bool:
    """알아낸 주소 형식을 얹으면 정적 HTML 만으로 목록이 읽히는가.

    읽힌다면 이 크롤러는 실행마다 브라우저를 띄울 이유가 없다. 판정은 실행 때와 같은 파서로
    한다 — 여기서 따로 세면 "판정은 정적이라는데 실행은 0건" 이 생긴다.
    """
    if not static_html.strip():
        return False
    items, _ = _items_from_html(
        static_html, base_url, selectors.model_copy(update={"list": link.selectors(selectors.list)})
    )
    return bool(items)


def _items_from_html(
    html: str, base_url: str, selectors: SelectorSet, *, linkless_fallback: bool = False
) -> tuple[list[ListItem], str]:
    """목록을 읽는다. `linkless_fallback` 이면 링크만 못 읽은 목록을 링크 없는 항목으로 받는다.

    네이버 실측(2026-09-17): 모델이 고른 링크 셀렉터가 `#n` 을 돌려줘 항목 10건이 전부 버려졌고,
    판정은 "항목 0건" 으로 끝나 클릭 단계에 가지 못했다. 제목은 잡혔으니 목록은 맞다 — 링크 없는
    항목으로 받으면 `_needs_more` 가 참이 되어 항목을 눌러 상세 주소를 알아낸다.

    알아낸 주소 형식을 검사하는 자리(`_static_list_works`)는 이 대체를 쓰지 않는다. 링크를 못
    읽는 형식을 "읽힌다" 고 판정하면 실행이 전부 실패한다.
    """
    try:
        result = parse_list(html, selectors.list, base_url)
    except CrawlDataError as exc:
        if not linkless_fallback or list_only(selectors.list):
            return [], str(exc)
        linkless = selectors.list.model_copy(update={"link": "", "link_template": ""})
        try:
            fallback = parse_list(html, linkless, base_url)
        except CrawlDataError:
            return [], str(exc)
        return fallback.items, (
            f"링크를 읽지 못해 링크 없이 항목 {len(fallback.items)}건을 잡았다 ({exc})"
        )
    return result.items, f"항목 {len(result.items)}건"


def _needs_more(items: list[ListItem], selectors: SelectorSet) -> bool:
    """상세로 갈 값이 항목에 없는가. 없으면 렌더나 클릭으로 넘어간다."""
    if list_only(selectors.list):
        return True
    return items[0].detail_absent or not items[0].link.strip()


def _item_nodes(html: str, selectors: SelectorSet) -> list[Tag]:
    """항목 노드 전부. 첫 항목은 클릭한 것과 같은 것이다.

    전부 넘기는 이유는 알아낸 주소 형식이 **몇 건에서 실제로 주소를 내는지** 세기 위해서다.
    실제로 열어 확인하는 것은 그중 두 건뿐이다 (`app/selector/link_probe.py`).
    """
    soup = BeautifulSoup(html, "html.parser")
    return select_nodes(soup, selectors.list.item, "list.item")


def _title(items: list[ListItem]) -> str:
    return items[0].title if items else ""
