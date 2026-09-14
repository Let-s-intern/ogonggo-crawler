"""제공자 설정. 값은 `app_settings` 표에 들어간다 — 새 표를 만들지 않는다.

`app/notify/settings.py` 와 같은 자리를 쓰면서 저장소만 따로 둔다. `app/settings.py` 는
`dict[str, int]` 를 내보내는 API 에 묶여 있어서 문자열 값을 얹으면 이미 있는 화면이 흔들린다.

**키는 제공자마다 한 벌, 모델은 제공자가 아니라 기능에 붙는다** (2026-08-27 결정).
모델을 제공자에 붙이면 같은 제공자를 쓰는 두 기능이 다른 모델을 쓸 수 없다. 셀렉터 생성에
비싼 모델을, 분류에 싼 모델을 두면서 키는 하나만 넣는 쓰임이 이 모양이라야 된다.

읽기는 환경변수로 떨어진다. 저장된 값이 없으면 배포가 넣어 둔 값을 쓰고, 한 번 저장된 뒤로는
DB 가 이긴다 (`app/settings.py` 와 같은 규칙). 읽기는 예외를 던지지 않는다 — 손으로 넣은 값
하나가 분류를 통째로 멈추게 둘 수 없다. 쓰기는 반대로 깐깐하다.

**키가 없는 제공자를 기능에 지정하면 거절한다.** 조용히 다른 제공자로 넘어가지 않는다
(`.claude/rules/llm.md`).

값을 화면에 다시 그리지 않는다. 내보내는 것은 있음·없음과 끝 네 자리뿐이다.

**화면에서 추가한 회사도 여기 들어간다** (2026-09-14 결정). 정의는 `llm_custom_{이름}` 행에
JSON 으로, 키는 다른 제공자와 같은 `llm_key_{이름}` 행에 들어간다. 코드에 든 다섯과 달리
환경변수로 떨어지는 값이 없다 (`app/llm/custom.py`).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.llm import custom
from app.llm import providers as registry
from app.llm.base import LlmCallError, Provider, Usage
from app.llm.log import FEATURES, record_call
from app.llm.openai_compat import custom_entry

logger = logging.getLogger(__name__)

# `app_settings` 의 행 이름. 접두사로 갈라 두면 스냅샷에서 이 설정만 골라내기도 쉽다
KEY_PREFIX = "llm_key_"
PROVIDER_PREFIX = "llm_provider_"
MODEL_PREFIX = "llm_model_"
CUSTOM_PREFIX = "llm_custom_"

# 화면에 보이는 자릿수. 이보다 짧거나 같은 값은 전부 가린다 — 네 자리 키의 끝 네 자리는
# 그 키 전체다 (`.claude/tasks/todo/prd-llm-providers.md` 4번)
TAIL = 4

# 연결 테스트 호출이 `llm_calls.feature` 에 남기는 이름. 기능이 아니라서 `FEATURES` 에 넣지
# 않는다 — 넣으면 제공자를 고르는 칸이 하나 더 생긴다. 일별 토큰·비용 합에는 들어간다
CONNECTION_TEST = "connection_test"

# 기능을 사람이 읽는 이름으로. 화면이 이 낱말을 그대로 쓴다
FEATURE_LABELS: dict[str, str] = {
    "selector_generate": "셀렉터 생성",
    "selector_repair": "AI 수정",
    "classify": "본문 분류",
    "image_read": "이미지 읽기",
}

# 연결 테스트가 보내는 글. 답이 작아야 토큰이 적게 든다. `JSON` 이라는 낱말이 있어야
# `json_object` 를 받아 주는 회사가 있다
CHECK_INSTRUCTION = "너는 연결 확인에 답한다. 정해진 모양으로만 짧게 답한다."
CHECK_PROMPT = '연결 확인이다. ok 는 true 로, word 는 "확인" 으로 JSON 으로 답한다.'


class LlmSettingError(ValueError):
    """저장할 수 없는 값. 거절 사유를 화면이 그대로 옮긴다."""


class ConnectionCheck(BaseModel):
    """연결 테스트가 받는 답의 모양."""

    ok: bool
    word: str


def key_row(provider: str) -> str:
    return f"{KEY_PREFIX}{provider}"


def provider_row(feature: str) -> str:
    return f"{PROVIDER_PREFIX}{feature}"


def model_row(feature: str) -> str:
    return f"{MODEL_PREFIX}{feature}"


def custom_row(name: str) -> str:
    return f"{CUSTOM_PREFIX}{name}"


# 코드에 든 제공자의 키 행과 기능 행. 화면에서 추가한 회사의 행은 이름이 정해져 있지 않아
# 여기 적을 수 없다 — 그 행까지 가리는 것이 `is_llm_row` 다
ROWS: tuple[str, ...] = (
    *(key_row(name) for name in sorted(registry.PROVIDERS)),
    *(provider_row(feature) for feature in FEATURES),
    *(model_row(feature) for feature in FEATURES),
)


def is_llm_row(key: str) -> bool:
    """이 저장소의 행인가. 스냅샷을 옮길 때 무엇을 옮기는지도 이 판정이 정한다."""
    return key in ROWS or key.startswith((CUSTOM_PREFIX, KEY_PREFIX))


@dataclass(frozen=True)
class KeyView:
    """제공자 하나의 키 상태. **값은 여기 없다.**"""

    provider: str
    present: bool
    # 끝 네 자리. 짧은 키는 빈 문자열이다 — 전체가 보이느니 아무것도 보이지 않는 편이 낫다
    tail: str
    # DB 에 저장된 값인가(True), 환경변수에서 온 값인가(False)
    stored: bool


@dataclass(frozen=True)
class FeatureView:
    """기능 하나가 무엇으로 도는가."""

    feature: str
    label: str
    provider: str
    model: str
    stored: bool
    # 이 조합으로 지금 부를 수 있는가. 못 부르면 그 사유가 `problem` 에 있다
    problem: str = ""


@dataclass(frozen=True)
class CustomView:
    """화면에서 추가한 회사 하나. 폼에 다시 채울 값이라 글자로 들고 있다. **키는 여기 없다.**"""

    name: str
    label: str = ""
    base_url: str = ""
    schema_mode: str = ""
    # 빈 글자는 "보내지 않음" 이다
    temperature: str = ""
    max_tokens: str = ""
    images: bool = False
    list_models: bool = True
    models_url: str = ""
    extra_body: str = ""
    prices: str = ""
    # 이 회사를 가리키는 기능의 화면 이름
    used_by: tuple[str, ...] = ()
    # 연결 테스트 칸에 미리 채울 모델. 이 회사를 쓰는 기능의 모델, 없으면 단가를 적은 첫 모델
    test_model: str = ""
    # 저장된 정의를 읽지 못한 사유. 있으면 이 회사는 어느 기능에서도 부를 수 없다
    problem: str = ""


@dataclass(frozen=True)
class LlmConfig:
    """화면이 그리는 설정 한 벌. 키 전체 값은 어디에도 없다."""

    keys: tuple[KeyView, ...]
    features: tuple[FeatureView, ...]
    customs: tuple[CustomView, ...] = ()
    # 기능에 고를 수 있는 제공자 이름. 코드에 든 다섯과 정의를 읽을 수 있는 추가한 회사다
    provider_names: tuple[str, ...] = ()

    def key(self, provider: str) -> KeyView:
        for view in self.keys:
            if view.provider == provider:
                return view
        raise LlmSettingError(f"제공자 `{provider}` 를 모른다")


@dataclass(frozen=True)
class ConnectionResult:
    """연결 테스트 한 번의 결과. 못 부른 것도 예외가 아니라 결과다 — 화면이 사유를 옮긴다."""

    ok: bool
    message: str


def mask(value: str) -> str:
    """화면에 내보낼 끝 네 자리. 네 자리 이하는 전부 가린다."""
    trimmed = value.strip()
    if len(trimmed) <= TAIL:
        return ""
    return trimmed[-TAIL:]


def read_config(conn: sqlite3.Connection, settings: Settings | None = None) -> LlmConfig:
    """저장된 설정. 없는 값은 환경변수로 떨어진다. 읽는 김에 채워 넣지 않는다."""
    base = settings or get_settings()
    stored = _stored(conn)
    entries = _entries(stored)
    names = tuple(sorted(entries))
    return LlmConfig(
        keys=tuple(_key_view(entries[name], stored, base) for name in names),
        features=tuple(_feature_view(feature, entries, stored, base) for feature in FEATURES),
        customs=_custom_views(stored, base),
        provider_names=names,
    )


def write_key(
    conn: sqlite3.Connection, provider: str, value: str, settings: Settings | None = None
) -> LlmConfig:
    """제공자 하나의 키를 저장한다. 빈 값을 주면 지우고 환경변수로 돌아간다."""
    _entry(_entries(_stored(conn)), provider)
    trimmed = value.strip()
    if trimmed:
        _upsert(conn, {key_row(provider): trimmed})
    else:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (key_row(provider),))
    return read_config(conn, settings)


def write_feature(
    conn: sqlite3.Connection,
    feature: str,
    provider: str,
    model: str,
    settings: Settings | None = None,
) -> LlmConfig:
    """기능 하나가 쓸 제공자와 모델을 저장한다. 하나라도 거절되면 아무것도 저장되지 않는다.

    모델을 비워 두면 그 행을 지운다. 그러면 그 제공자의 환경변수 모델을 따라간다 — 지금 값을
    베껴 넣어 두면 배포가 모델을 올려도 이 기능만 옛 모델에 남는다. 화면에서 추가한 회사는
    따라갈 환경변수 모델이 없어 모델을 적어야 한다.
    """
    if feature not in FEATURES:
        raise LlmSettingError(f"모르는 기능이다: {feature}")
    base = settings or get_settings()
    stored = _stored(conn)
    entry = _entry(_entries(stored), provider)

    if not _key_value(entry, stored, base):
        raise LlmSettingError(
            f"`{provider}` 의 API 키가 없다. 키를 먼저 저장한 뒤에 기능에 지정한다"
        )

    trimmed = model.strip()
    resolved_model = trimmed or entry.model_of(base)
    if not resolved_model:
        raise LlmSettingError(f"`{provider}` 는 따라갈 환경변수 모델이 없다. 모델 이름을 적는다")
    try:
        registry.resolve(feature, provider, resolved_model, _with_customs(base, stored))
    except LlmCallError as exc:
        raise LlmSettingError(str(exc)) from exc

    _upsert(conn, {provider_row(feature): provider})
    if trimmed:
        _upsert(conn, {model_row(feature): trimmed})
    else:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (model_row(feature),))
    return read_config(conn, base)


def write_custom(
    conn: sqlite3.Connection,
    name: str,
    *,
    label: str,
    base_url: str,
    schema_mode: str,
    temperature: str,
    max_tokens: str,
    images: bool,
    list_models: bool,
    extra_body: str,
    prices: str,
    models_url: str = "",
    settings: Settings | None = None,
) -> LlmConfig:
    """화면에서 추가한 회사 하나의 정의를 저장한다. 같은 이름이면 고친다.

    **이미 이 회사를 쓰는 기능이 새 정의로 못 쓰게 되면 거절한다.** 분류가 쓰는 회사를 "강제
    없음" 으로 바꾸거나 이미지 읽기가 쓰는 회사의 이미지를 끄면, 다음 호출부터 그 기능이 선다.
    기능에 지정할 때 거는 규칙을 정의를 고칠 때도 같게 건다.
    """
    try:
        trimmed = custom.check_name(name, registry.PROVIDERS)
        definition = custom.from_form(
            label=label,
            base_url=base_url,
            schema_mode=schema_mode,
            temperature=temperature,
            max_tokens=max_tokens,
            images=images,
            list_models=list_models,
            extra_body=extra_body,
            prices=prices,
            models_url=models_url,
        )
    except custom.CustomDefinitionError as exc:
        raise LlmSettingError(str(exc)) from exc

    base = settings or get_settings()
    stored = _stored(conn)
    value = custom.to_json(definition)
    checked = _with_customs(base, {**stored, custom_row(trimmed): value})
    for feature in FEATURES:
        if _feature_provider(feature, stored, base) != trimmed:
            continue
        try:
            registry.resolve(feature, trimmed, stored.get(model_row(feature), ""), checked)
        except LlmCallError as exc:
            raise LlmSettingError(
                f"{FEATURE_LABELS[feature]} 기능이 `{trimmed}` 을 쓰고 있어 "
                f"이렇게 바꿀 수 없다: {exc}"
            ) from exc

    _upsert(conn, {custom_row(trimmed): value})
    return read_config(conn, base)


def delete_custom(
    conn: sqlite3.Connection, name: str, settings: Settings | None = None
) -> LlmConfig:
    """화면에서 추가한 회사 하나를 지운다. 그 회사의 키 행도 같이 지운다 (2026-09-14 결정).

    **쓰는 기능이 있으면 거절한다.** 지우고 나면 그 기능은 `unknown_provider` 로 서는데, 그
    사실은 다음 수집이나 분류가 돌 때에야 드러난다.
    """
    base = settings or get_settings()
    stored = _stored(conn)
    trimmed = name.strip()
    if custom_row(trimmed) not in stored:
        raise LlmSettingError(f"`{trimmed}` 은 화면에서 추가한 회사가 아니다")
    used = [
        FEATURE_LABELS[feature]
        for feature in FEATURES
        if _feature_provider(feature, stored, base) == trimmed
    ]
    if used:
        raise LlmSettingError(
            f"{', '.join(used)} 기능이 `{trimmed}` 을 쓰고 있다. "
            "그 기능의 제공자를 먼저 바꾼 뒤에 지운다"
        )
    conn.execute(
        "DELETE FROM app_settings WHERE key IN (?, ?)", (custom_row(trimmed), key_row(trimmed))
    )
    return read_config(conn, base)


async def check_connection(
    conn: sqlite3.Connection,
    name: str,
    model: str,
    settings: Settings | None = None,
    client: Any | None = None,
) -> ConnectionResult:
    """화면에서 추가한 회사를 실제로 한 번 부른다. 주소·키·모델·답 형식 강제가 되는지 본다.

    호출은 성공도 실패도 `llm_calls` 에 `connection_test` 로 남는다 (2026-09-14 결정).
    """
    base = settings or get_settings()
    stored = _stored(conn)
    trimmed = name.strip()
    if custom_row(trimmed) not in stored:
        raise LlmSettingError(f"`{trimmed}` 은 화면에서 추가한 회사가 아니다")
    entry = _entries(stored).get(trimmed)
    if entry is None:
        raise LlmSettingError(f"`{trimmed}` 의 정의를 읽을 수 없다. 정의를 다시 저장한다")
    model_name = model.strip()
    if not model_name:
        raise LlmSettingError("테스트할 모델 이름을 적는다")
    key = _key_value(entry, stored, base)
    if not key:
        raise LlmSettingError(f"`{trimmed}` 의 API 키가 없다. 키를 먼저 저장한다")

    resolved = _with_customs(base, stored).model_copy(
        update={
            "llm_custom_keys": {**base.llm_custom_keys, trimmed: key},
            "llm_custom_models": {**base.llm_custom_models, trimmed: model_name},
        }
    )
    try:
        text, usage = await entry.call_model(
            client or entry.build_client(resolved),
            model_name,
            CHECK_PROMPT,
            1,
            "연결 테스트",
            response_schema=ConnectionCheck,
            system_instruction=CHECK_INSTRUCTION,
        )
    except Exception as exc:  # SDK 마다 예외가 달라 테스트에서만 넓게 받는다
        reason = exc.reason if isinstance(exc, LlmCallError) else type(exc).__name__
        failed = Usage(
            provider=trimmed,
            model=model_name,
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            latency_ms=0,
        )
        record_call(conn, feature=CONNECTION_TEST, usage=failed, ok=False, error=f"{reason}: {exc}")
        return ConnectionResult(
            ok=False, message=f"`{trimmed}` 의 {model_name} 을 부르지 못했다: {exc}"
        )

    record_call(conn, feature=CONNECTION_TEST, usage=usage)
    try:
        ConnectionCheck.model_validate_json(text)
    except ValidationError:
        return ConnectionResult(
            ok=False,
            message=(
                f"불렀지만 답이 정해진 모양이 아니었다(토큰 {usage.total_tokens:,}개). "
                f"답 형식 강제 방식을 확인한다. 받은 답: {text[:200]}"
            ),
        )
    return ConnectionResult(
        ok=True,
        message=(
            f"`{trimmed}` 의 {model_name} 로 불렀고 정해진 모양으로 답했다. "
            f"토큰 {usage.total_tokens:,}개, {usage.latency_ms:,}ms"
        ),
    )


def settings_for(
    conn: sqlite3.Connection | None, feature: str, settings: Settings | None = None
) -> Settings:
    """이 기능이 쓸 설정 한 벌. 저장된 값이 있으면 그것으로 덮은 사본이다.

    돌려주는 것이 `Settings` 인 이유는 부르는 쪽이 이미 그것을 넘기고 있어서다. 기능마다
    사본이 따로 나오므로 **같은 제공자를 쓰는 두 기능이 다른 모델을 쓸 수 있다.**

    `conn` 이 없으면 환경변수 그대로다. 저장소를 몰래 열지 않는다 — 어느 DB 를 읽었는지
    부르는 쪽이 모르는 채로 설정이 바뀌면 그 호출은 아무도 설명할 수 없다.

    화면에서 추가한 회사의 정의는 기능과 상관없이 늘 실린다. 그 회사를 가리키면 키와 모델도
    설정의 사전에 실린다 — 부르는 쪽은 코드에 든 제공자와 똑같이 `for_feature` 만 부른다.
    """
    base = settings or get_settings()
    if conn is None:
        return base

    stored = _stored(conn)
    name = _feature_provider(feature, stored, base)
    update: dict[str, Any] = {
        registry.FEATURE_SETTING[feature]: name,
        "llm_custom_providers": {**base.llm_custom_providers, **_custom_raw(stored)},
    }

    entry = _entries(stored).get(name)
    if entry is not None:
        # 모르는 이름이면 아무것도 덮지 않는다. 부르는 쪽이 `unknown_provider` 로 선다
        model = stored.get(model_row(feature)) or entry.model_of(base)
        key = _key_value(entry, stored, base)
        if name in registry.PROVIDERS:
            update[entry.model_setting] = model
            update[entry.key_setting] = key
        else:
            update["llm_custom_models"] = {**base.llm_custom_models, name: model}
            update["llm_custom_keys"] = {**base.llm_custom_keys, name: key}
    return base.model_copy(update=update)


async def list_models(
    conn: sqlite3.Connection, provider: str, settings: Settings | None = None
) -> tuple[list[str], str]:
    """그 제공자가 지금 주는 모델 목록과, 못 받았으면 그 사유.

    **목록을 못 받는 것이 저장을 막지 않는다.** 사유만 적고 빈 목록을 돌려준다 — 운영자는
    모델 이름을 손으로 적으면 되고, 목록은 편의다. 모델 ID 를 소스에 적지 않으려고 물어보는
    것이라 (`.claude/rules/llm.md`) 못 받았다고 적어 둔 값으로 대신하지 않는다.
    """
    base = settings or get_settings()
    stored = _stored(conn)
    entry = _entry(_entries(stored), provider)
    if entry.list_models is None:
        return [], f"`{provider}` 는 모델 목록을 주지 않는다. 이름을 손으로 적는다"

    key = _key_value(entry, stored, base)
    if not key:
        return [], f"`{provider}` 의 API 키가 없다. 키를 저장하면 목록을 받아 온다"

    overridden = base.model_copy(update=_key_update(entry, key, base))
    try:
        client = entry.build_client(overridden)
        names = await entry.list_models(client)
    except LlmCallError as exc:
        return [], str(exc)
    except Exception as exc:  # SDK 마다 예외가 달라 여기서만 넓게 받는다
        logger.warning("%s 모델 목록을 받지 못했다: %s", provider, exc)
        return [], f"목록을 받지 못했다: {exc}"
    return sorted(names), ""


def _entries(stored: dict[str, str]) -> dict[str, Provider]:
    """부를 수 있는 제공자 전부. 코드에 든 다섯에 정의를 읽을 수 있는 추가한 회사를 더한다.

    정의를 읽지 못한 회사는 빠진다 — 읽기는 서지 않는다. 그 사유는 `CustomView.problem` 이
    화면에 보여 준다.
    """
    entries = dict(registry.PROVIDERS)
    for name, raw in _custom_raw(stored).items():
        if name in entries:
            continue
        try:
            entries[name] = custom_entry(name, custom.parse(raw))
        except custom.CustomDefinitionError as exc:
            logger.warning("추가한 회사 `%s` 의 정의를 읽지 못했다: %s", name, exc)
    return entries


def _entry(entries: dict[str, Provider], provider: str) -> Provider:
    entry = entries.get(provider)
    if entry is None:
        raise LlmSettingError(
            f"제공자 `{provider}` 를 모른다. 쓸 수 있는 것: {', '.join(sorted(entries))}"
        )
    return entry


def _custom_raw(stored: dict[str, str]) -> dict[str, str]:
    """추가한 회사의 이름 → 정의 JSON."""
    return {
        key[len(CUSTOM_PREFIX) :]: value
        for key, value in stored.items()
        if key.startswith(CUSTOM_PREFIX)
    }


def _with_customs(base: Settings, stored: dict[str, str]) -> Settings:
    """추가한 회사의 정의를 실은 사본. 이름으로 항목을 찾는 `registry.resolve` 에 넘긴다."""
    return base.model_copy(
        update={"llm_custom_providers": {**base.llm_custom_providers, **_custom_raw(stored)}}
    )


def _key_update(entry: Provider, key: str, base: Settings) -> dict[str, Any]:
    """키 하나를 설정 사본에 싣는 값. 코드에 든 제공자는 칸이고 추가한 회사는 사전이다."""
    if entry.name in registry.PROVIDERS:
        return {entry.key_setting: key}
    return {"llm_custom_keys": {**base.llm_custom_keys, entry.name: key}}


def _key_value(entry: Provider, stored: dict[str, str], base: Settings) -> str:
    """이 제공자의 키. 저장된 값이 있으면 그것, 없으면 환경변수."""
    return stored.get(key_row(entry.name)) or entry.key_of(base)


def _feature_provider(feature: str, stored: dict[str, str], base: Settings) -> str:
    """이 기능이 가리키는 제공자 이름. 저장된 값이 있으면 그것, 없으면 환경변수."""
    return stored.get(provider_row(feature)) or str(
        getattr(base, registry.FEATURE_SETTING[feature])
    )


def _key_view(entry: Provider, stored: dict[str, str], base: Settings) -> KeyView:
    value = _key_value(entry, stored, base)
    return KeyView(
        provider=entry.name,
        present=bool(value),
        tail=mask(value),
        stored=bool(stored.get(key_row(entry.name))),
    )


def _feature_view(
    feature: str, entries: dict[str, Provider], stored: dict[str, str], base: Settings
) -> FeatureView:
    name = _feature_provider(feature, stored, base)
    entry = entries.get(name)
    if entry is None:
        problem = (
            f"`{name}` 의 정의를 읽을 수 없다. 정의를 다시 저장한다"
            if custom_row(name) in stored
            else f"제공자 `{name}` 를 모른다"
        )
        return FeatureView(
            feature=feature,
            label=FEATURE_LABELS[feature],
            provider=name,
            model="",
            stored=bool(stored.get(provider_row(feature))),
            problem=problem,
        )

    model = stored.get(model_row(feature)) or entry.model_of(base)
    problem = ""
    if not _key_value(entry, stored, base):
        problem = f"`{name}` 의 API 키가 없다"
    elif not model:
        problem = f"`{name}` 의 모델이 비어 있다. 모델 이름을 적고 저장한다"
    else:
        try:
            registry.resolve(feature, name, model, _with_customs(base, stored))
        except LlmCallError as exc:
            problem = str(exc)
    return FeatureView(
        feature=feature,
        label=FEATURE_LABELS[feature],
        provider=name,
        model=model,
        stored=bool(stored.get(provider_row(feature))),
        problem=problem,
    )


def _custom_views(stored: dict[str, str], base: Settings) -> tuple[CustomView, ...]:
    views: list[CustomView] = []
    for name, raw in sorted(_custom_raw(stored).items()):
        using = [
            feature for feature in FEATURES if _feature_provider(feature, stored, base) == name
        ]
        used_by = tuple(FEATURE_LABELS[feature] for feature in using)
        try:
            definition = custom.parse(raw)
        except custom.CustomDefinitionError as exc:
            views.append(CustomView(name=name, used_by=used_by, problem=str(exc)))
            continue
        models = [stored.get(model_row(feature), "") for feature in using]
        test_model = next((model for model in models if model), "") or next(
            iter(definition.prices), ""
        )
        views.append(
            CustomView(
                name=name,
                label=definition.label,
                base_url=definition.base_url,
                schema_mode=definition.schema_mode,
                temperature="" if definition.temperature is None else f"{definition.temperature:g}",
                max_tokens="" if definition.max_tokens is None else str(definition.max_tokens),
                images=definition.images,
                list_models=definition.list_models,
                models_url=definition.models_url,
                extra_body=custom.pretty(definition.extra_body),
                prices=custom.pretty(
                    {model: price.model_dump() for model, price in definition.prices.items()}
                ),
                used_by=used_by,
                test_model=test_model,
            )
        )
    return tuple(views)


def _stored(conn: sqlite3.Connection) -> dict[str, str]:
    """저장된 행. 읽지 못하면 빈 채로 돌려준다 — 읽기가 호출을 멈추지 않는다."""
    try:
        rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    except sqlite3.Error as exc:
        logger.warning("제공자 설정을 읽지 못했다: %s. 환경변수 값을 쓴다", exc)
        return {}
    return {
        str(row["key"]): str(row["value"]).strip() for row in rows if is_llm_row(str(row["key"]))
    }


def _upsert(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    conn.executemany(
        """
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')
        """,
        list(values.items()),
    )
