"""본문이 이미지로만 올라온 공고의 이미지를 수집할 때 읽어 원문에 붙인다 (2026-09-13 결정).

모델도 실사이트도 부르지 않는다. 이미지는 Pillow 로 그 자리에서 만들고, 받기는 가짜 fetch
클라이언트가, 읽기는 가짜 읽는 쪽이 한다.
"""

from __future__ import annotations

import io
import json
import pathlib
import sqlite3
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from PIL import Image

from app import db
from app.config import Settings
from app.crawler.collect import Collectors, HtmlDetailCollector
from app.crawler.fetcher import FetchError, FetchResult
from app.crawler.images import (
    IMAGE_TEXT_MARK,
    MAX_TILES,
    SHORT_BODY_CHARS,
    TILE_HEIGHT,
    TILE_OVERLAP,
    ImageReadError,
    LlmImageReader,
    needs_reading,
    read_detail_images,
    split_tall,
)
from app.crawler.parser import DetailParseResult, ListItem, ListParseResult, parse_detail
from app.crawler.runner import SCHEDULE, RunTarget, run_once
from app.llm.base import ImageInput, LlmCallError
from app.llm.claude import CLAUDE
from app.llm.openai_compat import QWEN_PROVIDER
from app.selector.schema import DETAIL_FIELDS, validate_selectors
from tests.test_llm_claude_gpt import FakeAnthropic
from tests.test_llm_qwen import FakeClient as FakeOpenAiClient
from tests.test_selector_generator import FakeClient as FakeGeminiClient

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SEEDS = pathlib.Path(__file__).parent.parent / "seeds" / "site-configs-20260826.json"
CONFIGS = {
    entry["name"]: entry for entry in json.loads(SEEDS.read_text(encoding="utf-8"))["crawlers"]
}

PAGE = "https://example.test/jobs/1"
IMAGE_URL = "https://example.test/a.png"
READ_TEXT = "주요업무\n파트너 발굴"


def png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def heights(parts: Sequence[ImageInput]) -> list[int]:
    return [Image.open(io.BytesIO(part.data)).size[1] for part in parts]


class FakeFetcher:
    """`request()` 만 흉내 낸다. 주소마다 돌려줄 바이트를 정해 둔다."""

    def __init__(self, contents: dict[str, bytes], error: Exception | None = None) -> None:
        self.contents = contents
        self.error = error
        self.urls: list[str] = []

    async def request(self, url: str, **_: Any) -> FetchResult:
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return FetchResult(url=url, status_code=200, text="", content=self.contents[url])


