"""저장소 설정. 값은 `app_settings` 표에 들어간다 — 새 표를 만들지 않는다.

`app/notify/settings.py` 와 같은 방식이다. 저장소를 따로 두는 이유도 같다 — `/api/settings`
가 내보내는 `dict[str, int]` 에 주소와 키를 섞으면 이미 있는 화면이 같이 흔들린다.

## 환경변수 (2026-09-28 결정)

처음에는 환경변수 짝을 두지 않았다. 운영에서 키를 DB 에 두면 DB 내보내기에 키가 같이 나가고, DB 를
새로 만들면 다시 넣어야 해서 `S3_*` 환경변수를 읽는다 (`app/config.py`). AI 키와 같은 순서다 —
화면에서 저장한 값이 있으면 그 값이고, 없으면 환경변수다. 환경변수에 버킷이 있으면 실제 S3 로 보고
엔드포인트를 비우고, 공개 주소가 없으면 버킷 주소(`https://버킷.s3.지역.amazonaws.com`)로 만든다.

여섯이 한 벌이다. 엔드포인트만 비울 수 있고, 그때는 SDK 가 지역으로 주소를 만든다 (실제 S3).
주소 형식을 운영자가 고르게 하지 않는다 (`.claude/tasks/todo/prd-fields-and-logo.md` 5장).

읽기는 예외를 던지지 않고 쓰기는 깐깐하다. 손으로 넣은 깨진 값 하나가 화면을 못 열게 두지
않는다. 다만 못 읽은 값은 기본값으로 떨어지므로, 저장을 지나지 않은 값은 저장소에 닿지
못하고 연결 확인이 그 사유를 댄다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.config import Settings, get_settings

ENDPOINT = "s3_endpoint"
REGION = "s3_region"
BUCKET = "s3_bucket"
ACCESS_KEY = "s3_access_key"
SECRET_KEY = "s3_secret_key"
PUBLIC_BASE = "s3_public_base"

KEYS: tuple[str, ...] = (ENDPOINT, REGION, BUCKET, ACCESS_KEY, SECRET_KEY, PUBLIC_BASE)

# 비밀로 다루는 키. 화면은 끝 네 자리만 그리고 내보내기 경고가 이름을 댄다
SECRET_KEYS: tuple[str, ...] = (ACCESS_KEY, SECRET_KEY)

# docker-compose.yml 의 서비스 이름이다. 컨테이너 안에서 부르는 주소라 localhost 가 아니다
DEFAULT_ENDPOINT = "http://minio:9000"
# MinIO 는 지역을 무시한다. 실제 S3 로 옮길 때 바꾼다
DEFAULT_REGION = "us-east-1"
# 브라우저가 이미지를 읽는 주소. 서버가 부르는 엔드포인트와 다르다
DEFAULT_PUBLIC_BASE = "http://localhost:9000"

# 화면에 그릴 때 남기는 끝자리 수. `app/llm/settings.py` 와 같다
TAIL = 4


class StorageSettingError(ValueError):
    """저장할 수 없는 값. 거절 사유를 화면이 그대로 옮긴다."""


@dataclass(frozen=True)
class StorageConfig:
    """저장소 설정 한 벌. 저장된 값이 없으면 이 기본값이 그대로 쓰인다."""

    # 서버가 부르는 주소. 비어 있으면 SDK 가 지역으로 만든다 (실제 S3)
    endpoint: str = DEFAULT_ENDPOINT
    region: str = DEFAULT_REGION
    bucket: str = ""
    access_key: str = ""
    secret_key: str = ""
    public_base: str = DEFAULT_PUBLIC_BASE

    @property
    def configured(self) -> bool:
        """올릴 수 있는 상태인지. 아직 저장한 적이 없으면 거짓이다."""
        return bool(self.bucket and self.access_key and self.secret_key)

    def public_url(self, key: str) -> str:
        """올린 객체를 브라우저가 여는 주소."""
        return f"{self.public_base.rstrip('/')}/{key.lstrip('/')}"

    def validate(self) -> None:
        """저장하기 전에 본다. 하나라도 걸리면 아무것도 저장하지 않는다."""
        # 엔드포인트만 비울 수 있다. 비면 SDK 가 지역으로 주소를 만든다
        if self.endpoint and not self.endpoint.startswith(("http://", "https://")):
            raise StorageSettingError(
                f"엔드포인트는 http:// 나 https:// 로 시작해야 한다: {self.endpoint!r}"
            )
        if not self.public_base.startswith(("http://", "https://")):
            raise StorageSettingError(
                f"공개 주소는 http:// 나 https:// 로 시작해야 한다: {self.public_base!r}"
            )
        if not self.bucket:
            raise StorageSettingError("버킷 이름이 비어 있다")
        if not self.region:
            raise StorageSettingError("지역이 비어 있다. MinIO 라면 us-east-1 로 둔다")
        # 키가 없으면 SDK 가 익명으로 부르고, 실패 사유가 권한 오류로 뭉개진다.
        # 저장 단계에서 거절하는 편이 무엇을 채워야 하는지 분명하다
        if not self.access_key:
            raise StorageSettingError("접근 키가 비어 있다")
        if not self.secret_key:
            raise StorageSettingError("비밀 키가 비어 있다")


def env_config(settings: Settings | None = None) -> StorageConfig | None:
    """환경변수의 설정. 버킷이 없으면 None 이다 — 그때는 로컬 MinIO 기본값을 쓴다."""
    base = settings or get_settings()
    bucket = base.s3_bucket.strip()
    if not bucket:
        return None
    region = base.s3_region.strip() or DEFAULT_REGION
    public_base = base.s3_public_base.strip() or f"https://{bucket}.s3.{region}.amazonaws.com"
    return StorageConfig(
        endpoint=base.s3_endpoint.strip(),
        region=region,
        bucket=bucket,
        access_key=base.s3_access_key.strip(),
        secret_key=base.s3_secret_key.strip(),
        public_base=public_base,
    )


def stored_keys(conn: sqlite3.Connection) -> dict[str, str]:
    """화면에서 저장한 값. 저장한 적이 없으면 빈 사전이다."""
    return {
        str(row["key"]): str(row["value"])
        for row in conn.execute(
            f"SELECT key, value FROM app_settings WHERE key IN ({','.join('?' * len(KEYS))})",
            KEYS,
        )
    }


def read_config(conn: sqlite3.Connection, settings: Settings | None = None) -> StorageConfig:
    """쓸 설정. 화면에서 저장한 값, 없으면 환경변수, 그것도 없으면 기본값이다.

    읽는 김에 채워 넣지 않는다.
    """
    stored = stored_keys(conn)
    default = env_config(settings) or StorageConfig()
    return StorageConfig(
        endpoint=stored.get(ENDPOINT, default.endpoint),
        region=stored.get(REGION, default.region),
        bucket=stored.get(BUCKET, default.bucket),
        access_key=stored.get(ACCESS_KEY, default.access_key),
        secret_key=stored.get(SECRET_KEY, default.secret_key),
        public_base=stored.get(PUBLIC_BASE, default.public_base),
    )


def write_config(conn: sqlite3.Connection, config: StorageConfig) -> StorageConfig:
    """설정 한 벌을 저장한다. 거절된 값은 하나도 저장되지 않는다.

    앞뒤 공백을 떼고 나서 본다. 붙여넣은 키 끝의 개행 하나가 인증 실패로 나오면 화면에는
    맞는 값으로 보인다.
    """
    cleaned = StorageConfig(
        endpoint=config.endpoint.strip(),
        region=config.region.strip(),
        bucket=config.bucket.strip(),
        access_key=config.access_key.strip(),
        secret_key=config.secret_key.strip(),
        public_base=config.public_base.strip(),
    )
    cleaned.validate()
    values = {
        ENDPOINT: cleaned.endpoint,
        REGION: cleaned.region,
        BUCKET: cleaned.bucket,
        ACCESS_KEY: cleaned.access_key,
        SECRET_KEY: cleaned.secret_key,
        PUBLIC_BASE: cleaned.public_base,
    }
    conn.executemany(
        """
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')
        """,
        list(values.items()),
    )
    return read_config(conn)


def mask(value: str) -> str:
    """화면에 내보낼 끝 네 자리. 네 자리 이하는 전부 가린다 (`app/llm/settings.py`)."""
    trimmed = value.strip()
    if len(trimmed) <= TAIL:
        return ""
    return trimmed[-TAIL:]
