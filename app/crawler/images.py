"""본문이 이미지로만 올라온 공고의 글자를 수집할 때 한 번 읽어 원문에 붙인다 (2026-09-13 결정).

우아한형제들 상세는 본문 글자가 187자뿐이고, 주요업무·지원자격·우대사항·근무조건·급여·전형
절차가 850×6966 이미지 한 장에 있다. 일곱 픽스처 중 그런 곳은 여기 하나다 — 그래서 본문이
짧고(`SHORT_BODY_CHARS` 미만) 원문 영역에 이미지가 있을 때만 읽는다. 토스처럼 글이 충분한
공고의 배너는 읽지 않는다.

| 단계 | 하는 일 |
|---|---|
| 받기 | 공용 fetch 클라이언트로 받는다. robots·딜레이·User-Agent 가 같다 |
| 조각내기 | 세로로 긴 이미지는 조금씩 겹쳐 자른다. Claude·GPT 는 큰 이미지를 줄여 글자가 뭉개진다 |
| 읽기 | '이미지 읽기' 기능이 고른 제공자로 조각 전부를 한 번에 부른다 |
| 붙이기 | 원문 끝에 붙인다. 분류는 원문을 읽으므로 분류 쪽은 그대로다 |

**실패해도 공고는 저장한다.** 이미지를 못 받았거나 키가 없거나 호출이 실패하면 이미지 글 없이
적재하고 사유를 실행 기록에 남긴다. 수집은 아는 주소를 다시 열지 않으므로 그 공고의 이미지
글은 비어 있는 채로 남는다.
"""

from __future__ import annotations

import io
import logging
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

from PIL import Image
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.crawler.fetcher import FetchError, FetchPolicy
from app.crawler.parser import DetailParseResult
from app.llm import settings as llm_settings
from app.llm.base import ImageInput, LlmCallError, Usage
from app.llm.log import IMAGE_READ, record_call
from app.llm.providers import for_feature

logger = logging.getLogger(__name__)

# 본문 글자가 이보다 짧아야 이미지를 읽는다. 우아한형제들이 187자, 그다음으로 짧은 롯데가
# 1,141자다 (2026-09-13 측정)
SHORT_BODY_CHARS = 500
# 한 공고에서 받는 이미지 수와 한 장의 크기, 한 호출에 싣는 조각 수의 상한. 잘못 잡힌 영역이
# 사진첩이어도 호출 하나가 한없이 커지지 않게 한다
MAX_IMAGES = 5
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TILES = 8
# 조각 하나의 높이와 조각끼리 겹치는 높이. 경계에 걸린 줄이 어느 조각에서든 온전히 보이게 겹친다
TILE_HEIGHT = 1400
TILE_OVERLAP = 80
# 원문에서 이미지에서 읽은 글이 시작하는 자리
IMAGE_TEXT_MARK = "[이미지에 적힌 글]"

# 제공자가 그대로 받는 형식. 이 밖의 형식은 JPEG 로 바꿔 보낸다
_SENDABLE = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
_SPACES = re.compile(r"\s+")

IMAGE_READ_INSTRUCTION = (
    "너는 채용공고 이미지에 적힌 글자를 옮겨 적는다. 보이는 글자를 위에서 아래로 적힌 그대로 "
    "옮기고, 요약하거나 다듬거나 없는 글자를 지어내지 않는다."
)

IMAGE_READ_PROMPT = """아래 이미지는 채용공고 한 건을 위에서부터 차례로 자른 조각이다.
조각 경계는 조금씩 겹쳐 있어, 앞 조각 끝과 다음 조각 처음에 같은 줄이 보이면 한 번만 적는다.

- 이미지에 적힌 글자를 한 줄씩 lines 에 그대로 옮긴다. 단어를 바꾸거나 요약하지 않는다.
- 소제목(`주요업무`, `지원자격` 같은)도 한 줄로 적는다.
- 표는 한 칸씩 `이름: 값` 으로 적는다(`근무형태: 인턴(3개월 계약)`).
- 화살표로 이어진 단계는 `입사지원 > 서류전형` 처럼 한 줄로 적는다.
- 장식·아이콘·로고처럼 글자가 아닌 것은 적지 않는다.
"""


class ImageReadError(RuntimeError):
    """이미지를 받거나 조각내거나 읽은 답을 쓸 수 없다. 공고는 이미지 글 없이 저장된다."""


class ImageText(BaseModel):
    """이미지에서 읽은 글. 적힌 순서대로 한 줄씩이다."""

    lines: list[str] = Field(default_factory=list)


class ImageReader(Protocol):
    """이미지 조각을 읽어 글로 돌려주는 것. 실행은 모델을 부르는 것을, 테스트는 가짜를 쓴다."""

    async def read(self, parts: Sequence[ImageInput]) -> str: ...


def needs_reading(body: str, images: Sequence[str]) -> bool:
    """이 공고의 이미지를 읽어야 하는가. 본문이 짧고 원문 영역에 이미지가 있을 때만이다."""
    return bool(images) and len(_SPACES.sub(" ", body).strip()) < SHORT_BODY_CHARS


