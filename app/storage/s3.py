"""S3 호환 저장소에 로고를 올린다. MinIO 도 실제 S3 도 이 코드 하나로 부른다.

주소 형식을 우리가 만들지 않는다. `endpoint_url` 이 있으면 `엔드포인트/버킷/키` 로,
비어 있으면 SDK 가 지역으로 `버킷.s3.지역.amazonaws.com/키` 를 만든다. 운영자가 고를 값이
아니다 (`.claude/tasks/todo/prd-fields-and-logo.md` 5장).

공용 fetch 클라이언트를 지나지 않는 두 번째 자리다. 우리가 올린 객체만 만지므로 robots 를
물을 상대가 아니고 지킬 딜레이도 없다 (`.claude/rules/crawling.md`, 2026-08-28).

받는 것은 이미지뿐이고 크기 상한이 있다. 어느 형식인지는 파일 이름이 아니라 내용으로
정한다 — `.png` 로 이름만 바꾼 실행 파일이 우리 도메인에서 서비스되게 두지 않는다.

## 받는 형식 (2026-09-28 결정)

예전에는 PNG·JPEG·WebP 셋만 받았다. 운영자가 가진 로고 파일 형식이 제각각이라 웬만한 이미지는
다 받는다. 브라우저가 그대로 그리는 형식(PNG·JPEG·WebP·GIF·AVIF·SVG)은 그대로 올리고, 그 밖에
Pillow 가 여는 형식(BMP·TIFF·ICO 등)은 PNG 로 바꿔 올린다 — 오공고 화면이 그리지 못하는 파일을
대표 이미지로 보내지 않는다.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image, UnidentifiedImageError

from app.storage.settings import StorageConfig

logger = logging.getLogger(__name__)

# 그대로 올리는 형식. 앞 바이트로 판정한다
PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff"
RIFF = b"RIFF"
WEBP = b"WEBP"
GIF = (b"GIF87a", b"GIF89a")
# AVIF 는 ISO 미디어 상자다. 4바이트 크기 뒤에 `ftyp` 와 브랜드가 온다
FTYP = b"ftyp"
AVIF_BRANDS = (b"avif", b"avis")

# SVG 는 텍스트라 앞 바이트가 아니라 여는 태그로 본다. 스크립트를 품은 SVG 가 공개 주소에서
# 열리면 그것이 곧 XSS 라, 스크립트·이벤트 속성·외부 문서를 품은 것은 받지 않는다
_SVG_OPEN = re.compile(
    rb"^\s*(<\?xml[^>]*>\s*)?(<!--.*?-->\s*)*(<!DOCTYPE[^>]*>\s*)?<svg[\s>]", re.S | re.I
)
_SVG_ACTIVE = re.compile(
    rb"<\s*(script|foreignObject|iframe|embed|object)\b|\son[a-z]+\s*=|javascript:", re.I
)

# 로고는 200px 안팎으로 그린다. 5MiB 는 디자인 도구에서 생각 없이 내보낸 파일이나 무압축
# BMP·TIFF 도 지나가게 두면서, 사진을 잘못 고른 것은 막는다
MAX_IMAGE_BYTES = 5 * 1024 * 1024

# 화면에 적는 문구. 형식과 상한을 두 곳에서 따로 쓰지 않는다
ACCEPTED = "PNG, JPEG, WebP, GIF, AVIF, SVG, BMP, TIFF, ICO 등 대부분의 이미지"
MAX_IMAGE_LABEL = "5MB"
# 파일 고르기 창이 보여 주는 형식. 내용 검사는 서버가 따로 한다
ACCEPT_ATTR = "image/*,.svg,.ico,.bmp,.tif,.tiff"

# 저장소가 답하지 않을 때 오래 매달리지 않는다. 운영자가 화면 앞에서 기다리는 동작이다
_CONNECT_TIMEOUT = 5
_READ_TIMEOUT = 15


class StorageError(RuntimeError):
    """저장소 동작 실패. 사유를 낱말 하나와 문장 하나로 갖는다.

    고치는 방법이 사유마다 다르다 — 키가 틀린 것과 버킷이 없는 것과 주소에 못 닿는 것은
    같은 화면에 같은 문구로 나오면 안 된다.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class ImageKind:
    """올릴 이미지의 형식. 파일 이름이 아니라 내용에서 나온다."""

    extension: str
    content_type: str


