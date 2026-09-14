"""회사 화면 (6.1.V ~ 6.8.V).

보는 것은 넷이다. 네비게이션에 자리가 생겼는지, 그 주소가 열리면서 목록 조각을 부르는지,
행이 없을 때 화면이 "없음" 으로 끝내지 않고 언제 생기는지 말하는지, 그리고 공고 수가
많은 회사가 앞에 서는지.

조회 조건 둘(`로고 없음`, `공고 N건 이상`)을 함께 걸면 등록할 목록이 나온다. 걸린 회사 수와
공고 합계가 실제와 같은지가 그 조건이 쓸모 있는지를 정한다.

로고를 저장하면 그 회사명을 가진 공고 전부에 붙는다. 붙는 건수를 문장으로 알리는지, 그리고
그 숫자가 실제 공고 수와 같은지를 본다 — 공고마다 넣는 자리가 없으므로 그 문장이 운영자가
방금 한 일의 크기를 아는 유일한 자리다.

저장소 공개 주소를 바꾸면 이미 올린 파일은 따라가지 않는다. 그 사실이 행에 `옛 저장소` 로
보이는지도 본다.

파일 올리기는 저장소를 부르는 자리를 바꿔치기한다. 화면 테스트가 컨테이너가 떠 있는지에
매달리게 두지 않는다 (`tests/test_ui_storage.py` 와 같다). 실제 왕복은 로컬 MinIO 로 따로
확인한다.

공고는 정규화를 지나 넣는다. 화면이 세는 값과 로고가 실제로 붙는 경로가 같은 이름이라야
숫자가 뜻을 갖는다.
"""

from __future__ import annotations

import json
import pathlib
import re
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import companies, db
from app.api.settings import get_connection
from app.api.ui import NAV_GROUPS
from app.main import app
from app.normalize.engine import insert_normalized
from app.storage import s3
from app.storage import settings as store


def add_job(conn: sqlite3.Connection, company_name: str, seq: int, parent: str = "") -> None:
    """공고 한 건을 정규화까지 넣는다. 없던 회사면 행이 함께 생긴다."""
    record = {"title": f"공고 {seq}", "body": "본문", "company_name": company_name}
    if parent:
        record["parent_company_name"] = parent
    cursor = conn.execute(
        """
        INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash)
        VALUES (1, ?, ?, ?)
        """,
        (f"https://x/{seq}", json.dumps(record, ensure_ascii=False), f"hash-{seq}"),
    )
    insert_normalized(conn, int(cursor.lastrowid or 0), [])


def names_in_order(body: str) -> list[str]:
    """표에 그려진 회사명을 나온 순서대로. 정렬을 보는 유일한 방법이다."""
    return re.findall(r'<td class="cell-text font-medium text-slate-900">([^<]+)</td>', body)


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    # `raw_jobs.workflow_id` 가 외래키다. 공고를 넣는 테스트가 여기에 매달린다
    connection.execute(
        "INSERT INTO crawlers (id, name, list_url, status)"
        " VALUES (1, '테스트', 'https://x', 'draft')"
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '테스트')")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def client(tmp_path: pathlib.Path, conn: sqlite3.Connection) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(tmp_path / "jobs.db")
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[get_connection] = request_connection
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_네비게이션에_회사_로고가_있다() -> None:
    """개별 화면이 아니라 `데이터 확인` 묶음 안에 있다 (`app/api/ui.py` 의 `NAV_GROUPS`).

    이름도 `회사` 에서 `회사 로고` 로 바뀌었다 — 이 화면이 실제로 하는 일이 로고 관리다
    (2026-08-29 결정).
    """
    data_group = next(members for path, label, members in NAV_GROUPS if label == "데이터 확인")
    assert ("/companies", "회사 로고") in data_group


def test_회사_화면이_열리고_네비게이션이_켜진다(client: TestClient) -> None:
    response = client.get("/companies")

    assert response.status_code == 200
    assert '<a href="/companies" aria-current="page"' in response.text


