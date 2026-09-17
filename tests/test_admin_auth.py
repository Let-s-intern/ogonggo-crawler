"""운영 화면 잠금.

공개 URL 에 떠 있는 동안 주소를 아는 누구나 DB 를 내려받고 크롤러를 지울 수 있었다. 그 구멍을
막는 자물쇠라, 여기서 확인하는 것은 "로그인이 된다" 가 아니라 **잠기지 않은 길이 남아 있지
않다** 는 쪽이다.

`/health` 만은 열려 있어야 한다. Coolify 가 이 응답으로 배포 성공을 판정한다 — 잠그면 배포가
실패한다.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api import auth
from app.config import get_settings
from app.main import app


@pytest.fixture
def client() -> Iterator[TestClient]:
    """기본 비밀번호로 뜬 서버. 쿠키는 conftest 가 붙여 준다."""
    os.environ["ADMIN_PASSWORD"] = auth.DEFAULT_PASSWORD
    get_settings.cache_clear()
    yield TestClient(app, follow_redirects=False)
    os.environ.pop("ADMIN_PASSWORD", None)
    get_settings.cache_clear()


@pytest.fixture
def locked(client: TestClient) -> TestClient:
    """쿠키 없는 손님. 주소만 알고 들어온 쪽이다."""
    client.cookies.clear()
    return client


# 잠긴 경로. 화면·조각·운영 API·제공 API 를 한 줄씩 대표로 둔다
LOCKED_PATHS = [
    "/",
    "/settings",
    "/jobs",
    "/ui/health",
    "/ui/settings/export",
    "/api/crawlers",
    "/api/workflows",
    "/api/jobs",
]


def test_health_is_open_without_cookie(locked: TestClient) -> None:
    """Coolify 의 배포 판정이 여기 걸려 있다. 잠그면 배포가 실패한다."""
    response = locked.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    # 어떤 코드가 떠 있는지 같이 돌려준다. 이미지 태그를 고정하지 않으므로 배포된 커밋을
    # 아는 길이 이 값뿐이다 (`docker-compose.coolify.yml`)
    assert body["build"]


@pytest.mark.parametrize("path", LOCKED_PATHS)
def test_locked_without_cookie(locked: TestClient, path: str) -> None:
    response = locked.get(path)

    # 화면은 로그인으로 보내고(303), 그 밖은 거절한다(401). 200 이 나오면 열려 있는 것이다
    assert response.status_code in (303, 401)


def test_page_sends_the_visitor_to_login(locked: TestClient) -> None:
    response = locked.get("/settings", headers={"accept": "text/html"})

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


def test_api_answers_401_not_a_login_page(locked: TestClient) -> None:
    """소비 측과 스크립트는 리다이렉트를 로그인 화면으로 삼키면 안 된다."""
    response = locked.get("/api/crawlers")

    assert response.status_code == 401


def test_delivery_api_is_locked(locked: TestClient) -> None:
    """소비 측이 아직 안 붙었다. 열어 두면 정규화된 공고가 그대로 공개된다."""
    assert locked.get("/api/jobs").status_code == 401
    assert locked.post("/api/jobs/delivered", json={"ids": [1]}).status_code == 401


def test_htmx_gets_a_redirect_header(locked: TestClient) -> None:
    """HTMX 는 리다이렉트를 따라가 로그인 화면을 조각 자리에 갈아 넣는다."""
    response = locked.get("/ui/health", headers={"hx-request": "true"})

    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/login"


@pytest.mark.parametrize(
    "forged",
    [
        "1",
        "abc.def",
        ".",
        f"{int(time.time())}.",
        f"{int(time.time())}.{'A' * 43}",
    ],
)
def test_made_up_cookie_is_refused(locked: TestClient, forged: str) -> None:
    """서명하지 않았다면 값을 지어내는 것으로 잠금이 통째로 없는 것과 같아진다."""
    locked.cookies.set(auth.COOKIE_NAME, forged)

    assert locked.get("/api/crawlers").status_code == 401


def test_cookie_signed_with_another_password_is_refused(locked: TestClient) -> None:
    """서명 키가 비밀번호에서 나온다. 비밀번호를 바꾸면 나간 쿠키가 전부 무효다."""
    os.environ["ADMIN_PASSWORD"] = "다른-비밀번호"
    get_settings.cache_clear()
    stale = auth.issue_token()
    os.environ["ADMIN_PASSWORD"] = auth.DEFAULT_PASSWORD
    get_settings.cache_clear()

    locked.cookies.set(auth.COOKIE_NAME, stale)

    assert locked.get("/api/crawlers").status_code == 401


def test_expired_cookie_is_refused(locked: TestClient) -> None:
    issued = str(int(time.time()) - auth.MAX_AGE_SECONDS - 60)
    locked.cookies.set(auth.COOKIE_NAME, f"{issued}.{auth._sign(issued)}")

    assert locked.get("/api/crawlers").status_code == 401


def test_non_ascii_cookie_is_refused_not_crashed() -> None:
    """쿠키 값은 밖에서 온다. ASCII 밖의 글자가 들어와도 거절이지 500 이 아니다."""
    assert auth.token_is_valid("1756000000.한글서명") is False


def test_korean_password_is_accepted(locked: TestClient) -> None:
    """운영자가 한글 비밀번호를 넣을 수 있다. 견주는 자리가 터지면 로그인이 통째로 막힌다."""
    os.environ["ADMIN_PASSWORD"] = "한글-비밀번호"
    get_settings.cache_clear()

    response = locked.post("/login", data={"password": "한글-비밀번호", "next": "/"})

    assert response.status_code == 303
    assert locked.get("/review").status_code == 200


def test_empty_password_is_treated_as_unset(locked: TestClient) -> None:
    """`.env.example` 을 그대로 복사하면 이름만 있고 값이 빈 줄이 생긴다.

    그것을 빈 비밀번호로 받으면 아무것도 입력하지 않고 열리면서 경고도 뜨지 않는다.
    """
    os.environ["ADMIN_PASSWORD"] = ""
    get_settings.cache_clear()

    # 빈 값으로는 열리지 않는다. 폼 검사가 먼저 걷어내므로 303 이 아니기만 하면 된다
    assert locked.post("/login", data={"password": "", "next": "/"}).status_code != 303
    assert locked.get("/api/crawlers").status_code == 401
    # 설정하지 않은 것으로 보므로 기본값 경고가 뜨고, 기본값으로 열린다
    assert auth.admin_password_is_default() is True
    assert (
        locked.post("/login", data={"password": auth.DEFAULT_PASSWORD, "next": "/"}).status_code
        == 303
    )


def test_login_page_is_open(locked: TestClient) -> None:
    response = locked.get("/login")

    assert response.status_code == 200
    assert "비밀번호" in response.text


def test_correct_password_opens_the_lock(locked: TestClient) -> None:
    response = locked.post("/login", data={"password": auth.DEFAULT_PASSWORD, "next": "/settings"})

    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    assert auth.COOKIE_NAME in response.cookies
    # 받은 쿠키로 잠긴 화면에 들어간다
    assert locked.get("/review").status_code == 200


def test_wrong_password_is_refused_without_saying_why(locked: TestClient) -> None:
    response = locked.post("/login", data={"password": "틀린값", "next": "/"})

    assert response.status_code == 401
    assert auth.COOKIE_NAME not in response.cookies
    # 무엇이 틀렸는지도, 시도한 값도 화면에 남기지 않는다
    assert "틀린값" not in response.text
    assert locked.get("/api/crawlers").status_code == 401


def test_password_value_never_reaches_the_screen(locked: TestClient) -> None:
    assert auth.DEFAULT_PASSWORD not in locked.get("/login").text


def test_logout_takes_the_cookie_back(locked: TestClient) -> None:
    """먼저 정상으로 받은 쿠키가, 로그아웃 뒤에 지워지라는 지시로 돌아온다."""
    locked.post("/login", data={"password": auth.DEFAULT_PASSWORD, "next": "/"})
    assert locked.get("/review").status_code == 200

    response = locked.post("/logout")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    instruction = response.headers["set-cookie"]
    assert instruction.startswith(f'{auth.COOKIE_NAME}=""')
    assert "Max-Age=0" in instruction
    assert locked.get("/api/crawlers").status_code == 401


def test_login_refuses_an_outside_target(locked: TestClient) -> None:
    """돌아갈 자리로 남의 주소를 넣어 튕겨 보내지 못하게 한다."""
    response = locked.post(
        "/login", data={"password": auth.DEFAULT_PASSWORD, "next": "//evil.example"}
    )

    assert response.headers["location"] == "/"


def test_default_password_is_announced_on_screen(client: TestClient) -> None:
    """공개 주소에서 기본값은 잠기지 않은 것과 같다. 운영자가 모른 채 두면 안 된다."""
    assert "ADMIN_PASSWORD" in client.get("/review").text
    # 로그인 화면은 아직 못 들어온 쪽이 본다
    client.cookies.clear()
    assert "ADMIN_PASSWORD" in client.get("/login").text


def test_no_warning_once_the_password_is_set(client: TestClient) -> None:
    os.environ["ADMIN_PASSWORD"] = "충분히-긴-운영-비밀번호"
    get_settings.cache_clear()
    client.cookies.clear()

    assert "기본 비밀번호" not in client.get("/login").text
