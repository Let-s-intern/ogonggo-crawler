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
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from app.config import Settings, get_settings
from app.llm import providers as registry
from app.llm.base import LlmCallError, Provider
from app.llm.log import FEATURES

logger = logging.getLogger(__name__)

# `app_settings` 의 행 이름. 접두사로 갈라 두면 스냅샷에서 이 설정만 골라내기도 쉽다
KEY_PREFIX = "llm_key_"
PROVIDER_PREFIX = "llm_provider_"
MODEL_PREFIX = "llm_model_"

# 화면에 보이는 자릿수. 이보다 짧거나 같은 값은 전부 가린다 — 네 자리 키의 끝 네 자리는
# 그 키 전체다 (`.claude/tasks/todo/prd-llm-providers.md` 4번)
TAIL = 4

# 기능을 사람이 읽는 이름으로. 화면이 이 낱말을 그대로 쓴다
FEATURE_LABELS: dict[str, str] = {
    "selector_generate": "셀렉터 생성",
    "selector_repair": "AI 수정",
    "classify": "본문 분류",
    "image_read": "이미지 읽기",
}


class LlmSettingError(ValueError):
    """저장할 수 없는 값. 거절 사유를 화면이 그대로 옮긴다."""


def key_row(provider: str) -> str:
    return f"{KEY_PREFIX}{provider}"


def provider_row(feature: str) -> str:
    return f"{PROVIDER_PREFIX}{feature}"


def model_row(feature: str) -> str:
    return f"{MODEL_PREFIX}{feature}"


# 이 저장소가 쓰는 행 전부. 스냅샷을 옮길 때 무엇을 옮기는지도 이 목록이 정한다
ROWS: tuple[str, ...] = (
    *(key_row(name) for name in sorted(registry.PROVIDERS)),
    *(provider_row(feature) for feature in FEATURES),
    *(model_row(feature) for feature in FEATURES),
)


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
class LlmConfig:
    """화면이 그리는 설정 한 벌. 키 전체 값은 어디에도 없다."""

    keys: tuple[KeyView, ...]
    features: tuple[FeatureView, ...]

    def key(self, provider: str) -> KeyView:
        for view in self.keys:
            if view.provider == provider:
                return view
        raise LlmSettingError(f"제공자 `{provider}` 를 모른다")


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
    return LlmConfig(
        keys=tuple(_key_view(name, stored, base) for name in sorted(registry.PROVIDERS)),
        features=tuple(_feature_view(feature, stored, base) for feature in FEATURES),
    )