def test_회사_화면이_목록_조각을_부른다(client: TestClient) -> None:
    assert 'hx-get="/ui/companies"' in client.get("/companies").text


def test_목록이_저장된_회사를_그린다(client: TestClient, conn: sqlite3.Connection) -> None:
    """모회사 열은 2026-08-29 에 뺐다 — 크롤러 등록 화면이 그 값을 필수로 받는다."""
    companies.ensure(conn, "삼성SDS", "삼성전자")
    companies.set_logo_url(conn, "삼성SDS", "https://cdn.test/sds.png")
    conn.commit()

    body = client.get("/ui/companies").text

    assert "삼성SDS" in body
    assert "https://cdn.test/sds.png" in body


def test_로고가_없으면_빈_칸으로_두지_않는다(client: TestClient, conn: sqlite3.Connection) -> None:
    companies.ensure(conn, "토스")
    conn.commit()

    assert "로고 없음" in client.get("/ui/companies").text


def test_행이_없으면_언제_생기는지_말한다(client: TestClient) -> None:
    body = client.get("/ui/companies").text

    assert "정규화되면 그 회사명으로 행이 생긴다" in body


def test_공고_수가_그_회사의_공고와_같다(client: TestClient, conn: sqlite3.Connection) -> None:
    add_job(conn, "토스", 1)
    add_job(conn, "토스", 2)
    add_job(conn, "당근", 3)
    conn.commit()

    body = client.get("/ui/companies").text

    assert "2건" in body
    assert "1건" in body


def test_공고가_많은_회사가_앞에_선다(client: TestClient, conn: sqlite3.Connection) -> None:
    """이름 순이면 로고 하나가 몇 건에 붙는지를 화면에서 알 수 없다."""
    for seq in range(1, 4):
        add_job(conn, "카카오", seq)
    add_job(conn, "가나다", 10)
    companies.ensure(conn, "공고없는회사")
    conn.commit()

    assert names_in_order(client.get("/ui/companies").text) == [
        "카카오",
        "가나다",
        "공고없는회사",
    ]


def test_공고가_하나도_없는_회사도_0건으로_남는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """빠지면 로고를 지울 회사를 화면에서 찾을 수 없다."""
    companies.ensure(conn, "폐업한회사")
    conn.commit()

    body = client.get("/ui/companies").text

    assert "폐업한회사" in body
    assert "0건" in body


def test_로고_없음_조건이_로고를_넣은_회사를_뺀다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    add_job(conn, "토스", 1)
    add_job(conn, "당근", 2)
    companies.set_logo_url(conn, "토스", "https://cdn.test/toss.png")
    conn.commit()

    body = client.get("/ui/companies", params={"no_logo": "on"}).text

    assert names_in_order(body) == ["당근"]


def test_공고_N건_이상_조건이_적은_회사를_뺀다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    for seq in range(1, 4):
        add_job(conn, "카카오", seq)
    add_job(conn, "당근", 10)
    conn.commit()

    body = client.get("/ui/companies", params={"min_jobs": "3"}).text

    assert names_in_order(body) == ["카카오"]


def test_둘을_걸면_등록할_목록만_남는다(client: TestClient, conn: sqlite3.Connection) -> None:
    """로고가 없으면서 공고가 여러 개인 회사. 이것이 먼저 등록할 목록이다."""
    for seq in range(1, 4):
        add_job(conn, "로고없는큰회사", seq)
    for seq in range(4, 7):
        add_job(conn, "로고있는큰회사", seq)
    add_job(conn, "로고없는작은회사", 7)
    companies.set_logo_url(conn, "로고있는큰회사", "https://cdn.test/x.png")
    conn.commit()

    body = client.get("/ui/companies", params={"no_logo": "on", "min_jobs": "2"}).text

    assert names_in_order(body) == ["로고없는큰회사"]


