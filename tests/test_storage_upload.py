"""업로드 클라이언트 테스트 (5.4.V 의 픽스처 몫).

실제 왕복은 로컬 MinIO 로 확인한다. 여기서 보는 것은 그물이다 — 이미지가 아닌 것과 큰 것이
저장소에 닿기 전에 걸리는지, 확장자가 파일 이름이 아니라 내용에서 나오는지, 그리고 SDK
예외가 고치는 방법이 갈리는 사유로 번역되는지.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from app.storage import s3
from app.storage.settings import StorageConfig

PNG_BYTES = s3.PNG + b"\x00" * 32
JPEG_BYTES = s3.JPEG + b"\x00" * 32
WEBP_BYTES = s3.RIFF + b"\x00\x00\x00\x00" + s3.WEBP + b"\x00" * 32
GIF_BYTES = b"GIF89a" + b"\x00" * 32
AVIF_BYTES = b"\x00\x00\x00\x1cftypavif" + b"\x00" * 32
SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
PLAIN_SVG = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'


def bmp_bytes() -> bytes:
    """Pillow 로 만든 진짜 BMP. 브라우저가 그리지 않는 형식의 대표다."""
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 0, 0)).save(out, format="BMP")
    return out.getvalue()


def config() -> StorageConfig:
    return StorageConfig(
        endpoint="http://minio:9000",
        region="us-east-1",
        bucket="logos",
        access_key="minioadmin",
        secret_key="minioadmin",
        public_base="http://localhost:9000/logos",
    )


class FakeClient:
    """put_object 를 받아 적기만 하는 클라이언트."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {}


@pytest.mark.parametrize(
    ("data", "extension", "content_type"),
    [
        (PNG_BYTES, "png", "image/png"),
        (JPEG_BYTES, "jpg", "image/jpeg"),
        (WEBP_BYTES, "webp", "image/webp"),
        (GIF_BYTES, "gif", "image/gif"),
        (AVIF_BYTES, "avif", "image/avif"),
        (PLAIN_SVG, "svg", "image/svg+xml"),
    ],
)
def test_detects_accepted_formats(data: bytes, extension: str, content_type: str) -> None:
    kind = s3.detect_image(data)
    assert kind.extension == extension
    assert kind.content_type == content_type


@pytest.mark.parametrize(
    "svg",
    [
        SVG_BYTES,
        b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
        b'<svg><a href="javascript:alert(1)"><rect/></a></svg>',
    ],
)
def test_refuses_svg_with_script(svg: bytes) -> None:
    """스크립트를 품은 SVG 는 공개 주소에서 열면 그대로 돈다."""
    with pytest.raises(s3.StorageError) as caught:
        s3.prepare_image(svg)
    assert caught.value.reason == "not_an_image"
    assert "SVG" in caught.value.message


def test_converts_other_images_to_png() -> None:
    """브라우저가 그리지 않는 형식은 PNG 로 바꿔 올린다."""
    data, kind = s3.prepare_image(bmp_bytes())
    assert kind == s3.PNG_KIND
    assert data.startswith(s3.PNG)


def test_refuses_what_is_not_an_image() -> None:
    with pytest.raises(s3.StorageError) as caught:
        s3.prepare_image(b"MZ\x90\x00 not an image at all")
    assert caught.value.reason == "not_an_image"
    assert s3.ACCEPTED in caught.value.message


def test_refuses_oversize() -> None:
    with pytest.raises(s3.StorageError) as caught:
        s3.detect_image(s3.PNG + b"\x00" * s3.MAX_IMAGE_BYTES)
    assert caught.value.reason == "too_large"
    assert s3.MAX_IMAGE_LABEL in caught.value.message


def test_refuses_empty() -> None:
    with pytest.raises(s3.StorageError) as caught:
        s3.detect_image(b"")
    assert caught.value.reason == "not_an_image"


def test_extension_comes_from_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """이름이 무엇이든 내용이 JPEG 면 `.jpg` 로 올라간다."""
    fake = FakeClient()
    monkeypatch.setattr(s3, "client", lambda _config: fake)

    url = s3.upload_image(config(), data=JPEG_BYTES, name="acme")

    assert url == "http://localhost:9000/logos/acme.jpg"
    assert fake.calls[0]["Key"] == "acme.jpg"
    assert fake.calls[0]["ContentType"] == "image/jpeg"
    assert fake.calls[0]["Bucket"] == "logos"


def test_refused_upload_never_calls_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """거절된 파일은 저장소에 닿지 않는다."""
    fake = FakeClient()
    monkeypatch.setattr(s3, "client", lambda _config: fake)

    with pytest.raises(s3.StorageError):
        s3.upload_image(config(), data=SVG_BYTES, name="acme")
    assert fake.calls == []


def test_unconfigured_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient()
    monkeypatch.setattr(s3, "client", lambda _config: fake)

    with pytest.raises(s3.StorageError) as caught:
        s3.upload_image(StorageConfig(), data=PNG_BYTES, name="acme")
    assert caught.value.reason == "not_configured"
    assert fake.calls == []


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("InvalidAccessKeyId", "bad_credentials"),
        ("SignatureDoesNotMatch", "bad_credentials"),
        ("NoSuchBucket", "no_bucket"),
        ("AccessDenied", "denied"),
        ("InternalError", "failed"),
    ],
)
def test_translates_client_errors(code: str, reason: str) -> None:
    """키가 틀린 것과 버킷이 없는 것과 권한이 막힌 것이 갈려 나온다."""
    error = ClientError({"Error": {"Code": code}}, "PutObject")
    assert s3.translate(error, config()).reason == reason


def test_translates_unreachable() -> None:
    error = EndpointConnectionError(endpoint_url="http://minio:9000")
    translated = s3.translate(error, config())
    assert translated.reason == "unreachable"
    assert "http://minio:9000" in translated.message


def test_endpoint_url_is_none_when_empty() -> None:
    """엔드포인트가 비면 SDK 가 지역으로 주소를 만든다. 빈 문자열을 넘기지 않는다."""
    aws = StorageConfig(
        endpoint="",
        region="ap-northeast-2",
        bucket="logos",
        access_key="AKIA0000",
        secret_key="secret",
        public_base="https://logos.s3.ap-northeast-2.amazonaws.com",
    )
    built = s3.client(aws)
    assert built.meta.endpoint_url == "https://s3.ap-northeast-2.amazonaws.com"


def test_환경변수로_실제_S3_설정을_읽는다() -> None:
    from app.config import Settings
    from app.storage import settings as store

    env = Settings(
        s3_region="ap-northeast-2",
        s3_bucket="ogonggo-logos",
        s3_access_key="AKIA-test",
        s3_secret_key="secret-test",
    )
    config = store.env_config(env)

    assert config is not None and config.configured
    # 실제 S3 는 엔드포인트를 비우고, 공개 주소는 버킷 주소다
    assert config.endpoint == ""
    assert config.public_base == "https://ogonggo-logos.s3.ap-northeast-2.amazonaws.com"
    assert store.env_config(Settings()) is None


def test_화면에_저장한_값이_환경변수보다_먼저다(tmp_path: Any) -> None:
    from app import db
    from app.config import Settings
    from app.storage import settings as store

    env = Settings(s3_bucket="env-bucket", s3_access_key="a-env", s3_secret_key="s-env")
    conn = db.connect(tmp_path / "jobs.db")
    db.migrate_up(conn)

    assert store.read_config(conn, env).bucket == "env-bucket"
    store.write_config(conn, config())
    assert store.read_config(conn, env).bucket == "logos"
    conn.close()
