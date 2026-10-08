"""수집에서 대상 직군 밖 공고를 거른다 (LC-3444, `app/crawler/screen.py`).

실사이트에도 AI 에도 나가지 않는다. 실행 흐름은 판정을 손으로 주는 가짜 거르개로 보고, 거르개가
AI 답을 읽는 것은 가짜 Gemini 클라이언트로 본다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator, Sequence

import pytest

from app import db
from app.config import Settings
from app.crawler import screen, target_fields
from app.crawler.parser import ListItem as Item
from app.crawler.runner import _is_known, run_once
from app.crawler.screen import BodyVerdict, Screener, ScreenError
from app.deliver import spring
from tests.test_body_required import LIST_URL, StubDetail, collectors, target
from tests.test_selector_generator import FakeClient as FakeGeminiClient

ITEMS = [
    Item(index=0, title="[마케팅] 퍼포먼스 마케터", link=f"{LIST_URL}/1", date=""),
    Item(index=1, title="[생산] 설비보전 엔지니어", link=f"{LIST_URL}/2", date=""),
    Item(index=2, title="2026년 BNK캐피탈 신입사원 채용", link=f"{LIST_URL}/3", date=""),
    Item(index=3, title="2026년 하반기 공개채용", link=f"{LIST_URL}/4", date=""),
]


class FakeScreener(Screener):
    """제목·본문 판정을 손으로 준다. 무엇을 물었는지 남긴다."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        titles: dict[str, str],
        bodies: dict[str, str],
        *,
        fail_titles: bool = False,
        fail_bodies: bool = False,
    ) -> None:
        super().__init__(conn, target_fields.DEFAULT_FIELDS)
        self._titles = titles
        self._bodies = bodies
        self._fail_titles = fail_titles
        self._fail_bodies = fail_bodies
        self.asked_titles: list[str] = []
        self.asked_bodies: list[str] = []

    async def titles(self, titles: Sequence[str]) -> list[str]:
        self.asked_titles.extend(titles)
        if self._fail_titles:
            raise ScreenError("호출 실패")
        return [self._titles.get(title, screen.UNKNOWN) for title in titles]

    async def body(self, title: str, body: str) -> BodyVerdict:
        self.asked_bodies.append(title)
        if self._fail_bodies:
            raise ScreenError("호출 실패")
        return BodyVerdict(verdict=self._bodies.get(title, screen.IN), reason="근거")


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES (?, ?, 'draft')", ("예시", LIST_URL)
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '예시 채용')")
    try:
        yield connection
    finally:
        connection.close()


def stored_links(conn: sqlite3.Connection) -> list[str]:
    return [str(row["source_url"]) for row in conn.execute("SELECT source_url FROM raw_jobs")]