def test_걸린_회사_수와_공고_합계를_적는다(client: TestClient, conn: sqlite3.Connection) -> None:
    """숫자가 실제와 다르면 무엇을 먼저 등록할지를 이 화면으로 정할 수 없다."""
    for seq in range(1, 4):
        add_job(conn, "카카오", seq)
    for seq in range(4, 6):
        add_job(conn, "당근", seq)
    add_job(conn, "작은회사", 6)
    conn.commit()

    body = client.get("/ui/companies", params={"min_jobs": "2"}).text

    assert "조건에 걸린 자회사" in body
    assert "2곳, 공고 합계 5건" in body


def test_조건에_걸린_회사가_없으면_조건_탓임을_말한다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    add_job(conn, "토스", 1)
    conn.commit()

    body = client.get("/ui/companies", params={"min_jobs": "9"}).text

    assert "이 조건에 걸린 회사가 없다" in body


def test_숫자_칸을_비워도_조건_없음으로_읽는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """정수로 받으면 빈 값이 422 가 되어 조건을 지우려던 조작이 오류로 돌아온다."""
    add_job(conn, "토스", 1)
    conn.commit()

    response = client.get("/ui/companies", params={"min_jobs": ""})

    assert response.status_code == 200
    assert names_in_order(response.text) == ["토스"]


def test_화면에_조회_조건_둘이_있다(client: TestClient) -> None:
    body = client.get("/companies").text

    assert 'name="no_logo"' in body
    assert 'name="min_jobs"' in body


def test_로고를_저장하면_붙는_건수를_문장으로_알린다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    for seq in range(1, 4):
        add_job(conn, "카카오", seq)
    conn.commit()

    response = client.put(
        "/ui/companies/logo",
        data={"name": "카카오", "logo_url": "https://cdn.test/kakao.png"},
    )

    assert response.status_code == 200
    assert "공고 3건에 붙는다" in response.text
    assert "공고마다 따로 넣지 않는다" in response.text
    stored = companies.read(conn, "카카오")
    assert stored is not None and stored.logo_url == "https://cdn.test/kakao.png"


def test_알린_건수가_그_회사의_공고_수와_같다(client: TestClient, conn: sqlite3.Connection) -> None:
    """다른 회사의 공고까지 세면 운영자는 붙지도 않은 건수를 붙었다고 읽는다."""
    for seq in range(1, 3):
        add_job(conn, "토스", seq)
    for seq in range(3, 8):
        add_job(conn, "당근", seq)
    conn.commit()

    body = client.put(
        "/ui/companies/logo", data={"name": "토스", "logo_url": "https://cdn.test/t.png"}
    ).text

    counted = conn.execute(
        "SELECT count(*) AS n FROM normalized_jobs WHERE company_name = '토스'"
    ).fetchone()["n"]
    assert counted == 2
    assert f"공고 {counted}건에 붙는다" in body


def test_공고가_없는_회사에_저장하면_다음부터_붙는다고_말한다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    companies.ensure(conn, "새회사")
    conn.commit()

    body = client.put(
        "/ui/companies/logo", data={"name": "새회사", "logo_url": "https://cdn.test/n.png"}
    ).text

    assert "지금은 이 회사명을 가진 공고가 없다" in body


def test_주소를_비우면_지우고_빠지는_건수를_말한다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """지우기 단추를 따로 두지 않는다. 비움이 곧 지움이다."""
    for seq in range(1, 3):
        add_job(conn, "토스", seq)
    companies.set_logo_url(conn, "토스", "https://cdn.test/t.png")
    conn.commit()

    body = client.put("/ui/companies/logo", data={"name": "토스", "logo_url": " "}).text

    assert "로고를 지웠다" in body
    assert "공고 2건에서 함께 빠진다" in body
    stored = companies.read(conn, "토스")
    assert stored is not None and stored.logo_url is None


