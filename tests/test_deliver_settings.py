"""오공고(Spring) 전송 설정 저장소.

`app/notify/settings.py` 와 같은 자리다. 키는 여기 없다 — 크롤러 `.env` 에서 읽는다
(2026-09-15 결정).

| 확인 | 깨지면 |
|---|---|
| 값이 없으면 꺼져 있고 주소가 비어 있다 | 설치하자마자 어디론가 보낸다 |
| 저장한 값이 그대로 읽힌다 | 화면에서 켠 전송이 돌지 않는다 |
| 틀린 주소·건수, 주소 없이 켜기는 거절하고 아무것도 저장하지 않는다 | 반쯤 저장된 설정으로 보낸다 |
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db
from app.deliver.settings import DeliverConfig, DeliverSettingError, read_config, write_config


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


def test_값이_없으면_꺼져_있다(conn: sqlite3.Connection) -> None:
    config = read_config(conn)

    assert (config.url, config.enabled, config.batch_size) == ("", False, 100)
    assert config.configured is False


def test_저장하고_다시_읽으면_그대로다(conn: sqlite3.Connection) -> None:
    write_config(
        conn, DeliverConfig(url="https://admin-api.example.com", enabled=True, batch_size=50)
    )

    config = read_config(conn)

    assert (config.url, config.enabled, config.batch_size) == (
        "https://admin-api.example.com",
        True,
        50,
    )
    assert config.configured is True


def test_http로_시작하지_않는_주소는_거절된다(conn: sqlite3.Connection) -> None:
    with pytest.raises(DeliverSettingError, match="http"):
        write_config(conn, DeliverConfig(url="ftp://x.example.com"))

    assert read_config(conn).url == ""


def test_주소_없이는_켤_수_없다(conn: sqlite3.Connection) -> None:
    with pytest.raises(DeliverSettingError, match="켤 수 없다"):
        write_config(conn, DeliverConfig(enabled=True))

    assert read_config(conn).enabled is False


def test_한_번에_보내는_건수는_1_이상이어야_한다(conn: sqlite3.Connection) -> None:
    with pytest.raises(DeliverSettingError, match="1 이상"):
        write_config(conn, DeliverConfig(url="https://x.example.com", batch_size=0))