class FakeReader:
    def __init__(self, text: str = READ_TEXT, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[list[ImageInput]] = []

    async def read(self, parts: Sequence[ImageInput]) -> str:
        self.calls.append(list(parts))
        if self.error is not None:
            raise self.error
        return self.text


def short_detail(body: str = "구분 : 신입") -> DetailParseResult:
    return DetailParseResult(
        fields={"title": "공고", "body": body},
        missing=[],
        source_text="구분 : 신입",
        images=("/a.png",),
    )


def test_본문이_짧고_이미지가_있을_때만_읽는다() -> None:
    assert needs_reading("가" * (SHORT_BODY_CHARS - 1), ["/a.png"])
    assert not needs_reading("가" * SHORT_BODY_CHARS, ["/a.png"])
    assert not needs_reading("짧다", [])


def test_짧은_이미지는_받은_그대로_하나로_보낸다() -> None:
    data = png(100, 200)

    parts = split_tall(data)

    assert parts == [ImageInput(data=data, mime_type="image/png")]


def test_세로로_긴_이미지는_조금씩_겹쳐_자른다() -> None:
    """경계에 걸린 줄이 어느 조각에서든 온전히 보이게 겹친다."""
    parts = split_tall(png(100, 3000))

    step = TILE_HEIGHT - TILE_OVERLAP
    assert heights(parts) == [TILE_HEIGHT, TILE_HEIGHT, 3000 - 2 * step]
    assert {part.mime_type for part in parts} == {"image/jpeg"}


def test_열_수_없는_바이트는_읽기_실패다() -> None:
    with pytest.raises(ImageReadError):
        split_tall(b"not an image")


async def test_이미지에서_읽은_글을_원문_끝에_붙인다() -> None:
    fetcher = FakeFetcher({IMAGE_URL: png(100, 3000)})
    reader = FakeReader()

    result = await read_detail_images(short_detail(), PAGE, fetcher, reader)

    assert result.source_text == f"구분 : 신입\n{IMAGE_TEXT_MARK}\n{READ_TEXT}"
    assert fetcher.urls == [IMAGE_URL]
    assert len(reader.calls[0]) == 3
    assert result.notes == ()


async def test_본문이_충분하면_이미지를_받지도_읽지도_않는다() -> None:
    fetcher = FakeFetcher({IMAGE_URL: png(100, 100)})
    reader = FakeReader()
    detail = short_detail(body="가" * SHORT_BODY_CHARS)

    result = await read_detail_images(detail, PAGE, fetcher, reader)

    assert result is detail
    assert (fetcher.urls, reader.calls) == ([], [])


@pytest.mark.parametrize(
    ("fetch_error", "read_error"),
    [
        (FetchError("응답 404"), None),
        (None, LlmCallError("no_api_key", "GEMINI_API_KEY 가 비어 있다")),
    ],
)
async def test_받기나_읽기가_실패하면_원문은_그대로_두고_사유를_남긴다(
    fetch_error: Exception | None, read_error: Exception | None
) -> None:
    """공고는 저장한다. 이미지 하나 때문에 그 공고를 통째로 잃지 않는다."""
    fetcher = FakeFetcher({IMAGE_URL: png(100, 100)}, error=fetch_error)
    reader = FakeReader(error=read_error)

    result = await read_detail_images(short_detail(), PAGE, fetcher, reader)

    assert result.source_text == "구분 : 신입"
    assert len(result.notes) == 1
    assert "이미지를 읽지 못해" in result.notes[0]


async def test_한_호출에_싣는_조각은_상한까지다() -> None:
    fetcher = FakeFetcher({IMAGE_URL: png(50, TILE_HEIGHT * 20)})
    reader = FakeReader()

    await read_detail_images(short_detail(), PAGE, fetcher, reader)

    assert len(reader.calls[0]) == MAX_TILES


def parsed(site: str, fixture: str) -> DetailParseResult:
    html = (FIXTURES / fixture).read_text(encoding="utf-8")
    return parse_detail(html, validate_selectors(CONFIGS[site]["selectors"]).detail)


def test_우아한형제들은_원문_영역의_이미지를_읽을_대상이다() -> None:
    result = parsed("우아한형제들", "woowa-detail-R2607031-20260826.html")

    assert result.images == (
        "https://career-cdn.woowayouths.com/public/editor/recruit/2026/8/0/"
        "a7cf9f6a-67ef-4b25-b747-6aa934e0d9b5.jpg",
    )
    assert needs_reading(result.fields["body"], result.images)


def test_글이_충분한_토스는_배너가_있어도_읽을_대상이_아니다() -> None:
    result = parsed("토스", "toss-detail-7827417003-20260826.html")

    assert result.images
    assert not needs_reading(result.fields["body"], result.images)


SELECTORS = validate_selectors(
    {
        "list": {"item": "ul.jobs > li", "title": "h3", "link": "a", "date": "span.d"},
        "detail": {
            "title": "p.title",
            "body": "div.body",
            "requirements": "",
            "deadline": "",
            "department": "",
        },
    }
)
PAGE_HTML = (
    "<html><body><main><p class='title'>공고</p><div class='body'>구분 : 신입</div>"
    "<img src='/a.png'></main></body></html>"
)


class StubSource:
    async def fetch(self, url: str) -> FetchResult:
        return FetchResult(url=url, status_code=200, text=PAGE_HTML)


async def test_상세_수집기가_이미지를_읽어_원문에_붙인다() -> None:
    fetcher = FakeFetcher({IMAGE_URL: png(100, 100)})
    reader = FakeReader()
    collector = HtmlDetailCollector(StubSource(), SELECTORS.detail, fetcher=fetcher, reader=reader)

    result = await collector.collect(ListItem(index=0, title="공고", link=PAGE, date=""))

    assert result.source_text.endswith(f"{IMAGE_TEXT_MARK}\n{READ_TEXT}")
    assert fetcher.urls == [IMAGE_URL]


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status)"
        " VALUES ('예시', 'https://example.test/jobs', 'draft')"
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '예시 채용')")
    try:
        yield connection
    finally:
        connection.close()