def test_없는_회사에_저장하면_사유를_말한다(client: TestClient) -> None:
    """행을 만드는 것은 정규화다. 화면이 손으로 만들면 오타 하나로 로고가 안 붙는다."""
    response = client.put(
        "/ui/companies/logo", data={"name": "없는회사", "logo_url": "https://cdn.test/x.png"}
    )

    assert response.status_code == 200
    assert "회사 행이 없다" in response.text


def test_저장한_뒤에도_그_줄만_갈린다(client: TestClient, conn: sqlite3.Connection) -> None:
    """목록을 통째로 다시 부르면 방금 적은 문장이 사라진다."""
    add_job(conn, "토스", 1)
    add_job(conn, "당근", 2)
    conn.commit()

    body = client.put(
        "/ui/companies/logo", data={"name": "토스", "logo_url": "https://cdn.test/t.png"}
    ).text

    assert names_in_order(body) == ["토스"]


def test_목록에_로고_저장_폼이_있다(client: TestClient, conn: sqlite3.Connection) -> None:
    add_job(conn, "토스", 1)
    conn.commit()

    body = client.get("/ui/companies").text

    assert 'hx-put="/ui/companies/logo"' in body
    assert 'name="logo_url"' in body


def save_storage(conn: sqlite3.Connection, public_base: str) -> None:
    """저장소 설정 한 벌. 공개 주소만 이 테스트의 관심사다."""
    store.write_config(
        conn,
        store.StorageConfig(
            endpoint="http://minio:9000",
            region="us-east-1",
            bucket="logos",
            access_key="minioadmin",
            secret_key="minioadmin",
            public_base=public_base,
        ),
    )


def test_지금_저장소로_시작하는_주소에는_표시가_없다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    save_storage(conn, "http://localhost:9000/logos")
    companies.ensure(conn, "토스")
    companies.set_logo_url(conn, "토스", "http://localhost:9000/logos/toss.png")
    conn.commit()

    assert "옛 저장소" not in client.get("/ui/companies").text


def test_공개_주소를_바꾸면_기존_행에_옛_저장소가_뜬다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """엔드포인트를 바꿔도 이미 올린 파일은 따라가지 않는다. 무엇을 다시 올릴지가 보여야 한다."""
    save_storage(conn, "http://localhost:9000/logos")
    companies.ensure(conn, "토스")
    companies.set_logo_url(conn, "토스", "http://localhost:9000/logos/toss.png")
    conn.commit()
    assert "옛 저장소" not in client.get("/ui/companies").text

    save_storage(conn, "https://cdn.example.com/logos")
    conn.commit()

    assert "옛 저장소" in client.get("/ui/companies").text


def test_로고가_없는_행에는_표시가_붙지_않는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    save_storage(conn, "https://cdn.example.com/logos")
    companies.ensure(conn, "토스")
    conn.commit()

    assert "옛 저장소" not in client.get("/ui/companies").text


def test_저장_직후의_줄에도_같은_표시가_붙는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    save_storage(conn, "https://cdn.example.com/logos")
    companies.ensure(conn, "토스")
    conn.commit()

    body = client.put(
        "/ui/companies/logo", data={"name": "토스", "logo_url": "https://elsewhere.test/t.png"}
    ).text

    assert "옛 저장소" in body


def test_화면이_옛_저장소가_무슨_뜻인지_적는다(client: TestClient) -> None:
    """붙여넣은 외부 주소도 같은 표시를 받는다. 그 사실을 적지 않으면 없는 문제를 고치게 된다."""
    body = client.get("/companies").text

    assert "밖에 올려 둔 주소를 붙여넣은 것이면 그대로 두어도 된다" in body


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def test_파일을_올리면_주소가_회사에_적히고_미리보기가_나온다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_storage(conn, "http://localhost:9000/logos")
    for seq in range(1, 3):
        add_job(conn, "토스", seq)
    conn.commit()
    uploaded: dict[str, object] = {}

    def fake_upload(config: store.StorageConfig, *, data: bytes, name: str) -> str:
        uploaded["data"] = data
        uploaded["name"] = name
        return f"{config.public_base}/{name}.png"

    monkeypatch.setattr(s3, "upload_image", fake_upload)

    response = client.post(
        "/ui/companies/logo/upload",
        data={"name": "토스"},
        files={"file": ("toss.png", PNG_BYTES, "image/png")},
    )

    assert response.status_code == 200
    assert uploaded["data"] == PNG_BYTES
    stored = companies.read(conn, "토스")
    assert stored is not None and stored.logo_url is not None
    assert f'<img src="{stored.logo_url}"' in response.text
    assert "공고 2건에 붙는다" in response.text


