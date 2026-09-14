"""화면에 그리는 시각의 시간대 (21.1).

2026-08-24 05:41 KST 에 워크플로우 화면의 최근 실행이 `2026-08-23 20:15` 로 떠 있었다. SQLite
`datetime('now')` 가 UTC 를 넣고 화면이 그 값을 그대로 그린 것이다. 9시간 어긋난 채로는 "방금
돌았나" 를 판단할 수 없다.

저장된 값은 UTC 그대로 둔다. `normalized_at` 은 제공 API 의 폴링 커서라 값이 밀리면 소비 측이
이미 받은 것을 다시 받거나 못 받은 구간이 생긴다 (`.claude/docs/api-contract.md`). 바꾸는 것은
화면에 그리는 순간뿐이고, 그 순간은 이 필터 하나다.

| 확인 | 깨지면 |
|---|---|
| 저장 형식(`YYYY-MM-DD HH:MM:SS`)을 설정된 시간대로 옮긴다 | 화면이 9시간 전을 가리킨다 |
| 시간대가 붙은 ISO 형식도 같은 결과를 낸다 | 같은 화면에 두 시간대가 섞인다 |
| 자정을 넘는 값은 날짜까지 바뀐다 | 시각만 맞고 날짜가 하루 어긋난다 |
| 값 옆에 시간대 약칭을 적는다 | 어느 시간대인지 몰라 9시간 차이를 다시 의심한다 |
| 빈 값은 빈 문자열이다 | `None` 이 화면에 그대로 찍힌다 |
| 읽지 못한 값에서 예외가 나지 않는다 | 값 하나 때문에 화면 전체가 500 이 된다 |
| 시간대 설정이 틀리면 UTC 로 떨어진다 | 오타 하나로 모든 화면이 죽는다 |
"""

from __future__ import annotations

import pathlib
import re
from collections.abc import Iterator

import pytest

from app.api import ui
from app.config import Settings, get_settings


@pytest.fixture(autouse=True)
def clear_caches() -> Iterator[None]:
    """설정과 시간대 조회 둘 다 캐시된다. 테스트끼리 값을 물려주지 않게 매번 비운다."""
    get_settings.cache_clear()
    ui._zone.cache_clear()
    yield
    get_settings.cache_clear()
    ui._zone.cache_clear()


@pytest.fixture
def seoul(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY_TIMEZONE", "Asia/Seoul")


def test_기본값은_서울이다(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """운영자가 한 명이고 그가 한국에 있다는 것이 이 서비스의 전제다."""
    monkeypatch.delenv("DISPLAY_TIMEZONE", raising=False)
    monkeypatch.chdir(tmp_path)

    assert Settings().display_timezone == "Asia/Seoul"


def test_저장_형식이_9시간_뒤로_나온다(seoul: None) -> None:
    """SQLite `datetime('now')` 가 넣는 형식이다. 시간대 표시가 없고 값은 UTC 다."""
    assert ui.format_time("2026-08-23 20:15:00") == "2026-08-24 05:15:00 KST"


def test_ISO_형식도_같은_결과를_낸다(seoul: None) -> None:
    """재정규화 진행과 스케줄러는 시간대가 붙은 ISO 문자열을 넣는다."""
    assert ui.format_time("2026-08-23T20:15:00Z") == "2026-08-24 05:15:00 KST"
    assert ui.format_time("2026-08-23T20:15:00+00:00") == "2026-08-24 05:15:00 KST"


def test_자정을_넘으면_날짜가_바뀐다(seoul: None) -> None:
    """시각만 맞추고 날짜를 그대로 두면 하루 어긋난 값이 화면에 남는다."""
    assert ui.format_time("2026-08-23 15:00:00") == "2026-08-24 00:00:00 KST"
    assert ui.format_time("2026-08-23 14:59:59") == "2026-08-23 23:59:59 KST"


def test_시간대_약칭이_값에_붙는다(seoul: None) -> None:
    assert ui.format_time("2026-08-23 20:15:00").endswith(" KST")


def test_빈_값은_빈_문자열이다(seoul: None) -> None:
    """`None` 을 그대로 그리지 않는다. 화면에 `None` 이 찍히면 값이 있는 것처럼 보인다."""
    assert ui.format_time(None) == ""
    assert ui.format_time("") == ""
    assert ui.format_time("   ") == ""


def test_읽지_못한_값은_예외_없이_원문으로_나온다(seoul: None) -> None:
    """값 하나가 화면 전체를 죽이지 않는다."""
    assert ui.format_time("어제") == "어제"
    assert ui.format_time("2026-13-45 99:99:99") == "2026-13-45 99:99:99"
    assert ui.format_time(0) == ""


def test_시간대_설정이_틀리면_UTC_로_떨어진다(monkeypatch: pytest.MonkeyPatch) -> None:
    """오타 하나로 모든 화면이 죽지 않는다. 값 옆의 `UTC` 가 설정이 틀렸다고 말한다."""
    monkeypatch.setenv("DISPLAY_TIMEZONE", "Asia/Seuol")

    assert ui.format_time("2026-08-23 20:15:00") == "2026-08-23 20:15:00 UTC"


def test_다른_시간대를_설정하면_그대로_따른다(monkeypatch: pytest.MonkeyPatch) -> None:
    """시간대는 설정에서 온다. 필터 안에 9시간이 박혀 있지 않다."""
    monkeypatch.setenv("DISPLAY_TIMEZONE", "UTC")

    assert ui.format_time("2026-08-23 20:15:00") == "2026-08-23 20:15:00 UTC"


# --- 21.2 시각을 그리는 모든 자리가 이 필터를 거치는가 ---

TEMPLATES = pathlib.Path(__file__).parent.parent / "app" / "templates"
EXPRESSION = re.compile(r"\{\{(.+?)\}\}", re.DOTALL)
TIME_NAME = re.compile(r"\b\w+_at\b")
# 이름만 `_at` 으로 끝나는 모집 일시다. 사이트가 적은 날짜 글자라 UTC 시각이 아니고 필터를 거치면
# 오히려 틀린다 (`migrations/0031_spring_field_names.sql` 이 오공고 이름에 맞추며 붙은 꼬리다)
POSTING_DATES = ("recruitment_start_at", "recruitment_end_at")
# 매크로에 값을 넘기기만 하는 자리다. 그리는 것은 매크로 안이고 거기서 필터를 거친다
MACRO_CALLS = ("review_delivery(",)


def test_시각을_그리는_모든_자리가_필터를_거친다() -> None:
    """한 곳이라도 빠지면 그 화면만 9시간 어긋난다. 섞인 화면은 전부 어긋난 화면보다 나쁘다."""
    leaked: list[str] = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for expression in EXPRESSION.findall(line):
                if not [
                    name for name in TIME_NAME.findall(expression) if name not in POSTING_DATES
                ]:
                    continue
                if "as_time" in expression:
                    continue
                if any(call in expression for call in MACRO_CALLS):
                    continue
                leaked.append(f"{path.relative_to(TEMPLATES)}:{number}: {expression.strip()}")

    assert leaked == []
