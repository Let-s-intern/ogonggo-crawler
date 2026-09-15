"""오공고(Spring) 전송 설정. 값은 `app_settings` 표에 들어간다 — 새 표를 만들지 않는다.

`app/notify/settings.py` 와 같은 자리의 모듈이다. 읽기는 예외를 던지지 않는다. 손으로 넣은 깨진 값
하나가 화면을 죽이면 안 된다. 읽지 못한 값은 기본값으로 떨어지고 로그에 남는다. 쓰기는 반대로
깐깐하다 — 저장되는 값은 전부 검증을 지난다.

**키는 여기 두지 않는다** (2026-09-15 결정). 오공고 내부 API 키는 크롤러 `.env` 의
`OGONGGO_INTERNAL_API_KEY` 에서만 읽는다 (`app/config.py`). DB 에 두면 내보내기 파일과 화면으로
새어 나간다. 0036 전에 쓰던 `deliver_method`·`deliver_auth_header` 행은 더 읽지 않는다.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

logger = logging.getLogger(__name__)

URL = "deliver_url"
ENABLED = "deliver_enabled"
BATCH_SIZE = "deliver_batch_size"

KEYS: tuple[str, ...] = (URL, ENABLED, BATCH_SIZE)

DEFAULT_BATCH_SIZE = 100


class DeliverSettingError(ValueError):
    """저장할 수 없는 값. 거절 사유를 화면이 그대로 옮긴다."""


@dataclass(frozen=True)
class DeliverConfig:
    """전송 설정 한 벌. 저장된 값이 없으면 이 기본값이 그대로 쓰인다.

    `enabled` 는 분류가 끝날 때 자동으로 보낼지다. 전달 화면의 `지금 보내기` 는 이 값과 상관없이
    보낸다 (`app/deliver/spring.py`).
    """

    # 오공고 관리자 API 의 주소. 뒤에 `/api/v1/internal/jobs` 를 붙여 부른다
    url: str = ""
    enabled: bool = False
    batch_size: int = DEFAULT_BATCH_SIZE

    @property
    def configured(self) -> bool:
        return bool(self.url.strip())

    def validate(self) -> None:
        """저장하기 전에 본다. 하나라도 걸리면 아무것도 저장하지 않는다."""
        cleaned = self.url.strip()
        if cleaned and not cleaned.startswith(("http://", "https://")):
            raise DeliverSettingError(
                f"오공고 주소는 http:// 나 https:// 로 시작해야 한다: {self.url!r}"
            )
        if self.enabled and not cleaned:
            raise DeliverSettingError("주소가 비어 있으면 켤 수 없다")
        if self.batch_size < 1:
            raise DeliverSettingError(f"한 번에 보내는 건수는 1 이상이어야 한다: {self.batch_size}")


def read_config(conn: sqlite3.Connection) -> DeliverConfig:
    """저장된 설정. 값이 없거나 읽지 못하면 기본값이다. 읽는 김에 채워 넣지 않는다."""
    stored = {
        str(row["key"]): str(row["value"])
        for row in conn.execute(
            f"SELECT key, value FROM app_settings WHERE key IN ({','.join('?' * len(KEYS))})",
            KEYS,
        )
    }
    default = DeliverConfig()
    return DeliverConfig(
        url=stored.get(URL, default.url),
        enabled=stored.get(ENABLED, "0") == "1",
        batch_size=_as_int(stored.get(BATCH_SIZE), default.batch_size),
    )


def write_config(conn: sqlite3.Connection, config: DeliverConfig) -> DeliverConfig:
    """설정 한 벌을 저장한다. 거절된 값은 하나도 저장되지 않는다."""
    config.validate()
    values = {
        URL: config.url.strip(),
        ENABLED: "1" if config.enabled else "0",
        BATCH_SIZE: str(config.batch_size),
    }
    conn.executemany(
        """
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')
        """,
        list(values.items()),
    )
    return read_config(conn)


def _as_int(raw: str | None, fallback: int) -> int:
    if raw is None:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s 에 저장된 값을 정수로 읽을 수 없다: %r. 기본값을 쓴다", BATCH_SIZE, raw)
        return fallback
    if value < 1:
        logger.warning("%s 에 저장된 값이 1 미만이다: %r. 기본값을 쓴다", BATCH_SIZE, raw)
        return fallback
    return value