PNG_KIND = ImageKind("png", "image/png")
# 앞 바이트로 모르는 형식일 때의 문장. `prepare_image` 가 이 문장일 때만 Pillow 로 열어 본다
NOT_ACCEPTED = f"받는 형식이 아니다. {ACCEPTED} 를 올릴 수 있다"


def _check_size(data: bytes) -> None:
    if not data:
        raise StorageError("not_an_image", "파일이 비어 있다")
    if len(data) > MAX_IMAGE_BYTES:
        raise StorageError(
            "too_large",
            f"파일이 상한 {MAX_IMAGE_LABEL} 를 넘는다: {len(data)}바이트",
        )


def detect_image(data: bytes) -> ImageKind:
    """브라우저가 그대로 그리는 형식이면 그 형식이다. 아니면 거절한다 (변환은 `prepare_image`)."""
    _check_size(data)
    if data.startswith(PNG):
        return PNG_KIND
    if data.startswith(JPEG):
        return ImageKind("jpg", "image/jpeg")
    if data[:4] == RIFF and data[8:12] == WEBP:
        return ImageKind("webp", "image/webp")
    if data.startswith(GIF):
        return ImageKind("gif", "image/gif")
    if data[4:8] == FTYP and data[8:12] in AVIF_BRANDS:
        return ImageKind("avif", "image/avif")
    head = data.lstrip(b"\xef\xbb\xbf")
    if _SVG_OPEN.match(head[:4096]):
        if _SVG_ACTIVE.search(data):
            raise StorageError(
                "not_an_image",
                "스크립트나 이벤트 속성이 든 SVG 는 받지 않는다. PNG 로 내보내 올린다",
            )
        return ImageKind("svg", "image/svg+xml")
    raise StorageError("not_an_image", NOT_ACCEPTED)


def prepare_image(data: bytes) -> tuple[bytes, ImageKind]:
    """올릴 바이트와 형식. 브라우저가 못 그리는 형식은 Pillow 로 열어 PNG 로 바꾼다."""
    try:
        return data, detect_image(data)
    except StorageError as exc:
        if exc.message != NOT_ACCEPTED:
            raise
        refused = exc
    try:
        with Image.open(io.BytesIO(data)) as opened:
            opened.load()
            # 팔레트·CMYK 등은 PNG 가 받는 모드로 편다. 투명도는 남긴다
            image = opened if opened.mode in ("RGB", "RGBA", "L", "LA") else opened.convert("RGBA")
            out = io.BytesIO()
            image.save(out, format="PNG", optimize=True)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise refused from exc
    converted = out.getvalue()
    _check_size(converted)
    return converted, PNG_KIND


def client(config: StorageConfig) -> Any:
    """설정 한 벌로 만든 S3 클라이언트.

    `endpoint_url` 이 비면 넘기지 않는다. 빈 문자열을 넘기면 SDK 가 지역으로 주소를 만드는
    길로 가지 않고 그 자리에서 깨진다.
    """
    return boto3.client(
        "s3",
        endpoint_url=config.endpoint or None,
        region_name=config.region,
        aws_access_key_id=config.access_key,
        aws_secret_access_key=config.secret_key,
        config=BotoConfig(
            connect_timeout=_CONNECT_TIMEOUT,
            read_timeout=_READ_TIMEOUT,
            # 여기서 실패하는 것은 대개 설정이 틀린 것이다. 세 번 더 물어도 답이 같다
            retries={"max_attempts": 1},
        ),
    )


def upload_image(config: StorageConfig, *, data: bytes, name: str) -> str:
    """이미지 하나를 올리고 공개 주소를 돌려준다.

    `name` 은 확장자 없는 객체 이름이다. 확장자는 내용에서 정한 것을 붙인다 — 운영자가 적어
    온 이름을 믿으면 `image/png` 로 서비스되는 JPEG 가 생긴다.
    """
    if not config.configured:
        raise StorageError("not_configured", "저장소 설정이 아직 채워지지 않았다")
    data, kind = prepare_image(data)
    key = f"{name}.{kind.extension}"
    try:
        client(config).put_object(
            Bucket=config.bucket,
            Key=key,
            Body=data,
            ContentType=kind.content_type,
        )
    except (ClientError, BotoCoreError) as exc:
        raise translate(exc, config) from exc
    logger.info("로고를 올렸다: %s/%s (%d bytes)", config.bucket, key, len(data))
    return config.public_url(key)