def write_key(
    conn: sqlite3.Connection, provider: str, value: str, settings: Settings | None = None
) -> LlmConfig:
    """제공자 하나의 키를 저장한다. 빈 값을 주면 지우고 환경변수로 돌아간다."""
    _entry(provider)
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
    베껴 넣어 두면 배포가 모델을 올려도 이 기능만 옛 모델에 남는다.
    """
    if feature not in FEATURES:
        raise LlmSettingError(f"모르는 기능이다: {feature}")
    entry = _entry(provider)
    base = settings or get_settings()
    stored = _stored(conn)

    if not _key_value(entry, stored, base):
        raise LlmSettingError(
            f"`{provider}` 의 API 키가 없다. 키를 먼저 저장한 뒤에 기능에 지정한다"
        )

    trimmed = model.strip()
    resolved_model = trimmed or str(getattr(base, entry.model_setting))
    try:
        registry.resolve(feature, provider, resolved_model)
    except LlmCallError as exc:
        raise LlmSettingError(str(exc)) from exc

    _upsert(conn, {provider_row(feature): provider})
    if trimmed:
        _upsert(conn, {model_row(feature): trimmed})
    else:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (model_row(feature),))
    return read_config(conn, base)


def settings_for(
    conn: sqlite3.Connection | None, feature: str, settings: Settings | None = None
) -> Settings:
    """이 기능이 쓸 설정 한 벌. 저장된 값이 있으면 그것으로 덮은 사본이다.

    돌려주는 것이 `Settings` 인 이유는 부르는 쪽이 이미 그것을 넘기고 있어서다. 기능마다
    사본이 따로 나오므로 **같은 제공자를 쓰는 두 기능이 다른 모델을 쓸 수 있다.**

    `conn` 이 없으면 환경변수 그대로다. 저장소를 몰래 열지 않는다 — 어느 DB 를 읽었는지
    부르는 쪽이 모르는 채로 설정이 바뀌면 그 호출은 아무도 설명할 수 없다.
    """
    base = settings or get_settings()
    if conn is None:
        return base

    stored = _stored(conn)
    name = stored.get(provider_row(feature)) or str(
        getattr(base, registry.FEATURE_SETTING[feature])
    )
    update: dict[str, str] = {registry.FEATURE_SETTING[feature]: name}

    entry = registry.PROVIDERS.get(name)
    if entry is not None:
        # 모르는 이름이면 아무것도 덮지 않는다. 부르는 쪽이 `unknown_provider` 로 선다
        update[entry.model_setting] = stored.get(model_row(feature)) or str(
            getattr(base, entry.model_setting)
        )
        update[entry.key_setting] = _key_value(entry, stored, base)
    return base.model_copy(update=update)


async def list_models(
    conn: sqlite3.Connection, provider: str, settings: Settings | None = None
) -> tuple[list[str], str]:
    """그 제공자가 지금 주는 모델 목록과, 못 받았으면 그 사유.

    **목록을 못 받는 것이 저장을 막지 않는다.** 사유만 적고 빈 목록을 돌려준다 — 운영자는
    모델 이름을 손으로 적으면 되고, 목록은 편의다. 모델 ID 를 소스에 적지 않으려고 물어보는
    것이라 (`.claude/rules/llm.md`) 못 받았다고 적어 둔 값으로 대신하지 않는다.
    """
    entry = _entry(provider)
    if entry.list_models is None:
        return [], f"`{provider}` 는 모델 목록을 주지 않는다. 이름을 손으로 적는다"

    base = settings or get_settings()
    stored = _stored(conn)
    if not _key_value(entry, stored, base):
        return [], f"`{provider}` 의 API 키가 없다. 키를 저장하면 목록을 받아 온다"

    overridden = base.model_copy(update={entry.key_setting: _key_value(entry, stored, base)})
    try:
        client = entry.build_client(overridden)
        names = await entry.list_models(client)
    except LlmCallError as exc:
        return [], str(exc)
    except Exception as exc:  # SDK 마다 예외가 달라 여기서만 넓게 받는다
        logger.warning("%s 모델 목록을 받지 못했다: %s", provider, exc)
        return [], f"목록을 받지 못했다: {exc}"
    return sorted(names), ""


def _entry(provider: str) -> Provider:
    entry = registry.PROVIDERS.get(provider)
    if entry is None:
        raise LlmSettingError(
            f"제공자 `{provider}` 를 모른다. 쓸 수 있는 것: {', '.join(sorted(registry.PROVIDERS))}"
        )
    return entry


def _key_value(entry: Provider, stored: dict[str, str], base: Settings) -> str:
    """이 제공자의 키. 저장된 값이 있으면 그것, 없으면 환경변수."""
    return stored.get(key_row(entry.name)) or str(getattr(base, entry.key_setting))


def _key_view(provider: str, stored: dict[str, str], base: Settings) -> KeyView:
    entry = registry.PROVIDERS[provider]
    value = _key_value(entry, stored, base)
    return KeyView(
        provider=provider,
        present=bool(value),
        tail=mask(value),
        stored=bool(stored.get(key_row(provider))),
    )


def _feature_view(feature: str, stored: dict[str, str], base: Settings) -> FeatureView:
    name = stored.get(provider_row(feature)) or str(
        getattr(base, registry.FEATURE_SETTING[feature])
    )
    entry = registry.PROVIDERS.get(name)
    if entry is None:
        return FeatureView(
            feature=feature,
            label=FEATURE_LABELS[feature],
            provider=name,
            model="",
            stored=bool(stored.get(provider_row(feature))),
            problem=f"제공자 `{name}` 를 모른다",
        )

    model = stored.get(model_row(feature)) or str(getattr(base, entry.model_setting))
    problem = ""
    if not _key_value(entry, stored, base):
        problem = f"`{name}` 의 API 키가 없다"
    else:
        try:
            registry.resolve(feature, name, model)
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


def _stored(conn: sqlite3.Connection) -> dict[str, str]:
    """저장된 행. 읽지 못하면 빈 채로 돌려준다 — 읽기가 호출을 멈추지 않는다."""
    try:
        rows = conn.execute(
            f"SELECT key, value FROM app_settings WHERE key IN ({','.join('?' * len(ROWS))})",
            ROWS,
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("제공자 설정을 읽지 못했다: %s. 환경변수 값을 쓴다", exc)
        return {}
    return {str(row["key"]): str(row["value"]).strip() for row in rows}


def _upsert(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    conn.executemany(
        """
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')
        """,
        list(values.items()),
    )
