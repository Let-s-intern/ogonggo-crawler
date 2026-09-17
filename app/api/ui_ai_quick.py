"""설정 > AI 의 빠른 설정. DeepSeek API 키 하나만 넣으면 분류와 셀렉터 생성·수정이 DeepSeek 로 돈다.

2026-09-17 결정(LC-3344). 제공자 화면은 키·기능·모델·회사 정의가 한 폼에 있어 무엇을 넣어야 하는지
알기 어려웠다. 운영에서 쓰는 조합이 하나라, 그 조합을 키 하나로 끝내고 나머지는 `다른 AI 쓰기`
안에 접는다.

넣는 것은 화면의 "회사 추가"·"키 저장"·"기능에 지정" 과 같은 함수다 (`app/llm/settings.py`).
정의는 2026-09-17 실제 등록 시험에서 쓴 값이다 — 베타 주소, strict 도구 호출, 생각 모드 끔. 모델은
DeepSeek 가 주는 목록에서 `flash` 를 먼저 고른다. 이미지 읽기는 DeepSeek 가 이미지를 받지 않아
지정하지 않는다.

단가는 넣지 않는다. 확인하지 않은 단가를 적으면 비용 화면이 틀린 값을 믿게 된다 — 비용 화면이 단가
없음을 알리고, 운영자가 설정 > AI 의 모델 단가에 넣는다.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.api.settings import get_connection
from app.api.ui import render
from app.llm import custom
from app.llm import settings as store
from app.llm.log import CLASSIFY, SELECTOR_GENERATE, SELECTOR_REPAIR

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ui"], include_in_schema=False)

NAME = "deepseek"
FEATURES: tuple[str, ...] = (CLASSIFY, SELECTOR_GENERATE, SELECTOR_REPAIR)
DEFINITION: dict[str, object] = {
    "label": "DeepSeek",
    "base_url": "https://api.deepseek.com/beta",
    "schema_mode": custom.STRICT_TOOL,
    "temperature": "",
    "max_tokens": "8192",
    "images": False,
    "list_models": True,
    "extra_body": '{"thinking": {"type": "disabled"}}',
    "prices": "",
    "models_url": "https://api.deepseek.com",
}


def _state(conn: sqlite3.Connection) -> dict[str, object]:
    config = store.read_config(conn)
    defined = any(item.name == NAME for item in config.customs)
    key = next((view for view in config.keys if view.provider == NAME), None)
    using = [view for view in config.features if view.provider == NAME]
    return {
        "defined": defined,
        "key_present": bool(key and key.present),
        "key_tail": key.tail if key else "",
        "using": using,
        "model": using[0].model if using else "",
        "all_features": len(using) >= len(FEATURES),
    }


def _card(
    request: Request, conn: sqlite3.Connection, *, message: str = "", error: str = ""
) -> HTMLResponse:
    return render(
        request, "fragments/ai_quick.html", state=_state(conn), message=message, error=error
    )


@router.get("/ui/llm/quick", response_class=HTMLResponse)
def quick_card(
    request: Request, conn: Annotated[sqlite3.Connection, Depends(get_connection)]
) -> HTMLResponse:
    return _card(request, conn)


@router.post("/ui/llm/quick", response_class=HTMLResponse)
async def quick_save(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    api_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """키를 저장하고 DeepSeek 를 분류·셀렉터 생성·수정에 지정한다.

    키는 화면에 다시 그리지 않는다.
    """
    key = api_key.strip()
    if not key:
        return _card(request, conn, error="API 키가 비었습니다")
    try:
        if not _state(conn)["defined"]:
            store.write_custom(conn, NAME, **DEFINITION)  # type: ignore[arg-type]
        store.write_key(conn, NAME, key)
    except store.LlmSettingError as exc:
        return _card(request, conn, error=str(exc))

    models, reason = await store.list_models(conn, NAME)
    if not models:
        return _card(
            request,
            conn,
            error=(
                f"키는 저장했지만 DeepSeek 에 연결하지 못했습니다: {reason or '모델 목록이 비었다'}"
            ),
        )
    model = next((name for name in models if "flash" in name), None) or models[0]
    try:
        for feature in FEATURES:
            store.write_feature(conn, feature, NAME, model)
    except store.LlmSettingError as exc:
        return _card(request, conn, error=f"기능에 지정하지 못했습니다: {exc}")
    logger.info("빠른 설정: 분류·셀렉터 생성·수정을 %s/%s 로 지정했다", NAME, model)
    return _card(
        request, conn, message=f"연결했습니다. 분류와 셀렉터 생성·수정이 {model} 로 돕니다"
    )
