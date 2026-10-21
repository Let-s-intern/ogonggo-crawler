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
    # 제목으로 대상임이 드러난 공고도 본문으로 다시 묻는다. 경력은 대개 본문에만 있다
    assert fake.asked_bodies == [
        "[마케팅] 퍼포먼스 마케터",
        "2026년 BNK캐피탈 신입사원 채용",
        "2026년 하반기 공개채용",
    ]
    assert stored_links(conn) == [f"{LIST_URL}/1", f"{LIST_URL}/3"]
    assert screened(conn) == [(f"{LIST_URL}/2", "title"), (f"{LIST_URL}/4", "body")]
    assert (result.new_count, result.skipped_count, result.fail_count) == (2, 2, 0)


async def test_제목이_대상이어도_본문의_경력이_대상_밖이면_거른다(
    conn: sqlite3.Connection,
) -> None:
    # 제목에는 경력이 없고 본문에 "경력 5년 이상" 이 있는 공고다
    fake = FakeScreener(
        conn,
        titles={"[마케팅] 퍼포먼스 마케터": screen.IN},
        bodies={"[마케팅] 퍼포먼스 마케터": screen.OUT},
    )

    await run_once(conn, target(), collectors=collectors(StubDetail("본문"), ITEMS), screener=fake)

    assert f"{LIST_URL}/1" not in stored_links(conn)
    assert (f"{LIST_URL}/1", "body") in screened(conn)


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


def test_대상_직군은_저장된_값이_없으면_기본_직군이고_모두_켜면_경력만_거른다(
    conn: sqlite3.Connection,
) -> None:
    assert target_fields.read_fields(conn) == target_fields.DEFAULT_FIELDS
    screener = screen.make_screener(conn)
    assert screener is not None and screener.fields == target_fields.DEFAULT_FIELDS

    target_fields.write_fields(conn, target_fields.choices())

    assert target_fields.filtering_fields(conn) == ()
    screener = screen.make_screener(conn)
    assert screener is not None and screener.fields == ()
    assert "n.job_field IN" not in spring.target_sql(conn)
    assert "experience_min_years" in spring.target_sql(conn)


def test_프롬프트는_최대_경력으로_대상을_가른다() -> None:
    """2026-10-19 결정: "2~10년" 은 2년차도 지원할 수 있지만 뽑힐 가능성이 낮아 거른다.

    위가 닫힌 범위는 상한 3년, "N년 이상" 처럼 위가 열린 범위는 2년 이상까지가 대상이다.
    """
    prompt = screen.body_prompt((), "시니어 백엔드", "경력 5년 이상 필수")

    assert "모든 직군이 대상이다" in prompt
    assert "요구 경력의 위가 3년 이하인 자리가 대상이다" in prompt
    assert '"2~10년"' in prompt and "아래가 낮아도 대상 밖이다" in prompt
    assert "2년 이상까지만 대상" in prompt and '"경력 3년 이상"' in prompt
    assert "우대" in prompt
    assert "경력 2~10년" in screen.title_prompt((), ["백엔드 경력 2~10년"])
    assert "IT·개발:" in screen.title_prompt(("IT·개발",), ["백엔드"])


def test_직군_하나씩_켜고_끄며_모두_끄지는_못한다(conn: sqlite3.Connection) -> None:
    """공고 목록의 직군 칸 스위치 하나가 `set_field` 한 번이다 (2026-10-19)."""
    assert target_fields.set_field(conn, "게임", True) == (
        "IT·개발",
        "게임",
        "기획·전략",
        "마케팅·광고",
        "영업",
        "HR·총무",
    )
    target_fields.set_field(conn, "영업", False)
    assert "영업" not in target_fields.read_fields(conn)

    for name in target_fields.read_fields(conn)[:-1]:
        target_fields.set_field(conn, name, False)
    with pytest.raises(target_fields.TargetFieldError):
        target_fields.set_field(conn, "HR·총무", False)
    assert target_fields.read_fields(conn) == ("HR·총무",)


def test_오공고_직군에_없는_이름은_저장하지_않는다(conn: sqlite3.Connection) -> None:
    with pytest.raises(target_fields.TargetFieldError):
        target_fields.write_fields(conn, ["IT·개발", "없는직군"])

    assert target_fields.read_fields(conn) == target_fields.DEFAULT_FIELDS


def test_전송도_같은_대상_직군을_읽는다(conn: sqlite3.Connection) -> None:
    target_fields.write_fields(conn, ["게임", "IT·개발"])

    sql = spring.target_sql(conn)

    assert "n.job_field IN ('IT·개발', '게임')" in sql


async def test_운영자가_지운_공고는_다음_실행이_다시_담지_않는다(conn: sqlite3.Connection) -> None:
    """지운 공고는 폐기한 것이다 (2026-10-21, `migrations/0061_discarded_jobs.sql`)."""
    from app.api.review_filter import _delete_rows

    await run_once(conn, target(), collectors=collectors(StubDetail("본문"), ITEMS), limit=None)
    deleted = conn.execute(
        "SELECT id FROM raw_jobs WHERE source_url = ?", (f"{LIST_URL}/2",)
    ).fetchone()[0]
    _delete_rows(conn, [deleted])

    detail = StubDetail("본문")
    result = await run_once(conn, target(), collectors=collectors(detail, ITEMS), limit=None)

    assert f"{LIST_URL}/2" not in stored_links(conn)
    assert detail.calls == []
    assert result.new_count == 0
