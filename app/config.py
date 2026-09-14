"""환경변수 로딩. 이름은 `.env.example` 이 문서화한다."""

from functools import lru_cache
from typing import Any

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 빈 문자열을 값으로 받으면 안 되는 칸. compose 는 변수를 넣지 않아도 `""` 를 채워 넘긴다.
#
# 모델 ID 나 제공자 이름이 `""` 로 들어오면 없는 모델을 부르거나 없는 제공자를 찾게 되고,
# 증상은 401 이나 404 로 나타나 원인이 설정이라는 것이 보이지 않는다. `ADMIN_PASSWORD` 와
# `CRAWL_USER_AGENT` 에서 같은 것에 두 번 걸렸다
_BLANK_FALLS_BACK = (
    "gemini_model",
    "qwen_model",
    "qwen_base_url",
    "claude_model",
    "gpt_model",
    "ollama_model",
    "ollama_base_url",
    "selector_generate_provider",
    "selector_repair_provider",
    "classify_provider",
    "image_read_provider",
)

# 앞뒤 공백만 걷어낸다. 비어 있는 것은 "키가 없다" 가 맞는 값이라 그대로 둔다.
# 공백만 든 키는 `if not key` 를 통과해 버려서, 걷어내지 않으면 401 로만 나타난다
_KEYS = (
    "gemini_api_key",
    "qwen_api_key",
    "claude_api_key",
    "gpt_api_key",
    "ollama_api_key",
)


class Settings(BaseSettings):
    """`.env.example` 의 변수를 전부 읽는다. 기본값은 보수적으로(느리게) 둔다."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 모델 제공자. 키가 없어도 임포트와 서버 기동은 성공한다 — 그 제공자를 쓰는 기능만
    # 실패하고, 조용히 다른 제공자로 넘어가지 않는다 (`.claude/rules/llm.md`)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"

    # Qwen 은 OpenAI 호환 엔드포인트로 부른다. 기본 모델을 별칭(`qwen-plus` 등)으로 두지
    # 않는 것은 별칭이 응답을 스키마로 강제하지 못해 분류에 쓸 수 없어서다
    # (`app/llm/openai_compat.py`)
    qwen_api_key: str = ""
    qwen_model: str = "qwen3.8-flash"
    # 문서는 워크스페이스 전용 도메인을 권한다. 그 주소는 콘솔에서 봐야 알 수 있어 기본값으로
    # 둘 수 없다. 옮길 때 이 값을 바꾼다
    qwen_base_url: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

    claude_api_key: str = ""
    claude_model: str = "claude-haiku-4-5-20251001"

    gpt_api_key: str = ""
    gpt_model: str = "gpt-5.6-luna"

    # Ollama Cloud 도 OpenAI 호환 엔드포인트로 부른다. 기본 모델을 두는 것은 `models.list()`
    # 로 지금 있는 것을 확인했기 때문이고, 클라우드 모델 목록은 자주 바뀌므로 화면에서
    # 고르는 것이 정상 경로다 (`.claude/tasks/memos/llm-provider-조사.md`)
    ollama_api_key: str = ""
    ollama_model: str = "gpt-oss:120b"
    ollama_base_url: str = "https://ollama.com/v1"

    # 기능마다 어느 제공자를 쓰는가. 셋의 성격이 달라서 한 제공자로 다 하지 않는다 —
    # 셀렉터 생성은 등록할 때 한 번이고, 분류는 공고마다 한 번이라 비용의 대부분이 거기다.
    # 싼 제공자를 분류에 두고 정확한 제공자를 생성에 두는 선택이 가능해야 한다.
    #
    # 이름은 `app/llm/providers.py` 의 `PROVIDERS` 열쇠다. 없는 이름을 넣으면 그 기능이
    # `unknown_provider` 로 서고, 조용히 다른 제공자로 넘어가지 않는다
    selector_generate_provider: str = "gemini"
    selector_repair_provider: str = "gemini"
    classify_provider: str = "gemini"
    # 본문이 이미지로만 올라온 공고의 이미지를 수집할 때 읽는다. 글 읽기에 강한 모델을 분류와
    # 따로 둘 수 있게 기능을 나눈다 (`app/crawler/images.py`)
    image_read_provider: str = "gemini"

    # 화면에서 추가한 OpenAI 호환 회사. 이름 → 정의 JSON, 이름 → 키, 이름 → 모델이다.
    # 배포가 넣는 값이 아니다 — `app/llm/settings.py` 의 `settings_for` 가 기능마다 설정 사본을
    # 만들 때 DB 에서 채운다. 회사 이름이 정해져 있지 않아 칸 대신 사전이다 (`app/llm/custom.py`)
    llm_custom_providers: dict[str, str] = Field(default_factory=dict)
    llm_custom_keys: dict[str, str] = Field(default_factory=dict)
    llm_custom_models: dict[str, str] = Field(default_factory=dict)

    # 저장
    database_path: str = "./data/jobs.db"

    # 크롤링
    crawl_user_agent: str = "job-crawler-automation (contact: unset)"
    crawl_delay_seconds: float = 3.0
    crawl_timeout_seconds: float = 20.0
    crawl_max_retries: int = 3
    # 렌더 1회의 상한. 정적 타임아웃보다 길지만 무한정 기다리지 않는다
    render_timeout_seconds: float = 60.0

    # 화면
    # 저장은 UTC 그대로 두고, 화면에 그릴 때만 이 시간대로 옮긴다 (`app/api/ui.py`).
    # 제공 API 는 계약대로 UTC 다 — `.claude/docs/api-contract.md`
    display_timezone: str = "Asia/Seoul"

    # 운영 화면 잠금
    # 비밀번호 하나만 받는 자물쇠다. 계정도 권한도 만들지 않는다 (`app/api/auth.py`).
    # 기본값을 쓰고 있으면 화면과 기동 로그가 그렇다고 알린다 — 공개 주소에서 이 값은
    # 잠기지 않은 것과 같다
    admin_password: str = "1234"

    # 빌드가 심는 커밋 SHA. 이미지 태그를 고정하지 않으므로 떠 있는 코드를 아는 길이
    # 이 값뿐이다. 로컬에서 띄우면 비어 있다
    build_sha: str = "unknown"

    # 실행
    # 동시 실행 상한의 초기값. 한 번 app_settings 에 들어간 뒤로는 DB 값이 이긴다
    # (`app/settings.py`). 초기값으로 들어갈 값이라 여기서 범위를 지킨다
    max_concurrent_runs: int = Field(default=3, ge=1)
    # 워크플로우 첫 실행이 담을 항목 수의 초기 상한. 0 은 상한 없음이다. 등록하면 목록에
    # 걸린 과거 공고가 통째로 들어오는 것을 막는다 (`app/settings.py` 의 `FIRST_RUN_LIMIT`)
    first_run_limit: int = Field(default=20, ge=0)
    run_timeout_seconds: int = 600

    @field_validator(*_BLANK_FALLS_BACK, mode="before")
    @classmethod
    def _blank_is_not_a_value(cls, value: Any, info: ValidationInfo) -> Any:
        """빈 값은 "설정하지 않았다" 로 본다. 기본값으로 되돌린다."""
        if isinstance(value, str) and not value.strip():
            return cls.model_fields[str(info.field_name)].default
        return value

    @field_validator(*_KEYS, mode="before")
    @classmethod
    def _keys_lose_their_whitespace(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