async def test_모델로_읽을_때_조각을_싣고_호출을_남긴다(conn: sqlite3.Connection) -> None:
    client = FakeGeminiClient(json.dumps({"lines": ["주요업무", " 파트너 발굴 ", ""]}))
    reader = LlmImageReader(
        conn,
        settings=Settings(gemini_api_key="테스트키", gemini_model="gemini-3.5-flash"),
        client=client,
    )

    text = await reader.read([ImageInput(data=png(10, 10), mime_type="image/png")])

    assert text == READ_TEXT
    contents = client.calls[0]["contents"]
    assert isinstance(contents, list) and isinstance(contents[-1], str)
    rows = conn.execute("SELECT feature, ok FROM llm_calls").fetchall()
    assert [tuple(row) for row in rows] == [("image_read", 1)]


async def test_claude_는_이미지를_base64_블록으로_글_앞에_싣는다() -> None:
    client = FakeAnthropic('{"lines": []}')

    await CLAUDE.call_model(
        client,
        "claude-haiku-4-5-20251001",
        "읽어라",
        1,
        "이미지 읽기",
        response_schema=dict,
        system_instruction="지시",
        images=[ImageInput(data=b"abc", mime_type="image/png")],
    )

    content = client.calls[0]["messages"][0]["content"]
    assert content[0] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "YWJj"},
    }
    assert content[-1] == {"type": "text", "text": "읽어라"}


async def test_openai_호환은_이미지를_데이터_주소로_글_앞에_싣는다() -> None:
    client = FakeOpenAiClient('{"lines": []}')

    await QWEN_PROVIDER.call_model(
        client,
        "qwen3.8-flash",
        "읽어라",
        1,
        "이미지 읽기",
        response_schema=dict,
        system_instruction="지시",
        images=[ImageInput(data=b"abc", mime_type="image/png")],
    )

    content = client.calls[0]["messages"][1]["content"]
    assert content[0] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,YWJj"}}
    assert content[-1] == {"type": "text", "text": "읽어라"}


class OneItemList:
    async def collect(self) -> ListParseResult:
        item = ListItem(index=0, title="공고", link="https://example.test/jobs/1", date="")
        return ListParseResult(matched=1, items=[item], failures=[])


class NotedDetail:
    async def collect(self, item: ListItem) -> DetailParseResult:
        fields = dict.fromkeys(DETAIL_FIELDS, "")
        fields.update({"title": item.title, "body": "구분 : 신입"})
        return DetailParseResult(fields=fields, missing=[], notes=("이미지를 읽지 못했다",))


async def test_이미지를_읽지_못한_공고도_적재되고_사유는_실패로_세지_않는다(
    conn: sqlite3.Connection,
) -> None:
    collectors = Collectors(
        list_mode="static", detail_mode="static", list=OneItemList(), detail=NotedDetail()
    )
    target = RunTarget(
        list_url="https://example.test/jobs", selectors=SELECTORS, trigger=SCHEDULE, workflow_id=1
    )

    result = await run_once(conn, target, collectors=collectors, limit=1)

    assert (result.new_count, result.fail_count) == (1, 0)
    rows = conn.execute("SELECT reason, message FROM crawl_run_failures").fetchall()
    assert [tuple(row) for row in rows] == [(None, "이미지를 읽지 못했다")]
