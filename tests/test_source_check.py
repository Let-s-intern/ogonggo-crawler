"""상시 채용 원문 확인 (`app/source_check/`).

사이트도 오공고도 AI 도 부르지 않는다. 페이지는 가짜 `PageSource`, 오공고는 `httpx.MockTransport`,
AI 는 가짜 제공자가 답한다.

| 확인 | 깨지면 |
|---|---|
| 404·410·첫 화면으로 돌아감은 마감 | 내려간 공고가 계속 모집 중이다 |
| 5xx·봇 확인·로그인 화면·403 은 모르겠음 | 사이트가 잠깐 아프거나 막은 날 공고가 무더기로 닫힌다 |
| 제목이 보이고 마감 문구가 없으면 AI 를 부르지 않고 모집 중 | 공고마다 AI 비용이 나간다 |
| 마감 문구가 있으면 AI 가 정한다 | "마감된 공고 보기" 탭 하나로 모집 중 공고가 닫힌다 |
| 글자가 적으면 브라우저로 다시 연다 | 자바스크립트 화면이 전부 모르겠음이 된다 |
| 마감만 오공고에 보내고, 나뉜 공고의 원문은 한 번만 연다 | 같은 사이트를 여러 번 두드린다 |
| 다 보면 이어 볼 자리를 0 으로, 멈추면 본 데까지 남긴다 | 뒤쪽 공고를 영영 보지 못한다 |
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

from app import db
from app.config import Settings
from app.crawler.fetcher import (
    BotChallengeError,
    FetchError,
    FetchResult,
    ResponseStatusError,
    TransportError,
)
from app.deliver.settings import DeliverConfig, write_config
from app.job_analysis import ogonggo as analysis_ogonggo
from app.llm.base import Usage
from app.source_check import judge as judge_module
from app.source_check import runner
from app.source_check.judge import CLOSED, OPEN, UNKNOWN, judge, shows_title

KEY = "test-internal-key"
SETTINGS = Settings(ogonggo_internal_api_key=KEY)
TITLE = "백엔드 개발자 채용"
OPEN_PAGE = (
    f"<html><body><h1>{TITLE}</h1><p>{'자격 요건과 우대 사항을 적은 본문. ' * 20}</p></body></html>"
)


class Pages:
    """주소마다 정해 둔 답을 주는 가짜 `PageSource`."""

    def __init__(self, answers: dict[str, FetchResult | FetchError]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        answer = self.answers[url]
        if isinstance(answer, FetchError):
            raise answer
        return answer


def page(url: str, html: str = OPEN_PAGE, final_url: str | None = None) -> FetchResult:
    return FetchResult(url=final_url or url, status_code=200, text=html)


class FakeAi:
    def __init__(self, status: str = "closed", reason: str = "모집이 마감되었습니다") -> None:
        self.status = status
        self.reason = reason
        self.prompts: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeAi:
        ai = self

        class Provider:
            name = "fake"

            def build_client(self, settings: Settings) -> object:
                return object()

            async def call_model(
                self, client: Any, model: str, prompt: str, *args: Any, **kwargs: Any
            ) -> tuple[str, Usage]:
                ai.prompts.append(prompt)
                usage = Usage(
                    provider="fake",
                    model=model,
                    input_tokens=10,
                    output_tokens=2,
                    total_tokens=12,
                    latency_ms=1,
                )
                return json.dumps({"status": ai.status, "reason": ai.reason}), usage

        monkeypatch.setattr(
            judge_module, "for_feature", lambda feature, settings: (Provider(), "m")
        )
        return self


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    write_config(connection, DeliverConfig(url="https://ogonggo.test/"))
    try:
        yield connection
    finally:
        connection.close()


async def verdict(
    conn: sqlite3.Connection,
    answer: FetchResult | FetchError,
    *,
    url: str = "https://site.test/jobs/1",
    renderer: Pages | None = None,
) -> judge_module.Verdict:
    return await judge(
        conn,
        url,
        title=TITLE,
        company="오공고",
        fetcher=Pages({url: answer}),
        renderer=renderer,
        settings=SETTINGS,
    )


@pytest.mark.parametrize("status", [404, 410])
async def test_없어진_페이지는_마감이다(conn: sqlite3.Connection, status: int) -> None:
    result = await verdict(conn, ResponseStatusError("gone", status))
    assert result.status == CLOSED


@pytest.mark.parametrize(
    "error",
    [
        ResponseStatusError("forbidden", 403),
        BotChallengeError("challenge", 429),
        TransportError("서버 오류 503"),
        FetchError("robots"),
    ],
)
async def test_열지_못한_페이지는_모르겠음이다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, error: FetchError
) -> None:
    monkeypatch.setattr(judge_module, "_no_such_host", lambda url: False)
    result = await verdict(conn, error)
    assert result.status == UNKNOWN


async def test_도메인이_없으면_마감이다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(judge_module, "_no_such_host", lambda url: True)
    result = await verdict(conn, TransportError("전송 실패(ConnectError)"))
    assert result.status == CLOSED


async def test_첫_화면으로_돌아가면_마감이다(conn: sqlite3.Connection) -> None:
    url = "https://site.test/jobs/1"
    result = await verdict(conn, page(url, final_url="https://www.site.test/"), url=url)
    assert result.status == CLOSED


async def test_로그인_화면으로_돌아가면_모르겠음이다(conn: sqlite3.Connection) -> None:
    url = "https://site.test/jobs/1"
    result = await verdict(conn, page(url, final_url="https://site.test/login?next=/jobs/1"))
    assert result.status == UNKNOWN


async def test_제목이_보이고_마감_문구가_없으면_AI_없이_모집_중이다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    ai = FakeAi().install(monkeypatch)
    result = await verdict(conn, page("https://site.test/jobs/1"))
    assert result.status == OPEN
    assert ai.prompts == []


async def test_마감_문구가_있으면_AI_가_정한다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = OPEN_PAGE.replace("</h1>", "</h1><a>마감된 공고 보기</a>")
    ai = FakeAi(status="open", reason="메뉴 이름일 뿐이다").install(monkeypatch)

    result = await verdict(conn, page("https://site.test/jobs/1", html))

    assert result.status == OPEN
    assert "마감된 공고 보기" in ai.prompts[0]
    row = conn.execute("SELECT feature, ok FROM llm_calls").fetchone()
    assert tuple(row) == ("source_check", 1)


async def test_제목이_없으면_AI_가_마감으로_정할_수_있다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeAi(status="closed").install(monkeypatch)
    html = "<body>" + "요청하신 채용 정보는 모집이 마감되었습니다. " * 20 + "</body>"
    result = await verdict(conn, page("https://site.test/jobs/1", html))
    assert result.status == CLOSED


async def test_글자가_적으면_브라우저로_다시_연다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://site.test/jobs/1"
    ai = FakeAi().install(monkeypatch)
    rendered = Pages({url: page(url)})

    result = await verdict(conn, page(url, "<body><div id=root></div></body>"), renderer=rendered)

    assert rendered.calls == [url]
    assert result.status == OPEN
    assert ai.prompts == []


def test_정리된_제목도_낱말이_대부분_보이면_보인다고_본다() -> None:
    text = "오공고 백엔드 개발자 (경력) 상시 채용 안내"
    assert shows_title(text, "[오공고] 백엔드 개발자 채용")
    assert not shows_title("디자이너 모집", "[오공고] 백엔드 개발자 채용")


def server(
    targets: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> tuple[list[httpx.Request], Callable[[], None]]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/source-check-targets"):
            after = int(request.url.params["afterId"])
            size = int(request.url.params["size"])
            data = [item for item in targets if item["jobId"] > after][:size]
            return httpx.Response(200, json={"data": data})
        return httpx.Response(200, json={"data": None})

    monkeypatch.setattr(analysis_ogonggo, "transport", httpx.MockTransport(handle))
    return seen, lambda: seen.clear()


def target(job_id: int, url: str, source: str = "CRAWLER") -> dict[str, Any]:
    return {
        "jobId": job_id,
        "source": source,
        "sourceUrl": url,
        "companyName": "오공고",
        "title": TITLE,
    }


async def test_마감만_보내고_나뉜_공고의_원문은_한_번만_연다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen, _ = server(
        [
            target(1, "https://a.test/1#1"),
            target(2, "https://a.test/1#2"),
            target(3, "https://b.test/3", source="COMPANY"),
            target(4, "https://c.test/4", source="WORK24"),
        ],
        monkeypatch,
    )
    pages = Pages(
        {
            "https://a.test/1": ResponseStatusError("gone", 404),
            "https://b.test/3": page("https://b.test/3"),
            "https://c.test/4": TransportError("시간 초과"),
        }
    )
    monkeypatch.setattr(judge_module, "_no_such_host", lambda url: False)

    result = await runner.run_once(
        conn, settings=SETTINGS, fetcher=pages, renderer=Pages({}), report=False
    )

    closes = [r.url.path for r in seen if r.method == "POST"]
    assert closes == [
        "/api/v1/internal/jobs/1/source-check-close",
        "/api/v1/internal/jobs/2/source-check-close",
    ]
    assert seen[0].headers["X-Internal-Api-Key"] == KEY
    assert pages.calls.count("https://a.test/1") == 1
    assert (result.closed, result.count(OPEN), result.count(UNKNOWN)) == (2, 1, 1)
    assert result.finished
    assert runner.read_cursor(conn) == 0


async def test_멈춤_시각이면_본_데까지_남기고_다음에_이어_본다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = [target(i, f"https://s{i}.test/{i}") for i in range(1, 151)]
    seen, _ = server(targets, monkeypatch)
    pages = Pages({item["sourceUrl"]: page(item["sourceUrl"]) for item in targets})
    calls = {"n": 0}

    def past(stop_at: datetime | None) -> bool:
        calls["n"] += 1
        return calls["n"] > 2  # 첫 쪽 앞의 두 번만 통과한다

    monkeypatch.setattr(runner, "_past", past)

    first = await runner.run_once(
        conn,
        settings=SETTINGS,
        fetcher=pages,
        renderer=Pages({}),
        report=False,
        stop_at=datetime.now() + timedelta(hours=1),
    )

    assert len(first.checked) == runner.PAGE_SIZE
    assert not first.finished
    assert runner.read_cursor(conn) == runner.PAGE_SIZE

    monkeypatch.setattr(runner, "_past", lambda stop_at: False)
    second = await runner.run_once(
        conn, settings=SETTINGS, fetcher=pages, renderer=Pages({}), report=False
    )

    assert [item.target.job_id for item in second.checked][0] == runner.PAGE_SIZE + 1
    assert second.finished
    assert runner.read_cursor(conn) == 0


async def test_손으로_돌리면_마감하지_않고_이어_볼_자리도_옮기지_않는다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen, _ = server([target(1, "https://a.test/1")], monkeypatch)
    runner.write_cursor(conn, 0)

    result = await runner.run_once(
        conn,
        settings=SETTINGS,
        fetcher=Pages({"https://a.test/1": ResponseStatusError("gone", 404)}),
        renderer=Pages({}),
        close=False,
        limit=5,
    )

    assert result.count(CLOSED) == 1
    assert {r.method for r in seen} == {"GET"}
    assert runner.read_cursor(conn) == 0


def test_보고에_마감한_공고와_모르겠음_사유를_적는다() -> None:
    result = runner.RunResult(finished=True, closed=1)
    t = runner.Target(1, "CRAWLER", "https://a.test/1", "오공고", TITLE)
    result.checked = [
        runner.Checked(t, judge_module.Verdict(CLOSED, "응답 404")),
        runner.Checked(t, judge_module.Verdict(UNKNOWN, "응답 403")),
    ]

    lines = runner.report_lines(result)

    assert lines[0] == "확인 2건 · 모집 중 0 · 마감 1 · 모르겠음 1"
    assert any("마감 1 오공고" in line and "응답 404" in line for line in lines)
    assert lines[-1] == "모르겠음 사유: 응답 403 1"


async def test_시작과_끝에_슬랙을_한_통씩_보낸다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = [target(1, "https://a.test/1")]
    server(targets, monkeypatch)
    pages = Pages({"https://a.test/1": page("https://a.test/1")})
    sent: list[tuple[str, list[str], str]] = []

    async def report(name: str, lines: list[str], *, ok: bool = True, state: str = "") -> None:
        sent.append((name, list(lines), state))

    monkeypatch.setattr(runner.work_report, "report", report)
    runner.write_cursor(conn, 0)

    await runner.run_once(
        conn,
        settings=SETTINGS,
        fetcher=pages,
        renderer=Pages({}),
        stop_at=datetime(2026, 11, 15, 13, 20),
    )

    assert [(name, state) for name, _, state in sent] == [
        (runner.REPORT_NAME, "시작"),
        (runner.REPORT_NAME, ""),
    ]
    assert sent[0][1] == ["처음 공고부터 본다 · 13:20 에 멈춘다"]


async def test_손으로_돌리면_시작_알림도_보내지_않는다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    server([target(1, "https://a.test/1")], monkeypatch)
    sent: list[str] = []

    async def report(name: str, lines: list[str], **kw: object) -> None:
        sent.append(name)

    monkeypatch.setattr(runner.work_report, "report", report)

    await runner.run_once(
        conn,
        settings=SETTINGS,
        fetcher=Pages({"https://a.test/1": page("https://a.test/1")}),
        renderer=Pages({}),
        close=False,
    )

    assert sent == []


def test_시작_알림은_이어_볼_자리를_적는다() -> None:
    assert runner.start_lines(120, None) == ["공고 120 다음부터 이어서 본다"]