def test_올린_객체_이름에_회사명을_넣지_않는다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """이름이 한글이라 공개 주소가 퍼센트 인코딩으로 덮이고, 회사명이 바뀌면 어긋난다."""
    save_storage(conn, "http://localhost:9000/logos")
    companies.ensure(conn, "토스")
    conn.commit()
    seen: dict[str, str] = {}

    def fake_upload(config: store.StorageConfig, *, data: bytes, name: str) -> str:
        seen["name"] = name
        return f"{config.public_base}/{name}.png"

    monkeypatch.setattr(s3, "upload_image", fake_upload)

    client.post(
        "/ui/companies/logo/upload",
        data={"name": "토스"},
        files={"file": ("toss.png", PNG_BYTES, "image/png")},
    )

    assert "토스" not in seen["name"]
    assert seen["name"].startswith("company_name/")


def test_저장소가_거절하면_사유가_그_줄에_남는다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """조용히 실패하면 운영자는 로고가 왜 안 붙는지 알 수 없다."""
    save_storage(conn, "http://localhost:9000/logos")
    companies.ensure(conn, "토스")
    conn.commit()

    def refuse(config: store.StorageConfig, *, data: bytes, name: str) -> str:
        raise s3.StorageError("not_an_image", "받는 형식이 아니다. PNG, JPEG, WebP 만 올릴 수 있다")

    monkeypatch.setattr(s3, "upload_image", refuse)

    body = client.post(
        "/ui/companies/logo/upload",
        data={"name": "토스"},
        files={"file": ("toss.svg", b"<svg/>", "image/svg+xml")},
    ).text

    assert "올리지 못했다" in body
    assert "받는 형식이 아니다" in body
    assert companies.read(conn, "토스") is not None
    stored = companies.read(conn, "토스")
    assert stored is not None and stored.logo_url is None


def test_저장소가_설정되지_않으면_파일_고르기_대신_할_일을_적는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    companies.ensure(conn, "토스")
    conn.commit()

    body = client.get("/ui/companies").text

    assert 'hx-post="/ui/companies/logo/upload"' not in body
    assert "파일 저장소가 아직 설정되지 않았다" in body


def test_저장소가_설정되면_파일_고르기가_나온다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    save_storage(conn, "http://localhost:9000/logos")
    companies.ensure(conn, "토스")
    conn.commit()

    body = client.get("/ui/companies").text

    assert 'hx-post="/ui/companies/logo/upload"' in body
    assert 'hx-encoding="multipart/form-data"' in body


def test_화면이_받는_형식과_상한을_적는다(client: TestClient) -> None:
    body = client.get("/companies").text

    assert s3.ACCEPTED in body
    assert s3.MAX_IMAGE_LABEL in body
    assert "SVG 는 받지 않는다" in body


def test_밖에_올려_둔_주소를_붙여넣으면_저장되고_미리보기가_뜬다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """우리 저장소를 거치지 않는 길이다. 파일은 남의 곳에 있고 우리는 주소만 갖는다."""
    save_storage(conn, "http://localhost:9000/logos")
    add_job(conn, "당근", 1)
    conn.commit()

    body = client.put(
        "/ui/companies/logo",
        data={"name": "당근", "logo_url": "https://cdn.elsewhere.test/daangn.png"},
    ).text

    stored = companies.read(conn, "당근")
    assert stored is not None and stored.logo_url == "https://cdn.elsewhere.test/daangn.png"
    assert '<img src="https://cdn.elsewhere.test/daangn.png"' in body