def screened(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute("SELECT source_url, stage FROM screened_jobs ORDER BY id")
    return [(str(row["source_url"]), str(row["stage"])) for row in rows]


async def test_제목으로_대상_밖이면_상세를_열지_않고_모르면_본문으로_거른다(
    conn: sqlite3.Connection,
) -> None:
    fake = FakeScreener(
        conn,
        titles={"[마케팅] 퍼포먼스 마케터": screen.IN, "[생산] 설비보전 엔지니어": screen.OUT},
        bodies={"2026년 하반기 공개채용": screen.OUT},
    )
    detail = StubDetail("본문")

    result = await run_once(
        conn, target(), collectors=collectors(detail, ITEMS), limit=None, screener=fake
    )

    # 제목으로 거른 공고는 상세를 열지 않는다
    assert f"{LIST_URL}/2" not in detail.calls
    # 제목으로 대상임이 드러난 공고는 본문으로 다시 묻지 않는다
    assert fake.asked_bodies == ["2026년 BNK캐피탈 신입사원 채용", "2026년 하반기 공개채용"]
    assert stored_links(conn) == [f"{LIST_URL}/1", f"{LIST_URL}/3"]
    assert screened(conn) == [(f"{LIST_URL}/2", "title"), (f"{LIST_URL}/4", "body")]
    assert (result.new_count, result.skipped_count, result.fail_count) == (2, 2, 0)


async def test_거른_공고는_다음_실행이_아는_공고로_본다(conn: sqlite3.Connection) -> None:
    first = FakeScreener(conn, titles={"[생산] 설비보전 엔지니어": screen.OUT}, bodies={})
    await run_once(conn, target(), collectors=collectors(StubDetail("본문"), ITEMS), screener=first)

    again = FakeScreener(conn, titles={}, bodies={})
    detail = StubDetail("본문")
    await run_once(conn, target(), collectors=collectors(detail, ITEMS), screener=again)

    assert again.asked_titles == []
    assert detail.calls == []
    assert _is_known(conn, 1, "source_url", f"{LIST_URL}/2")


async def test_판정이_실패하면_수집한다(conn: sqlite3.Connection) -> None:
    fake = FakeScreener(conn, titles={}, bodies={}, fail_titles=True, fail_bodies=True)

    result = await run_once(
        conn, target(), collectors=collectors(StubDetail("본문"), ITEMS), screener=fake
    )

    assert len(stored_links(conn)) == len(ITEMS)
    assert screened(conn) == []
    assert result.fail_count == 0
    messages = [failure.message for failure in result.failures]
    assert any("제목으로 직군을 거르지 못해" in message for message in messages)
    assert any("본문으로 직군을 거르지 못해" in message for message in messages)


async def test_테스트_실행은_거르지_않는다(conn: sqlite3.Connection) -> None:
    from app.crawler.runner import TEST, RunTarget
    from tests.test_body_required import SELECTORS

    fake = FakeScreener(conn, titles={"[생산] 설비보전 엔지니어": screen.OUT}, bodies={})
    preview = RunTarget(list_url=LIST_URL, selectors=SELECTORS, trigger=TEST, crawler_id=1)

    await run_once(conn, preview, collectors=collectors(StubDetail("본문"), ITEMS), screener=fake)

    assert fake.asked_titles == []
    assert screened(conn) == []


async def test_거르개는_ai_답을_읽고_호출을_collect_screen_으로_남긴다(
    conn: sqlite3.Connection,
) -> None:
    answers = [
        json.dumps({"items": [{"index": 0, "verdict": "out"}]}),
        json.dumps({"verdict": "out", "fields": ["생산·기능직"], "reason": "생산직만 뽑는다"}),
    ]
    client = FakeGeminiClient(*answers)
    screener = Screener(
        conn,
        target_fields.DEFAULT_FIELDS,
        settings=Settings(gemini_api_key="테스트키", gemini_model="gemini-3.5-flash"),
        client=client,
    )

    # 답에 빠진 제목은 모른다로 본다
    assert await screener.titles(["[생산] 설비", "신입사원 채용"]) == ["out", "unknown"]
    verdict = await screener.body("신입사원 채용", "생산직 모집")

    assert verdict.verdict == "out"
    assert "생산·기능직" in verdict.reason
    prompt = str(client.calls[0]["contents"])
    assert "마케팅·광고" in prompt and "퍼포먼스마케팅" in prompt
    rows = conn.execute("SELECT feature, ok FROM llm_calls").fetchall()
    assert [tuple(row) for row in rows] == [("collect_screen", 1), ("collect_screen", 1)]


def test_대상_직군은_저장된_값이_없으면_기본_직군이고_비우면_거르지_않는다(
    conn: sqlite3.Connection,
) -> None:
    assert target_fields.read_fields(conn) == target_fields.DEFAULT_FIELDS
    assert screen.make_screener(conn) is not None

    target_fields.write_fields(conn, [])

    assert target_fields.read_fields(conn) == ()
    assert screen.make_screener(conn) is None
    assert "job_field" not in spring.target_sql(conn)


def test_오공고_직군에_없는_이름은_저장하지_않는다(conn: sqlite3.Connection) -> None:
    with pytest.raises(target_fields.TargetFieldError):
        target_fields.write_fields(conn, ["IT·개발", "없는직군"])

    assert target_fields.read_fields(conn) == target_fields.DEFAULT_FIELDS


def test_전송도_같은_대상_직군을_읽는다(conn: sqlite3.Connection) -> None:
    target_fields.write_fields(conn, ["디자인", "IT·개발"])

    sql = spring.target_sql(conn)

    assert "'IT·개발'" in sql and "'디자인'" in sql
    assert "'마케팅·광고'" not in sql
