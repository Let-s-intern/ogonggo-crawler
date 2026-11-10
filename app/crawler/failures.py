"""실행 실패를 `crawl_runs.error_class` 로 옮기는 한 곳.

여러 값으로 나뉘어 있는 이유는 조치가 각각 다르기 때문이다 (`.claude/rules/crawling.md`).

| error_class | 무슨 일이 있었나 | 조치 |
|---|---|---|
| `transport` | 타임아웃, 5xx, 연결 끊김 | 백오프 재시도. 반복되면 사이트 상태 확인 |
| `selector_miss` | 가져왔는데 item 이 0개 매칭 | 재시도 금지. 셀렉터 재작성 |
| `parse` | 매칭은 됐는데 필드를 못 읽었다 | 그 필드 셀렉터만 보정 |
| `list_empty` | 목록에서 반복 항목을 못 잡았다 | 목록 셀렉터나 목록을 얻는 방식을 고친다 |
| `detail_unreachable` | 링크·속성·클릭 어느 것으로도 상세에 못 갔다 | 상세 경로를 다시 찾는다 |
| `detail_empty` | 상세에 갔는데 본문이 비었다 | 본문 셀렉터만 보정 |

뒤의 셋은 공고 하나가 상세에 도달하지 못한 경우를 가른다. 셋을 하나로 합치면 "목록을 못 읽었다"
와 "본문 셀렉터가 틀렸다" 가 같은 값으로 남아 조치가 갈리지 않는다. 이 셋에 걸린 공고는
`raw_jobs` 에 넣지 않고 `crawl_run_failures` 에 제목과 목록에서 읽은 주소로 남긴다
(`migrations/0010_run_failures.sql`).

실행 전체가 `RUN_TIMEOUT_SECONDS` 를 넘긴 경우는 종료 상태 `timeout` 으로 따로 남긴다.
느린 사이트일 수도, 목록이 갑자기 길어진 것일 수도 있어 어느 하나로 단정하지 않는다 —
`error_class` 는 비우고 사유만 적는다.

분류를 모르는 예외는 `parse` 로 밀어 넣지 않는다. `error_class` 를 NULL 로 두고 예외 이름을
`error_message` 에 남긴다 — 모르는 실패를 아는 실패로 위장하면 그 사이트를 계속 잘못 고치게 된다.

성공 판정도 여기 있다. **정상 파싱 0건은 실패다.** 신규 0건인 정상 실행과 같은 결과로 남기면
마크업이 바뀐 사이트와 새 공고가 없는 사이트를 구분할 수 없다 (`CLAUDE.md`). 하나만 예외다 —
페이지가 직접 공고가 없다고 말하면 `공고 없음` 인 성공이다 (`app/crawler/empty_list.py`).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.crawler.fetcher import FetchError
from app.crawler.parser import CrawlDataError

# `crawl_runs.error_class` 와 `crawl_run_failures.reason` 의 CHECK 제약과 같은 값이어야 한다.
ERROR_CLASSES: tuple[str, ...] = (
    "transport",
    "selector_miss",
    "parse",
    "list_empty",
    "detail_unreachable",
    "detail_empty",
)

SUCCESS = "success"
FAILED = "failed"
TIMEOUT = "timeout"

# 목록이 쓸 항목을 하나도 내놓지 않은 실행의 사유. 예외로 올라오는 실패가 아니라 실행이 끝난
# 뒤에 판정하는 것이라 예외 클래스가 없다
LIST_EMPTY = "list_empty"

# 목록 0건이지만 페이지가 공고가 없다고 말한 실행의 사유. 실패가 아니라 성공이다
# (`app/crawler/empty_list.py`)
NO_POSTINGS_MESSAGE = "목록에 공고가 없다고 나온다"

ZERO_ITEM_MESSAGE = "정상 파싱된 항목이 0건이다. 신규 0건인 정상 실행이 아니라 실패다"


class DetailUnreachableError(CrawlDataError):
    """링크·속성·클릭 어느 것으로도 상세에 못 갔다. 본문이 올 곳이 없다."""

    error_class = "detail_unreachable"


class DetailEmptyError(CrawlDataError):
    """상세는 열렸는데 본문이 비었다. 그 공고는 `raw_jobs` 에 넣지 않는다."""

    error_class = "detail_empty"


@dataclass(frozen=True)
class Failure:
    """`crawl_runs` 의 `error_class`, `error_message` 한 쌍."""

    error_class: str | None
    message: str


def classify(exc: BaseException) -> Failure:
    """예외를 `error_class` 로 옮긴다. 모르는 예외는 `error_class` 없이 남긴다."""
    if isinstance(exc, FetchError | CrawlDataError):
        return Failure(error_class=exc.error_class, message=str(exc))
    return Failure(error_class=None, message=f"분류되지 않은 실패({type(exc).__name__}): {exc}")


def run_status(
    success_count: int,
    failure: Failure | None = None,
    timed_out: bool = False,
    skipped_count: int = 0,
) -> str:
    """실행 하나의 종료 상태. 실패가 있거나 처리한 항목이 0건이면 `failed` 다.

    시간 제한에 걸린 실행은 `failed` 가 아니라 `timeout` 이다. 둘을 합치면 "셀렉터가 깨졌다" 와
    "사이트가 느리다" 가 같은 값으로 남아 조치가 갈리지 않는다.

    건너뛴 항목도 처리한 항목이다. 목록을 정상으로 읽었고 마감됐거나 이미 아는 공고여서 상세를
    열지 않았을 뿐이라, 전부 건너뛴 실행은 실패가 아니다. 그것까지 실패로 세면 마감이 지난
    공고만 남은 사이트가 매번 실패로 남고 자동 중지에 걸린다.
    """
    if timed_out:
        return TIMEOUT
    if failure is not None or (success_count == 0 and skipped_count == 0):
        return FAILED
    return SUCCESS