def test_http_로_시작하지_않는_주소는_받지_않는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """브라우저의 type=url 은 화면을 지나는 값만 막는다. 저장되면 그것이 곧 목록의 링크다."""
    companies.ensure(conn, "당근")
    conn.commit()

    body = client.put(
        "/ui/companies/logo",
        data={"name": "당근", "logo_url": "javascript:alert(1)"},
    ).text

    assert "주소를 받지 않았다" in body
    assert "http:// 나 https:// 로 시작해야 한다" in body
    stored = companies.read(conn, "당근")
    assert stored is not None and stored.logo_url is None


def test_거절해도_그_회사의_옛_주소는_그대로다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    companies.ensure(conn, "당근")
    companies.set_logo_url(conn, "당근", "https://cdn.elsewhere.test/old.png")
    conn.commit()

    client.put("/ui/companies/logo", data={"name": "당근", "logo_url": "ftp://x/y.png"})

    stored = companies.read(conn, "당근")
    assert stored is not None and stored.logo_url == "https://cdn.elsewhere.test/old.png"


def test_붙여넣기_길이_저장소를_거치지_않는다고_화면에_적힌다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    companies.ensure(conn, "당근")
    conn.commit()

    body = client.get("/ui/companies").text

    assert "우리 저장소를 거치지 않는다" in body


def test_모회사_탭은_다른_회사가_가리키는_행만_보여준다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """`register()` 가 만드는 진짜 모회사 행만 나온다. 자회사가 없는 단독 회사는 아무도
    가리키지 않으니 여기 나오지 않는다."""
    companies.register(conn, "삼성SDS", "삼성전자")
    companies.register(conn, "토스", "")
    conn.commit()

    subsidiary_body = client.get("/ui/companies").text
    parent_body = client.get("/ui/companies", params={"group": "parent"}).text

    assert "삼성SDS" in subsidiary_body
    assert "삼성전자" not in subsidiary_body
    assert "토스" in subsidiary_body

    assert "삼성전자" in parent_body
    assert "삼성SDS" not in parent_body
    assert "토스" not in parent_body


def test_모회사_탭에_로고_저장_폼이_있다(client: TestClient, conn: sqlite3.Connection) -> None:
    companies.register(conn, "삼성SDS", "삼성전자")
    conn.commit()

    body = client.get("/ui/companies", params={"group": "parent"}).text

    assert 'hx-put="/ui/companies/logo"' in body
    assert 'value="삼성전자"' in body


def test_모회사_행이_없으면_생기는_때를_말한다(client: TestClient) -> None:
    body = client.get("/ui/companies", params={"group": "parent"}).text

    assert "자회사가 있는 사이트가 수집되면" in body


def test_화면에_자회사_모회사_탭이_있다(client: TestClient) -> None:
    body = client.get("/companies").text

    assert 'name="group"' in body
    assert 'value="subsidiary"' in body
    assert 'value="parent"' in body


def test_모회사_열과_고치는_길은_더_이상_없다(client: TestClient, conn: sqlite3.Connection) -> None:
    """2026-08-29 결정. 크롤러 등록 화면이 모회사 이름을 필수로 받으면서 이 화면에서 사람이
    따로 고치는 자리는 필요 없어졌다 — 남기면 두 화면의 모회사 값이 갈리기만 한다."""
    companies.ensure(conn, "삼성SDS", "삼성전자")
    conn.commit()

    body = client.get("/ui/companies").text

    assert "모회사" not in body
    assert 'hx-put="/ui/companies/parent"' not in body

    response = client.put("/ui/companies/parent", data={"name": "삼성SDS", "parent_name": "x"})
    assert response.status_code == 404
