"""설정 로딩 테스트."""

import pathlib

import pytest

from app.config import Settings, get_settings

ENV_NAMES = [
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "CLAUDE_API_KEY",
    "CLAUDE_MODEL",
    "GPT_API_KEY",
    "GPT_MODEL",
    "QWEN_API_KEY",
    "QWEN_MODEL",
    "QWEN_BASE_URL",
    "SELECTOR_GENERATE_PROVIDER",
    "SELECTOR_REPAIR_PROVIDER",
    "CLASSIFY_PROVIDER",
    "DATABASE_PATH",
    "CRAWL_USER_AGENT",
    "CRAWL_DELAY_SECONDS",
    "CRAWL_TIMEOUT_SECONDS",
    "CRAWL_MAX_RETRIES",
    "MAX_CONCURRENT_RUNS",
    "RUN_TIMEOUT_SECONDS",
]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """환경변수를 걷어내고, `.env` 가 없는 디렉터리로 옮겨 순수 기본값만 남긴다."""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def test_defaults_are_conservative(clean_env: None) -> None:
    settings = Settings()

    assert settings.database_path == "./data/jobs.db"
    assert settings.crawl_delay_seconds == 3.0
    assert settings.crawl_timeout_seconds == 20.0
    assert settings.crawl_max_retries == 3
    assert settings.max_concurrent_runs == 3
    assert settings.run_timeout_seconds == 600
    assert "job-crawler-automation" in settings.crawl_user_agent


def test_env_overrides_defaults(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", "/srv/other.db")
    monkeypatch.setenv("CRAWL_DELAY_SECONDS", "9.5")
    monkeypatch.setenv("MAX_CONCURRENT_RUNS", "1")

    settings = Settings()

    assert settings.database_path == "/srv/other.db"
    assert settings.crawl_delay_seconds == 9.5
    assert settings.max_concurrent_runs == 1


def test_loads_without_api_key(clean_env: None) -> None:
    """키가 없어도 설정 로딩은 성공한다. 실패하는 것은 셀렉터 생성뿐이다."""
    settings = Settings()

    assert settings.gemini_api_key == ""
    assert settings.gemini_model == "gemini-3.5-flash"


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


def test_no_provider_key_is_required_to_load(clean_env: None) -> None:
    """넷 다 없어도 설정 로딩과 서버 기동은 성공한다. 실패하는 것은 그 기능뿐이다."""
    settings = Settings()

    assert settings.gemini_api_key == ""
    assert settings.claude_api_key == ""
    assert settings.gpt_api_key == ""
    assert settings.qwen_api_key == ""


def test_the_default_models_are_the_ones_the_research_settled_on(clean_env: None) -> None:
    """기본 모델이 바뀌면 비용이 바뀐다. 바꾸려면 이 줄을 같이 고치게 둔다."""
    settings = Settings()

    assert settings.gemini_model == "gemini-3.5-flash"
    assert settings.claude_model == "claude-haiku-4-5-20251001"
    assert settings.gpt_model == "gpt-5.6-luna"
    # 별칭이 아니다. 별칭은 응답을 스키마로 강제하지 못해 분류에 쓸 수 없다
    assert settings.qwen_model == "qwen3.8-flash"
    assert "dashscope" in settings.qwen_base_url


def test_every_feature_falls_back_to_gemini_when_nothing_is_chosen(clean_env: None) -> None:
    """고르지 않았을 때 어디로 떨어지는지를 잠근다. 지금까지 쓰던 제공자여야 한다."""
    settings = Settings()

    assert settings.selector_generate_provider == "gemini"
    assert settings.selector_repair_provider == "gemini"
    assert settings.classify_provider == "gemini"
    assert settings.image_read_provider == "gemini"


def test_each_feature_can_point_at_a_different_provider(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """분류만 싼 제공자로 옮기는 것이 이 설정을 두는 이유다."""
    monkeypatch.setenv("SELECTOR_GENERATE_PROVIDER", "claude")
    monkeypatch.setenv("CLASSIFY_PROVIDER", "qwen")

    settings = Settings()

    assert settings.selector_generate_provider == "claude"
    assert settings.classify_provider == "qwen"
    assert settings.selector_repair_provider == "gemini"


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
@pytest.mark.parametrize(
    ("name", "field", "expected"),
    [
        ("GEMINI_MODEL", "gemini_model", "gemini-3.5-flash"),
        ("CLAUDE_MODEL", "claude_model", "claude-haiku-4-5-20251001"),
        ("GPT_MODEL", "gpt_model", "gpt-5.6-luna"),
        ("QWEN_MODEL", "qwen_model", "qwen3.8-flash"),
        ("CLASSIFY_PROVIDER", "classify_provider", "gemini"),
        ("SELECTOR_GENERATE_PROVIDER", "selector_generate_provider", "gemini"),
        ("SELECTOR_REPAIR_PROVIDER", "selector_repair_provider", "gemini"),
        ("IMAGE_READ_PROVIDER", "image_read_provider", "gemini"),
    ],
)
def test_a_blank_value_is_not_a_value(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    blank: str,
    name: str,
    field: str,
    expected: str,
) -> None:
    """compose 는 변수를 넣지 않아도 `""` 를 채워 넘긴다.

    그대로 두면 없는 모델을 부르거나 없는 제공자를 찾게 되고, 증상은 401 이나 404 로 나타나
    원인이 설정이라는 것이 보이지 않는다.
    """
    monkeypatch.setenv(name, blank)

    assert getattr(Settings(), field) == expected


def test_a_blank_base_url_falls_back_instead_of_pointing_at_nothing(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QWEN_BASE_URL", "")

    assert "dashscope" in Settings().qwen_base_url


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_key_that_is_only_whitespace_counts_as_no_key(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    """공백만 든 키는 `if not key` 를 통과한다. 걷어내지 않으면 401 로만 나타난다."""
    monkeypatch.setenv("QWEN_API_KEY", blank)

    assert Settings().qwen_api_key == ""


def test_a_real_key_keeps_its_value(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEN_API_KEY", "  sk-진짜키  ")

    assert Settings().qwen_api_key == "sk-진짜키"