def split_tall(data: bytes) -> list[ImageInput]:
    """세로로 긴 이미지를 조금씩 겹쳐 자른 조각들. 짧고 보낼 수 있는 형식이면 원래 바이트 하나다."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            width, height = image.size
            mime_type = Image.MIME.get(image.format or "", "")
            if height <= TILE_HEIGHT and mime_type in _SENDABLE:
                return [ImageInput(data=data, mime_type=mime_type)]
            rgb = image.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageReadError(f"이미지를 열 수 없다: {exc}") from exc

    parts: list[ImageInput] = []
    top = 0
    while True:
        bottom = min(top + TILE_HEIGHT, height)
        buffer = io.BytesIO()
        rgb.crop((0, top, width, bottom)).save(buffer, format="JPEG", quality=90)
        parts.append(ImageInput(data=buffer.getvalue(), mime_type="image/jpeg"))
        if bottom >= height:
            return parts
        top = bottom - TILE_OVERLAP


async def read_detail_images(
    detail: DetailParseResult, page_url: str, fetcher: FetchPolicy, reader: ImageReader
) -> DetailParseResult:
    """읽어야 하는 공고면 이미지를 읽어 원문 끝에 붙인다.

    실패하면 원문은 그대로 두고 사유만 `notes` 에 남긴다.
    """
    if not needs_reading(detail.fields.get("body", ""), detail.images):
        return detail
    try:
        parts = await _download(detail.images, page_url, fetcher)
        text = (await reader.read(parts)).strip()
    except (FetchError, ImageReadError, LlmCallError) as exc:
        logger.warning("이미지를 읽지 못해 이미지 글 없이 저장한다 url=%s: %s", page_url, exc)
        note = f"이미지를 읽지 못해 이미지 글 없이 저장했다: {exc}"
        return replace(detail, notes=(*detail.notes, note))
    if not text:
        return replace(detail, notes=(*detail.notes, "이미지에서 읽은 글이 없다"))
    joined = f"{IMAGE_TEXT_MARK}\n{text}"
    source = f"{detail.source_text}\n{joined}" if detail.source_text.strip() else joined
    return replace(detail, source_text=source)


async def _download(
    sources: Sequence[str], page_url: str, fetcher: FetchPolicy
) -> list[ImageInput]:
    """이미지를 받아 조각낸다. 받을 수 없는 주소는 건너뛰고, 상한을 넘는 조각은 버린다."""
    parts: list[ImageInput] = []
    for source in list(dict.fromkeys(sources))[:MAX_IMAGES]:
        url = urljoin(page_url, source)
        if urlsplit(url).scheme not in ("http", "https"):
            continue
        result = await fetcher.request(url)
        if not result.content:
            raise ImageReadError(f"이미지가 비었다: {url}")
        if len(result.content) > MAX_IMAGE_BYTES:
            raise ImageReadError(
                f"이미지가 {len(result.content):,}바이트라 읽지 않는다"
                f"(상한 {MAX_IMAGE_BYTES:,}): {url}"
            )
        parts.extend(split_tall(result.content))
        if len(parts) >= MAX_TILES:
            break
    if not parts:
        raise ImageReadError("받을 수 있는 이미지 주소가 없다")
    return parts[:MAX_TILES]


class LlmImageReader:
    """'이미지 읽기' 기능이 고른 제공자로 조각 전부를 한 번에 읽는다.

    호출은 성공도 실패도 `llm_calls` 에 남긴다.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        settings: Settings | None = None,
        client: Any | None = None,
    ) -> None:
        self._conn = conn
        self._settings = settings
        self._client = client

    async def read(self, parts: Sequence[ImageInput]) -> str:
        # 화면에서 고른 제공자와 모델을 부를 때마다 다시 읽는다 (`app/llm/settings.py`)
        resolved = llm_settings.settings_for(self._conn, IMAGE_READ, self._settings)
        provider, model = for_feature(IMAGE_READ, resolved)
        client = self._client or provider.build_client(resolved)
        try:
            text, usage = await provider.call_model(
                client,
                model,
                IMAGE_READ_PROMPT,
                1,
                "이미지 읽기",
                response_schema=ImageText,
                system_instruction=IMAGE_READ_INSTRUCTION,
                images=parts,
            )
        except LlmCallError as exc:
            # 응답을 받지 못한 호출도 남긴다. 토큰은 알 수 없어 0 이다
            failed = Usage(
                provider=provider.name,
                model=model,
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                latency_ms=0,
            )
            record_call(
                self._conn, feature=IMAGE_READ, usage=failed, ok=False, error=f"{exc.reason}: {exc}"
            )
            raise
        record_call(self._conn, feature=IMAGE_READ, usage=usage)
        try:
            answer = ImageText.model_validate_json(text)
        except ValidationError as exc:
            raise ImageReadError(f"이미지 읽기 응답을 읽을 수 없다: {exc}") from exc
        return "\n".join(line.strip() for line in answer.lines if line.strip())
