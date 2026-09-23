"""페이지 이동 기록. 브라우저 없이 요청 객체만 흉내 낸다."""

from __future__ import annotations

from typing import Any

from app.crawler.playwright import RequestLog


class FakeFrame:
    def __init__(self, parent: Any = None) -> None:
        self.parent_frame = parent


class FakeRequest:
    def __init__(self, url: str, *, navigation: bool = True, frame: Any = None) -> None:
        self.url = url
        self._navigation = navigation
        self._frame = frame

    def is_navigation_request(self) -> bool:
        return self._navigation

    @property
    def frame(self) -> Any:
        if self._frame is None:
            raise RuntimeError("Frame for this navigation request is not available")
        return self._frame


class FakeContext:
    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def on(self, event: str, handler: Any) -> None:
        assert event == "request"
        self.handlers.append(handler)

    def emit(self, request: FakeRequest) -> None:
        for handler in self.handlers:
            handler(request)


def test_새_탭의_첫_이동과_리다이렉트를_모두_적는다() -> None:
    """새 탭의 첫 이동은 frame 을 읽으면 예외가 난다(GC녹십자, 2026-09-22). 그래도 적는다."""
    log = RequestLog()
    context = FakeContext()
    log.watch_navigations(context)
    mark = log.navigation_mark()

    context.emit(FakeRequest("https://example.test/job-invite/2859/"))
    context.emit(FakeRequest("https://example.test/job/slug/4467050/", frame=FakeFrame()))
    context.emit(FakeRequest("https://example.test/api/data", navigation=False))
    context.emit(FakeRequest("https://example.test/ad", frame=FakeFrame(parent=FakeFrame())))

    assert log.navigations_since(mark) == [
        "https://example.test/job-invite/2859/",
        "https://example.test/job/slug/4467050/",
    ]


async def test_렌더한_문서에_iframe_의_글을_옮겨_적는다() -> None:
    """한화 실측(2026-09-22): 본문이 에디터 iframe 안에 있어 `page.content()` 에 없었다."""
    from app.crawler.playwright import _INLINE_FRAMES_JS, _inline_frames

    class FakePage:
        def __init__(self) -> None:
            self.scripts: list[str] = []

        async def evaluate(self, script: str) -> None:
            self.scripts.append(script)

    page = FakePage()
    await _inline_frames(page)

    assert page.scripts == [_INLINE_FRAMES_JS]
    assert "contentDocument" in _INLINE_FRAMES_JS