@dataclass(frozen=True)
class CheckResult:
    """연결 확인 한 번의 결과. 실패했으면 어느 걸음에서인지가 같이 온다."""

    ok: bool
    step: str
    reason: str
    message: str


# 확인이 만드는 객체. 로고와 섞이지 않게 접두어를 둔다. 지우기까지 성공하면 남지 않는다
CHECK_PREFIX = "_check/"
CHECK_BODY = b"job-crawler storage check"


def check(config: StorageConfig) -> CheckResult:
    """작은 객체를 넣고, 읽고, 지운다. 저장된 설정으로 부른다.

    걸음을 나눠 부르는 이유는 사유를 가르기 위해서다. 넣기에서 죽은 것과 읽기에서 죽은 것은
    같은 `AccessDenied` 라도 고치는 자리가 다르다 — 앞은 쓰기 권한, 뒤는 읽기 권한이다.

    던지지 않는다. 이 함수를 부르는 자리가 화면이고, 화면은 실패도 그려야 한다.
    """
    if not config.configured:
        return CheckResult(
            ok=False,
            step="설정",
            reason="not_configured",
            message="버킷과 키를 먼저 저장한다. 확인은 저장된 값으로 한다",
        )

    key = f"{CHECK_PREFIX}{uuid4().hex}.txt"
    step = "연결"
    try:
        s3 = client(config)
        step = "넣기"
        s3.put_object(Bucket=config.bucket, Key=key, Body=CHECK_BODY, ContentType="text/plain")
        step = "읽기"
        body = s3.get_object(Bucket=config.bucket, Key=key)["Body"].read()
        if body != CHECK_BODY:
            return CheckResult(
                ok=False,
                step=step,
                reason="mismatch",
                message="넣은 것과 읽은 것이 다르다. 같은 이름의 다른 객체를 읽고 있다",
            )
        step = "지우기"
        s3.delete_object(Bucket=config.bucket, Key=key)
    except (ClientError, BotoCoreError) as exc:
        error = translate(exc, config)
        logger.info("저장소 연결 확인 실패: %s 에서 %s", step, error.reason)
        return CheckResult(ok=False, step=step, reason=error.reason, message=error.message)
    except Exception as exc:  # noqa: BLE001 - 화면이 부르는 자리다. 무엇이 와도 문장으로 답한다
        logger.warning("저장소 연결 확인이 예상 밖으로 실패했다: %s", exc)
        return CheckResult(
            ok=False,
            step=step,
            reason="failed",
            message=f"{type(exc).__name__}: {exc}",
        )

    where = config.endpoint or f"{config.region} 지역의 S3"
    return CheckResult(
        ok=True,
        step="지우기",
        reason="ok",
        message=f"버킷 `{config.bucket}` 에 넣고 읽고 지웠다 ({where})",
    )


def translate(exc: Exception, config: StorageConfig) -> StorageError:
    """SDK 예외를 고칠 방법이 갈리는 사유로 옮긴다.

    낱말은 다섯이다. 주소에 못 닿는 것(`unreachable`), 키가 틀린 것(`bad_credentials`),
    버킷이 없는 것(`no_bucket`), 키는 맞는데 권한이 없는 것(`denied`), 나머지(`failed`).
    """
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in ("InvalidAccessKeyId", "SignatureDoesNotMatch", "InvalidToken"):
            return StorageError(
                "bad_credentials",
                f"접근 키나 비밀 키가 저장소에서 거절됐다 ({code})",
            )
        if code in ("NoSuchBucket", "404", "NotFound"):
            return StorageError(
                "no_bucket",
                f"버킷 `{config.bucket}` 이 저장소에 없다. 콘솔에서 먼저 만든다",
            )
        if code in ("AccessDenied", "403", "Forbidden"):
            return StorageError(
                "denied",
                f"버킷 `{config.bucket}` 에 대한 권한이 없다. 키는 닿았고 권한이 막혔다",
            )
        return StorageError("failed", f"저장소가 거절했다 ({code}): {exc}")

    name = type(exc).__name__
    if "Connect" in name or "Endpoint" in name or "ConnectionError" in name:
        target = config.endpoint or f"{config.region} 지역의 S3"
        return StorageError("unreachable", f"저장소 주소에 닿지 못했다: {target}")
    return StorageError("failed", f"저장소 호출이 실패했다 ({name}): {exc}")
