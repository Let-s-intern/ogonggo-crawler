"""실행 1회. `crawl_runs` 행 하나가 실행 하나다.

행은 시작할 때 만들고, 어떤 종료 경로에서도 종료 상태와 카운트로 갱신한다. 기록이 없는 실행은
아무도 디버깅하지 못한다 (`.claude/rules/crawling.md`).

흐름은 목록 파싱 → 신규 판정 → 신규 건만 상세 → `raw_jobs` append → 정규화다
(`.claude/docs/architecture.md` 실행 흐름).

마감이 지난 공고와 이미 아는 공고는 상세를 열지 않고 건너뛴다. 건너뛴 수는 `skipped_count` 로
따로 세고 `fail_count` 와 섞지 않는다 — 건너뜀은 정상이고 실패는 고칠 것이다.

정규화는 적재한 건에 대해서만 돌고, 실패해도 실행을 죽이지 않는다. 규칙이 틀렸다고 수집한
공고를 버리면 규칙을 고쳐도 되살릴 원본이 없다. 실패한 건은 `raw_jobs` 에 그대로 남고
`fail_count` 로 세어져, 규칙을 고친 뒤 재정규화로 복구된다.

신규 판정을 두 단계로 나눈 이유가 하나 있다. `content_hash` 는 상세에서 오는 `body` 와
`deadline` 까지 넣어 만드는데, 상세를 가져오기 전에는 그 값이 없다. 그래서 목록 단계에서는
`source_url` 로 아는 공고인지만 보고, 아는 공고면 상세를 가져오지 않는다. 상세까지 간 건에
대해서만 `content_hash` 를 만들어 마지막으로 한 번 더 확인한다.

본문을 얻지 못한 공고는 적재하지 않고 실패로 남긴다. 목록에서 읽은 값만 넣고 성공으로 넘기면
`body` 가 빈 행이 쌓이고, 그것을 소비 측이 본문 없는 공고로 받는다. 대신 어느 공고를 왜 놓쳤는지가
`crawl_run_failures` 에 제목과 함께 남는다.

`raw_jobs` 는 append-only 다. 기존 행을 갱신하지 않는다 (`.claude/rules/data-safety.md`).

워크플로우가 없는 테스트 실행은 `raw_jobs` 에 적재하지 않는다. 적재할 워크플로우가 없기
때문이고, 테스트 실행이 원하는 것은 화면에 보여줄 미리보기이지 수집 데이터가 아니다.
그 실행의 `new_count` 는 0 이다 — 적재한 것이 없다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass, field

from app.config import get_settings
from app.crawler.collect import API, Collectors, html_collectors, open_collectors
from app.crawler.deadline import is_closed
from app.crawler.failures import (
    FAILED,
    LIST_EMPTY,
    SUCCESS,
    ZERO_ITEM_MESSAGE,
    DetailEmptyError,
    DetailUnreachableError,
    Failure,
    classify,
    run_status,
)
from app.crawler.fetcher import FetchPolicy, PageSource, get_fetcher
from app.crawler.hashing import content_hash
from app.crawler.images import LlmImageReader
from app.crawler.parser import ListItem
from app.normalize.engine import NormalizeError, insert_normalized, load_rules
from app.normalize.rules import Rule
from app.notify.message import NewJob
from app.notify.new_jobs import notify_new_jobs
from app.selector.api_schema import ApiConfigError, parse_api_config
from app.selector.schema import (
    SPLIT_DETAIL_FIELDS,
    DetailSelectors,
    ListSelectors,
    SelectorSchemaError,
    SelectorSet,
    validate_selectors,
)
from app.settings import FIRST_RUN_LIMIT, read_int
from app.side.runner import trigger_after_crawl

logger = logging.getLogger(__name__)

# 항목 하나가 어떻게 끝났는가. 미리보기 표가 그대로 읽는다.
STORED = "stored"
KNOWN = "known"
PREVIEW = "preview"

# 실행을 무엇이 시작했는가. `crawl_runs.trigger` 에 그대로 들어간다
# (`migrations/0007_run_trigger.sql`).
# 최근 실행이 있어도 그것이 사람이 누른 것이면 주기는 죽어 있는 것이라, 이 셋이 갈리지 않으면
# "주기가 실제로 도는가" 에 답할 수 없다
SCHEDULE = "schedule"
MANUAL = "manual"
TEST = "test"


@dataclass(frozen=True)
class RunTarget:
    """무엇을 실행하는가. `workflow_id` 와 `crawler_id` 중 하나는 있어야 한다."""

    list_url: str
    selectors: SelectorSet
    # `SCHEDULE` / `MANUAL` / `TEST`. 기본값을 두지 않는다 — 부르는 쪽이 자기가 누구인지 안다
    trigger: str
    workflow_id: int | None = None
    crawler_id: int | None = None

    def __post_init__(self) -> None:
        if self.workflow_id is None and self.crawler_id is None:
            raise ValueError("workflow_id 나 crawler_id 중 하나는 있어야 한다")


@dataclass(frozen=True)
class ItemResult:
    """항목 하나의 결과. `fields` 는 셀렉터로 뽑은 값 그대로다."""

    source_url: str
    state: str
    fields: dict[str, str]
    # 적재한 건만 값이 있다. 정규화가 읽을 `raw_jobs` 행이다
    raw_job_id: int | None = None
    # 공고는 다뤘지만 실행 기록에 남길 일. 이미지를 읽지 못한 것이 그렇다
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ItemFailure:
    """항목 하나가 어떻게 실패했는가. `crawl_run_failures` 행 하나가 된다."""

    source_url: str
    error_class: str | None
    message: str
    # 목록에서 읽은 제목. 건수와 사유만으로는 어느 공고였는지 알 수 없어 고칠 수가 없다
    title: str = ""


@dataclass
class RunResult:
    """`crawl_runs` 행에 들어간 값 그대로 + 미리보기."""

    run_id: int
    status: str
    matched: int = 0
    success_count: int = 0
    new_count: int = 0
    fail_count: int = 0
    # 적재하지 않고 넘긴 수. 마감이 지났거나 이미 아는 공고다. 실패가 아니라서 `fail_count`
    # 와 따로 센다 — 합치면 마감 날짜 형식이 바뀌어 전부 걸러진 사이트가 "새 공고 0건" 인
    # 정상 실행으로 보인다
    skipped_count: int = 0
    error_class: str | None = None
    error_message: str = ""
    items: list[ItemResult] = field(default_factory=list)
    failures: list[ItemFailure] = field(default_factory=list)


class WorkflowMissingError(LookupError):
    """워크플로우 행이 없다. `crawl_runs` 행을 만들 수 없어 기록도 남지 않는다."""


async def run_workflow(
    conn: sqlite3.Connection,
    workflow_id: int,
    *,
    trigger: str = SCHEDULE,
    fetcher: FetchPolicy | None = None,
    limit: int | None = None,
    timeout_seconds: float | None = None,
) -> RunResult:
    """스케줄러가 부르는 진입점. 무엇을 실행할지는 매번 테이블에서 다시 읽는다.

    `workflows` 와 `crawlers` 가 진실이다. 잡을 등록할 때의 값을 스케줄러가 들고 있다가
    쓰지 않는다 (`.claude/rules/crawling.md`). 정적으로 가져올지 렌더할지도 매번
    `crawlers.list_mode` 를 다시 읽어서 정한다.

    실행은 `RUN_TIMEOUT_SECONDS` 로 감싼다. 끝나지 않는 실행 하나가 동시 실행 자리를 영원히
    붙들고 있으면 나머지 워크플로우가 전부 멈춘다.

    셀렉터가 없거나 스키마에 맞지 않으면 실행하지 못하지만, 그것도 종료 경로다.
    `crawl_runs` 행을 실패로 남긴다.

    `trigger` 는 이 실행을 무엇이 시작했는지다. 기본값이 `SCHEDULE` 인 것은 여기가 스케줄러의
    진입점이기 때문이고, 화면의 1회 실행은 `MANUAL` 을 직접 준다.
    """
    row = conn.execute(
        """
        SELECT c.list_url AS list_url, c.selectors_json AS selectors_json,
               c.list_mode AS list_mode, c.detail_mode AS detail_mode,
               c.api_config_json AS api_config_json
          FROM workflows w
          JOIN crawlers c ON c.id = w.crawler_id
         WHERE w.id = ?
        """,
        (workflow_id,),
    ).fetchone()
    if row is None:
        raise WorkflowMissingError(f"워크플로우 {workflow_id} 가 없다")

    list_mode, detail_mode = str(row["list_mode"]), str(row["detail_mode"])
    # 저장된 설정이 실행할 수 있는 상태가 아니면 실행하지 못하지만, 그것도 종료 경로다.
    # transport·selector_miss·parse 중 어느 것도 아니므로 error_class 는 비워 두고 사유만
    # 남긴다. 셀렉터와 API 설정을 갈라 적는다 — 고칠 자리가 서로 다르다
    try:
        selectors = collect_selectors(row["selectors_json"], list_mode, detail_mode)
    except (json.JSONDecodeError, SelectorSchemaError) as exc:
        result = _config_failure(conn, workflow_id, trigger, f"셀렉터를 읽을 수 없다: {exc}")
        _record_outcome(conn, workflow_id, result)
        return result
    try:
        api_config = parse_api_config(row["api_config_json"])
    except ApiConfigError as exc:
        result = _config_failure(conn, workflow_id, trigger, f"API 설정을 읽을 수 없다: {exc}")
        _record_outcome(conn, workflow_id, result)
        return result

    bound = timeout_seconds if timeout_seconds is not None else get_settings().run_timeout_seconds
    # 첫 실행만 항목 수를 막는다. 부르는 쪽이 값을 줬으면 그것이 이긴다 — 화면의 1회 실행과
    # 테스트가 자기 상한을 들고 온다
    if limit is None:
        limit = first_run_limit(conn, workflow_id)
    # 목록과 상세를 무엇으로 가져올지는 crawlers 의 두 열이 정한다. 브라우저가 필요한 쪽이
    # 있으면 이 블록에서만 산다 (`app/crawler/collect.py`)
    async with open_collectors(
        list_mode=list_mode,
        detail_mode=detail_mode,
        list_url=row["list_url"],
        selectors=selectors,
        fetcher=fetcher or get_fetcher(),
        api_config=api_config,
        # 쪽을 넘기는 목록에서 아는 공고만 있는 쪽을 만나면 거기서 멈춘다. 목록이 새것부터
        # 오므로 그 뒤는 더 옛것이다 (`app/crawler/api_source.py`)
        known=lambda link: _is_known(conn, workflow_id, "source_url", link),
        # 본문이 이미지로만 올라온 공고의 이미지를 수집할 때 한 번 읽는다 (`app/crawler/images.py`)
        image_reader=LlmImageReader(conn),
    ) as collectors:
        result = await run_once(
            conn,
            RunTarget(
                list_url=row["list_url"],
                selectors=selectors,
                trigger=trigger,
                workflow_id=workflow_id,
            ),
            collectors=collectors,
            limit=limit,
            timeout_seconds=bound,
        )
    _record_outcome(conn, workflow_id, result)
    # 새 공고가 들어왔으면 알린다. 보내기가 실패해도 실행 결과는 그대로다 —
    # 알림이 안 갔다고 수집이 실패한 것은 아니다 (`app/notify/new_jobs.py`)
    await notify_new_jobs(
        conn,
        workflow_id=workflow_id,
        new_count=result.new_count,
        jobs=[
            NewJob(
                company=item.fields["company"],
                title=item.fields["title"],
                # 알림에서 눌러 바로 열 자리다
                url=item.source_url,
            )
            for item in result.items
            if item.state == STORED
        ],
    )
    # 새 공고가 있을 때만 `after_crawl` 분류를 잇는다. 실패해도 이 실행은 그대로다 —
    # 알림과 같은 이유다 (`app/side/runner.py` 의 `trigger_after_crawl`)
    trigger_after_crawl(conn, new_count=result.new_count)
    return result


# 목록과 상세가 둘 다 API 면 셀렉터가 하나도 쓰이지 않는다. 그런 크롤러에 빈 셀렉터를
# 요구하면, 아무도 읽지 않는 값이 없다는 이유로 실행이 멈춘다
_EMPTY_SELECTORS = SelectorSet(
    list=ListSelectors(item="", title="", link="", date=""),
    detail=DetailSelectors(title="", body="", requirements="", deadline="", department=""),
)


def collect_selectors(selectors_json: str | None, list_mode: str, detail_mode: str) -> SelectorSet:
    """저장된 셀렉터를 읽는다. 양쪽 다 API 인 크롤러는 셀렉터 없이도 실행한다."""
    if list_mode == API and detail_mode == API:
        return _EMPTY_SELECTORS
    return validate_selectors(json.loads(selectors_json or "null"))


def _record_outcome(conn: sqlite3.Connection, workflow_id: int, result: RunResult) -> None:
    """실행 결과를 `workflows` 에 반영한다. 연속 실패가 임계치에 닿으면 자동으로 멈춘다.

    `success_count` 와 `fail_count` 는 실행 횟수다. 항목 수는 `crawl_runs` 가 이미 들고 있고,
    화면 배지가 물어보는 것은 "이 워크플로우가 몇 번 실패했나" 이기 때문이다.

    성공이 아닌 것은 전부 실패로 센다. `timeout` 도 마찬가지다 — 끝나지 못한 실행을 성공으로
    세면 자동 중지가 영원히 걸리지 않는다.

    연속 실패 횟수는 따로 저장하지 않고 `crawl_runs` 에서 센다. 세는 곳과 기록하는 곳이 갈리면
    둘이 어긋나고, 어긋난 쪽을 믿을 근거가 없다.
    """
    succeeded = result.status == SUCCESS
    conn.execute(
        """
        UPDATE workflows
           SET success_count = success_count + ?,
               fail_count = fail_count + ?,
               last_run_at = datetime('now')
         WHERE id = ?
        """,
        (1 if succeeded else 0, 0 if succeeded else 1, workflow_id),
    )
    if succeeded:
        return

    row = conn.execute(
        "SELECT status, auto_stop_threshold FROM workflows WHERE id = ?", (workflow_id,)
    ).fetchone()
    threshold = row["auto_stop_threshold"] if row is not None else None
    # NULL 이면 자동 중지하지 않는다. 이미 멈춘 것을 다시 멈추지도 않는다
    if threshold is None or row["status"] != "active":
        return

    streak = consecutive_failures(conn, workflow_id, int(threshold))
    if streak < threshold:
        return

    conn.execute("UPDATE workflows SET status = 'paused' WHERE id = ?", (workflow_id,))
    logger.warning(
        "workflow %s: 연속 %s회 실패로 자동 중지한다 (임계치 %s)", workflow_id, streak, threshold
    )


def first_run_limit(conn: sqlite3.Connection, workflow_id: int) -> int | None:
    """이 실행이 첫 실행이면 담을 항목 수의 상한. 아니면 `None` 이다.

    등록하면 목록에 걸린 과거 공고가 통째로 들어온다. 2026-08-26 에 열두 곳을 등록하며 670건이
    그렇게 들어왔고 그 뒤 실행은 신규 0~1건이다. 저장량과 분류 토큰이 튀는 자리가 평소 수집이
    아니라 이 첫 실행이라, 막을 곳도 여기 하나다.

    **두 번째 실행부터는 걸지 않는다.** 상한이 계속 있으면 목록이 상한보다 길게 밀린 날에
    뒤쪽 공고를 영영 못 본다 — 목록에서 밀려난 공고는 다시 올라오지 않는다.

    실패한 실행도 첫 실행으로 세지 않는다. 셀렉터가 틀려 0건으로 끝난 뒤 고쳐서 다시 도는
    것이 흔한데, 그때 상한이 풀려 있으면 백필을 막으려던 것이 그대로 새어 나간다.
    """
    limit = read_int(conn, FIRST_RUN_LIMIT)
    if limit <= 0:
        return None

    row = conn.execute(
        "SELECT 1 FROM crawl_runs WHERE workflow_id = ? AND new_count > 0 LIMIT 1",
        (workflow_id,),
    ).fetchone()
    return None if row is not None else limit


def consecutive_failures(conn: sqlite3.Connection, workflow_id: int, limit: int) -> int:
    """마지막 실행부터 거슬러 올라가며 성공이 나올 때까지 센다.

    자동 중지 판정과 화면의 임계치 표시가 같은 값을 봐야 해서 공개해 둔다. 세는 곳이 둘이면
    화면이 말하는 연속 실패와 실제로 중지되는 시점이 어긋난다.

    아직 끝나지 않은 실행(`status` 가 NULL)은 성공도 실패도 아니라 세지 않는다.
    """
    rows = conn.execute(
        """
        SELECT status FROM crawl_runs
         WHERE workflow_id = ? AND status IS NOT NULL
         ORDER BY id DESC LIMIT ?
        """,
        (workflow_id, limit),
    ).fetchall()

    streak = 0
    for row in rows:
        if row["status"] == SUCCESS:
            break
        streak += 1
    return streak


def _config_failure(
    conn: sqlite3.Connection, workflow_id: int, trigger: str, message: str
) -> RunResult:
    cursor = conn.execute(
        "INSERT INTO crawl_runs (workflow_id, trigger) VALUES (?, ?)",
        (workflow_id, trigger),
    )
    result = RunResult(run_id=int(cursor.lastrowid or 0), status="")
    _finish_run(conn, result, Failure(error_class=None, message=message))
    return result


async def run_once(
    conn: sqlite3.Connection,
    target: RunTarget,
    *,
    fetcher: PageSource | None = None,
    collectors: Collectors | None = None,
    limit: int | None = None,
    timeout_seconds: float | None = None,
) -> RunResult:
    """1회 실행. 예외를 밖으로 던지지 않고 실패한 `RunResult` 로 돌려준다.

    무엇으로 가져올지는 `collectors` 가 들고 있다. 주지 않으면 `fetcher` 로 정적 HTML 을
    가져오는 수집기를 만든다 — 모드를 고를 것이 없는 호출의 지름길이다.

    `timeout_seconds` 가 있으면 그 시간을 넘긴 실행은 중단되고 `status=timeout` 으로 남는다.
    None 이면 시간 제한을 걸지 않는다 — 항목 수를 정해 놓고 도는 테스트 실행이 그렇다.

    시간 제한에 걸려도 그때까지 적재한 `raw_jobs` 는 지우지 않는다. append-only 라 되돌리지
    않고, 다음 실행이 같은 공고를 다시 넣지도 않는다 (`.claude/rules/data-safety.md`).
    """
    active = collectors or html_collectors(
        fetcher or get_fetcher(), target.list_url, target.selectors
    )
    run_id = _start_run(conn, target)
    result = RunResult(run_id=run_id, status="")
    failure: Failure | None = None
    timed_out = False

    try:
        # asyncio.timeout 은 안쪽의 취소를 경계에서 TimeoutError 로 바꿔 준다. 그래서 아래
        # BaseException 절(밖에서 온 취소)과 시간 제한이 섞이지 않는다
        async with asyncio.timeout(timeout_seconds) as bound:
            await _crawl(conn, target, active, limit, result)
    except TimeoutError as exc:
        # 제한을 넘겨서 끊긴 것인지, 안쪽에서 올라온 TimeoutError 인지 구분한다.
        # 후자를 timeout 으로 적으면 사이트 문제를 실행 시간 문제로 잘못 읽게 된다
        if not bound.expired():
            failure = classify(exc)
        else:
            timed_out = True
            failure = Failure(
                error_class=None,
                message=f"실행이 시간 제한 {timeout_seconds}초를 넘겨 중단됐다",
            )
    except Exception as exc:
        failure = classify(exc)
    except BaseException as exc:
        # 취소·강제 종료도 종료 경로다. 행을 남기고 나서 그대로 올려보낸다.
        _finish_run(conn, result, classify(exc))
        raise

    _finish_run(conn, result, failure, timed_out=timed_out)
    return result


async def _crawl(
    conn: sqlite3.Connection,
    target: RunTarget,
    collectors: Collectors,
    limit: int | None,
    result: RunResult,
) -> None:
    rules, rules_error = _load_rules(conn)
    parsed = await collectors.list.collect()

    result.matched = parsed.matched
    for miss in parsed.failures:
        result.fail_count += 1
        result.failures.append(
            ItemFailure(
                source_url=f"{target.list_url}#{miss.index}",
                error_class="parse",
                message=f"list.{miss.field}: {miss.message}",
            )
        )

    # 목록에서 읽은 날짜를 마감일로 볼 수 있는 크롤러인지는 수집기가 들고 있다. 항목마다
    # 다시 볼 값이 아니라 이 크롤러의 설정이다 (`app/crawler/collect.py`)
    for item in parsed.items[:limit]:
        if collectors.list_date_is_deadline and is_closed(item.date, rules):
            # 마감이 지난 공고다. 상세를 열지 않고 넘긴다 — 실패가 아니라 건너뜀이다.
            # 읽지 못한 날짜는 진행 중으로 본다 (`app/crawler/deadline.py`)
            result.skipped_count += 1
            continue

        try:
            collected = await _collect(conn, target, item, collectors)
        except Exception as exc:
            # 항목 하나가 실패해도 나머지는 계속 간다. 실패는 fail_count 로 남는다.
            classified = classify(exc)
            result.fail_count += 1
            result.failures.append(
                ItemFailure(
                    source_url=item.link,
                    error_class=classified.error_class,
                    message=classified.message,
                    title=item.title,
                )
            )
            continue

        result.items.append(collected)
        result.success_count += 1
        for note in collected.notes:
            # 공고는 다뤘지만 남길 일이 있다(이미지를 읽지 못했다). 실패로 세지 않는다 —
            # 사유 칸을 비워 여섯 실패와 섞이지 않게 한다
            result.failures.append(
                ItemFailure(source_url=item.link, error_class=None, message=note, title=item.title)
            )
        if collected.state == KNOWN:
            # 이미 아는 공고라 적재하지 않았다. 마감으로 넘긴 것과 같은 자리에 센다
            result.skipped_count += 1
        elif collected.state == STORED:
            result.new_count += 1
            _normalize(conn, collected, rules, rules_error, result)


async def _collect(
    conn: sqlite3.Connection, target: RunTarget, item: ListItem, collectors: Collectors
) -> ItemResult:
    """항목 하나를 처리한다. 아는 공고면 상세를 가져오지 않는다.

    본문을 얻지 못한 공고는 적재하지 않고 실패로 낸다. 목록에서 읽은 값만 넣고 성공으로
    넘기면 `body` 가 빈 행이 쌓이고, 소비 측은 그것을 본문이 없는 공고로 받는다
    (`.claude/tasks/done/fill-body/prd-fill-body.md`).

    실패는 둘로 갈린다. 상세로 갈 길이 없는 것은 `detail_unreachable` 이고 상세를 열었는데
    읽을 것이 없는 것은 `detail_empty` 다 — 앞은 경로를 다시 찾아야 하고 뒤는 본문 셀렉터만
    고치면 된다. 어느 공고였는지는 부르는 쪽이 목록에서 읽은 제목으로 적는다.
    """
    if item.detail_absent:
        # 상세로 갈 길이 없는 사이트다. 목록에는 본문이 없으므로 이 공고는 적재할 수 없다.
        raise DetailUnreachableError(
            "상세로 갈 길이 없어 본문을 얻지 못했다. `list.link` 나 `list.link_template`, "
            "상세 API 중 하나로 상세에 닿는 길을 등록해야 한다"
        )

    if _is_known(conn, target.workflow_id, "source_url", item.link):
        return ItemResult(source_url=item.link, state=KNOWN, fields={})

    detail = await collectors.detail.collect(item)
    record = _record(item, detail.fields, detail.source_text)
    if not record["body"].strip():
        # 상세는 열렸는데 본문이 없다. 나머지 필드가 채워져 있어도 적재하지 않는다.
        raise DetailEmptyError("상세를 열었지만 본문이 비었다. 상세의 본문 셀렉터를 고친다")
    digest = content_hash(record)

    if target.workflow_id is None:
        # 테스트 실행. 미리보기만 돌려주고 적재하지 않는다.
        return ItemResult(source_url=item.link, state=PREVIEW, fields=record, notes=detail.notes)

    if _is_known(conn, target.workflow_id, "content_hash", digest):
        return ItemResult(source_url=item.link, state=KNOWN, fields=record, notes=detail.notes)

    cursor = conn.execute(
        """
        INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash)
        VALUES (?, ?, ?, ?)
        """,
        (
            target.workflow_id,
            item.link,
            json.dumps(record, ensure_ascii=False),
            digest,
        ),
    )
    return ItemResult(
        source_url=item.link,
        state=STORED,
        fields=record,
        raw_job_id=int(cursor.lastrowid or 0),
        notes=detail.notes,
    )


def _load_rules(conn: sqlite3.Connection) -> tuple[list[Rule], str | None]:
    """정규화 규칙을 실행 시작에 한 번 읽는다. 못 읽어도 크롤링은 계속한다.

    실행 중간에 규칙이 바뀌어도 한 실행 안에서는 같은 규칙이 적용되게 하려고 한 번만 읽는다.

    저장된 설정이 깨져 있으면 이 실행의 정규화는 전부 실패하지만, 수집은 그대로 진행한다.
    수집을 멈추면 규칙을 고쳐도 그 사이의 공고는 사라지고 없다.
    """
    try:
        return load_rules(conn), None
    except NormalizeError as exc:
        logger.warning("정규화 규칙을 읽지 못했다. 이 실행은 적재만 한다: %s", exc)
        return [], str(exc)


def _normalize(
    conn: sqlite3.Connection,
    item: ItemResult,
    rules: list[Rule],
    rules_error: str | None,
    result: RunResult,
) -> None:
    """적재한 건 하나를 정규화한다. 실패는 그 건에서 끝나고 raw 는 남는다."""
    if item.raw_job_id is None:
        return

    message = rules_error
    if message is None:
        try:
            insert_normalized(conn, item.raw_job_id, rules)
            return
        except NormalizeError as exc:
            message = str(exc)

    # transport·selector_miss·parse 중 어느 것도 아니다. 분류를 비우고 사유만 남긴다
    # (`app/crawler/failures.py`).
    result.fail_count += 1
    result.failures.append(
        ItemFailure(source_url=item.source_url, error_class=None, message=message)
    )


def _record(item: ListItem, detail: dict[str, str], source_text: str = "") -> dict[str, str]:
    """`raw_jobs.raw_data_json` 에 그대로 들어가는 값. 정제하지 않는다.

    `company` 는 상세에서 뽑은 값을 먼저 쓰고, 없으면 목록에서 뽑은 값을 쓴다. 상세가 그
    공고 한 건만 다루는 페이지라 계열사가 섞인 사이트에서 더 정확하다. 둘 다 없으면 빈
    문자열이고, 그 자리를 무엇으로 채울지는 정규화 단계가 정한다.

    운영자가 적어 둔 `crawlers.default_company` 는 여기 들어오지 않는다. 추출한 것만 담는
    테이블이다 (`.claude/rules/data-safety.md`).

    상세가 없는 사이트에서는 `title` 과 `deadline` 이 목록에서 온다. 상세를 따라가지 않으니
    그쪽에서 올 값이 없고, 목록에 있는 것을 두고 빈 칸으로 남기면 공고를 알아볼 수 없다.

    상세가 비운 칸은 `item.extra` 가 채운다. 목록 응답이 상세 칸의 값까지 들고 있는 사이트가
    있어서다 — 카카오 목록 API 는 직군·근무지·모집인원·주요 업무·전형 절차를 항목마다 담아
    주는데 상세 문서에는 그것들이 한 덩어리로만 있다. 읽지 않으면 그 값들은 여기서 사라지고,
    매핑하지 않은 값은 저장되지 않으므로 다시 얻을 길이 없다
    (`.claude/tasks/memos/보류/split-body/prd-split-body.md`).

    **순서가 규칙이다.** 상세에서 읽은 값이 늘 이긴다. 목록 값은 상세가 비었을 때만 쓰이고,
    목록도 그 값을 안 주면 빈 칸이다. 빈 칸을 채우려고 뜻이 다른 값을 옮겨 오지 않는다.

    `source_text` 는 있을 때만 넣는다. 못 뽑은 건은 이 키가 없는 채로, 원문을 뽑기 전과
    똑같은 모양으로 적재된다 — 원문이 없다고 공고를 버리지 않는다. 이 값은
    `content_hash` 에 들어가지 않는다 (`app/crawler/hashing.py`).
    """
    carried = item.extra
    record = {
        "source_url": item.link,
        "title": detail["title"] or item.title,
        "body": detail["body"] or carried.get("body", ""),
        "requirements": detail["requirements"] or carried.get("requirements", ""),
        "deadline": detail["deadline"] or carried.get("deadline", "") or item.date,
        "department": detail["department"] or carried.get("department", ""),
        "company": detail["company"] or item.company,
        **{name: detail.get(name, "") or carried.get(name, "") for name in SPLIT_DETAIL_FIELDS},
        "list_title": item.title,
        "list_date": item.date,
    }
    if source_text.strip():
        record["source_text"] = source_text
    return record


def _is_known(conn: sqlite3.Connection, workflow_id: int | None, column: str, value: str) -> bool:
    """이미 적재한 공고인지 본다. 적재하지 않는 실행에는 아는 공고가 없다."""
    if workflow_id is None:
        return False
    # column 은 이 모듈 안에서 넘기는 고정 값 둘뿐이다. 밖에서 오는 값이 들어오지 않는다.
    row = conn.execute(
        f"SELECT 1 FROM raw_jobs WHERE workflow_id = ? AND {column} = ? LIMIT 1",
        (workflow_id, value),
    ).fetchone()
    return row is not None


def close_orphan_runs(conn: sqlite3.Connection) -> int:
    """프로세스가 죽으며 남긴 미완 실행을 닫는다. 기동 시 한 번 부른다.

    실행 중에 프로세스가 사라지면 `crawl_runs` 행이 종료 상태 없이 남는다. 밖에서 온 취소는
    코드가 받아 적지만, SIGKILL 이나 컨테이너 재시작은 받을 기회조차 주지 않는다.

    행이 영원히 미완으로 남으면 누적 카운트가 그 실행을 세지 못하고, 화면은 끝나지 않는
    실행을 계속 진행 중으로 읽는다. `.claude/rules/crawling.md` 는 어떤 종료 경로에서도
    행이 기록되기를 요구한다 — 여기가 마지막 경로다.

    `timeout` 으로 적는다. 얼마나 돌았는지 모르는 채 끝난 실행이고, 성공이 아닌 것은 실패로
    센다는 규칙과 어긋나지 않는다. 그때까지 적재한 `raw_jobs` 는 건드리지 않는다 — append-only 다.
    """
    try:
        cursor = conn.execute(
            """
            UPDATE crawl_runs
               SET status = 'timeout',
                   finished_at = datetime('now'),
                   error_message = '프로세스가 끝나기 전에 사라져 결과를 남기지 못했다'
             WHERE status IS NULL
            """
        )
    except sqlite3.OperationalError:
        # 스키마가 아직 없는 DB 다. 정리할 것도 없다.
        #
        # 이 함수는 기동 시 뒷정리이지 기동 조건이 아니다. 여기서 예외가 올라가면 앱이
        # 아예 뜨지 않는데, 정작 못 한 일은 지난 실행 행 몇 개를 닫는 것뿐이다.
        # 마이그레이션은 컨테이너가 uvicorn 앞에서 따로 돌린다 (`Dockerfile` 의 CMD).
        return 0
    return int(cursor.rowcount or 0)


def _start_run(conn: sqlite3.Connection, target: RunTarget) -> int:
    # started_at 은 테이블 기본값(datetime('now'))이 채운다. 다른 테이블과 같은 형식이어야 한다.
    cursor = conn.execute(
        "INSERT INTO crawl_runs (workflow_id, crawler_id, trigger) VALUES (?, ?, ?)",
        (target.workflow_id, target.crawler_id, target.trigger),
    )
    return int(cursor.lastrowid or 0)


def _finish_run(
    conn: sqlite3.Connection,
    result: RunResult,
    failure: Failure | None,
    *,
    timed_out: bool = False,
) -> None:
    """종료 상태와 카운트를 확정하고 놓친 공고를 남긴다. 정상 파싱 0건은 실패다.

    쓸 항목이 하나도 없이 끝난 실행은 `list_empty` 다. 사유 없이 건수만 남기면 목록을 못 읽은
    실행과 원인을 모르는 실행이 같은 행으로 보인다.

    실행 기록과 실패 목록은 한 트랜잭션으로 쓴다. 갈라지면 `fail_count` 는 3인데 어느 공고였는지
    아무 데도 없는 행이 남고, 그 실행은 건수만 알고 고칠 수는 없는 기록이 된다.
    """
    result.status = run_status(
        result.success_count, failure, timed_out=timed_out, skipped_count=result.skipped_count
    )
    if failure is not None:
        result.error_class = failure.error_class
        result.error_message = failure.message
    elif result.status == FAILED:
        # 실행 전체는 예외 없이 끝났는데 남은 항목이 0건인 경우다. 항목별 실패가 있으면 그
        # 분류를 그대로 쓴다 — 놓친 이유를 이미 알고 있으므로 추측할 것이 없다.
        first = result.failures[0] if result.failures else None
        if first is not None:
            result.error_class = first.error_class
            result.error_message = f"{ZERO_ITEM_MESSAGE}: {first.message}"
        else:
            # 항목별 실패조차 없다. 목록이 쓸 항목을 하나도 내놓지 않은 것이라 고칠 자리는
            # 목록 셀렉터나 목록을 얻는 방식이다 (`app/crawler/failures.py`).
            result.error_class = LIST_EMPTY
            result.error_message = ZERO_ITEM_MESSAGE

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            UPDATE crawl_runs
               SET finished_at = datetime('now'), status = ?, success_count = ?, new_count = ?,
                   fail_count = ?, skipped_count = ?, error_class = ?, error_message = ?
             WHERE id = ?
            """,
            (
                result.status,
                result.success_count,
                result.new_count,
                result.fail_count,
                result.skipped_count,
                result.error_class,
                result.error_message or None,
                result.run_id,
            ),
        )
        conn.executemany(
            """
            INSERT INTO crawl_run_failures (run_id, reason, title, source_url, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (result.run_id, item.error_class, item.title, item.source_url, item.message)
                for item in result.failures
            ],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    logger.info(
        "run %s %s: matched=%s success=%s new=%s fail=%s skipped=%s error_class=%s",
        result.run_id,
        result.status,
        result.matched,
        result.success_count,
        result.new_count,
        result.fail_count,
        result.skipped_count,
        result.error_class,
    )
