"""대시보드. 처음 들어오면 보이는 화면이다.

보여 주는 것: 오늘 숫자 넷(새 공고, 오공고 전송, 처리 대기, AI 비용), 최근 14일 추이, 확인이
필요한 것, 최근 전송한 공고, 워크플로우 상태, 이 프로세스의 최근 로그.

## 날짜

저장된 시각(UTC)을 표시 시간대의 날짜로 바꿔 센다. 새 공고는 `raw_jobs.crawled_at`, 오공고 전송은
`spring_deliveries.sent_at`, 비용은 `llm_calls.called_at` 이다. 셋 다 그 일이 일어난 시각이라 나중에
바뀌지 않는다. 예전의 '완성' 건수는 완성된 시각을 기록하지 않아 재분류할 때마다 그날로 몰려서
뺐다 (2026-09-15).

## 비용

호출 기록에 남은 모델마다 `app/llm/pricing.py` 의 단가로 계산한다. 단가를 모르는 모델의 호출은
비용에서 빠지고 그 호출 수를 따로 적는다.

## 로그는 이 프로세스가 방금 낸 것만이다

`app/log_ring.py` 의 메모리 버퍼를 그대로 읽는다. 재시작하면 비고, 컨테이너 로그를 대체하지 않는다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.api.settings import get_connection
from app.api.ui import display_zone, render, render_page
from app.classify.store import pending_count as classify_pending_count
from app.crawler.failures import SUCCESS
from app.crawler.runner import consecutive_failures
from app.deliver import spring
from app.llm import pricing
from app.log_ring import LogLine
from app.log_ring import handler as _log_handler

router = APIRouter(tags=["ui"], include_in_schema=False)

# 그래프에 보여줄 날짜 수. 너무 길면 막대가 가늘어져 못 읽고, 너무 짧으면 추이가 안 보인다
TREND_DAYS = 14

# 최근 전송한 공고 줄 수
RECENT_LIMIT = 5

# 연속 실패를 거슬러 세는 한도. 자동 중지 임계치가 없는 워크플로우도 이만큼만 본다
STREAK_LOOKBACK = 20

# 0이 아닌 막대의 최소 높이(%). 작은 값이 반올림으로 사라지지 않게 한다
_MIN_BAR_PCT = 4


def _offset_modifier() -> str:
    """`display_zone()` 의 지금 UTC 오프셋을 SQLite `date()` 보정 문자열로.

    시간 단위가 아닌 시간대는 없다고 본다 — 기본값 `Asia/Seoul` 은 정시 오프셋이다.
    """
    offset = datetime.now(display_zone()).utcoffset() or timedelta(0)
    hours = round(offset.total_seconds() / 3600)
    sign = "+" if hours >= 0 else "-"
    return f"{sign}{abs(hours)} hours"


def _day_range(days: int) -> list[date]:
    """오늘을 포함한 최근 `days`일. 오래된 날짜가 먼저다 — 그래프를 왼쪽부터 읽는다."""
    today = datetime.now(display_zone()).date()
    return [today - timedelta(days=i) for i in range(days - 1, -1, -1)]


def _now_text() -> str:
    """표시 시간대의 지금. 모집 일시와 같은 모양이라 마감 비교에 그대로 쓴다."""
    return datetime.now(display_zone()).strftime("%Y-%m-%d %H:%M:%S")


def _daily_added(conn: sqlite3.Connection, modifier: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT date(crawled_at, ?) AS day, count(*) AS n FROM raw_jobs GROUP BY day",
        (modifier,),
    ).fetchall()
    return {str(row["day"]): int(row["n"]) for row in rows}


def _daily_sent(conn: sqlite3.Connection, modifier: str) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT date(sent_at, ?) AS day, count(*) AS n
          FROM spring_deliveries
         WHERE status = 'sent' AND sent_at IS NOT NULL
         GROUP BY day
        """,
        (modifier,),
    ).fetchall()
    return {str(row["day"]): int(row["n"]) for row in rows}


@dataclass
class _DayCost:
    usd: float = 0.0
    calls: int = 0
    unpriced: int = 0


def _daily_cost(conn: sqlite3.Connection, modifier: str) -> dict[str, _DayCost]:
    """일별 예상 비용. 모델마다 단가가 달라 날짜·모델로 묶어 따로 곱한다."""
    result: dict[str, _DayCost] = {}
    rows = conn.execute(
        """
        SELECT date(called_at, ?) AS day, model, count(*) AS calls,
               coalesce(sum(input_tokens), 0)  AS in_tokens,
               coalesce(sum(output_tokens), 0) AS out_tokens
          FROM llm_calls
         GROUP BY day, model
        """,
        (modifier,),
    ).fetchall()
    for row in rows:
        day = result.setdefault(str(row["day"]), _DayCost())
        calls = int(row["calls"])
        day.calls += calls
        cost = pricing.cost_usd(
            str(row["model"] or ""), int(row["in_tokens"]), int(row["out_tokens"])
        )
        if cost is None:
            day.unpriced += calls
        else:
            day.usd += cost
    return result


def _bar_pct(value: float, peak: float) -> int:
    if value <= 0 or peak <= 0:
        return 0
    return max(round(value / peak * 100), _MIN_BAR_PCT)


@dataclass(frozen=True)
class TrendDay:
    """그래프 한 날짜. `*_pct` 는 서버가 미리 계산한 막대 높이(0~100)다."""

    label: str
    added: int
    sent: int
    cost_usd: float
    cost_krw: int
    calls: int
    unpriced: int
    added_pct: int
    sent_pct: int
    cost_pct: int


def trend(conn: sqlite3.Connection, days: int = TREND_DAYS) -> list[TrendDay]:
    modifier = _offset_modifier()
    added = _daily_added(conn, modifier)
    sent = _daily_sent(conn, modifier)
    costs = _daily_cost(conn, modifier)
    day_list = [d.isoformat() for d in _day_range(days)]
    peak = max(
        [added.get(key, 0) for key in day_list] + [sent.get(key, 0) for key in day_list] + [1]
    )
    cost_peak = max([costs[key].usd for key in day_list if key in costs] + [0.0])
    result: list[TrendDay] = []
    for key in day_list:
        cost = costs.get(key, _DayCost())
        result.append(
            TrendDay(
                label=f"{int(key[5:7])}/{int(key[8:10])}",
                added=added.get(key, 0),
                sent=sent.get(key, 0),
                cost_usd=cost.usd,
                cost_krw=round(cost.usd * pricing.KRW_PER_USD),
                calls=cost.calls,
                unpriced=cost.unpriced,
                added_pct=_bar_pct(added.get(key, 0), peak),
                sent_pct=_bar_pct(sent.get(key, 0), peak),
                cost_pct=_bar_pct(cost.usd, cost_peak),
            )
        )
    return result


@dataclass(frozen=True)
class Metrics:
    added_today: int
    added_yesterday: int
    sent_today: int
    sent_yesterday: int
    classify_waiting: int
    deliver_waiting: int
    cost_today_krw: int
    cost_today_usd: float
    calls_today: int
    unpriced_today: int


def metrics(conn: sqlite3.Connection, days: list[TrendDay], now: str) -> Metrics:
    today = days[-1] if days else None
    yesterday = days[-2] if len(days) >= 2 else None
    return Metrics(
        added_today=today.added if today else 0,
        added_yesterday=yesterday.added if yesterday else 0,
        sent_today=today.sent if today else 0,
        sent_yesterday=yesterday.sent if yesterday else 0,
        classify_waiting=classify_pending_count(conn),
        deliver_waiting=spring.pending_count(conn, now),
        cost_today_krw=today.cost_krw if today else 0,
        cost_today_usd=today.cost_usd if today else 0.0,
        calls_today=today.calls if today else 0,
        unpriced_today=today.unpriced if today else 0,
    )


@dataclass(frozen=True)
class Attention:
    """확인이 필요한 것 한 줄. `tone` 은 `chip-*` 색이다."""

    title: str
    badge: str
    tone: str
    detail: str


def attention(conn: sqlite3.Connection, now: str) -> list[Attention]:
    """연속 실패한 워크플로우, 오공고 전송 실패, 필수 칸이 빈 공고. 없는 것은 줄을 만들지 않는다."""
    items: list[Attention] = []
    for row in conn.execute(
        "SELECT id, name, status, auto_stop_threshold FROM workflows ORDER BY id"
    ):
        lookback = int(row["auto_stop_threshold"] or STREAK_LOOKBACK)
        streak = consecutive_failures(conn, int(row["id"]), max(lookback, 1))
        if not streak:
            continue
        last = conn.execute(
            """
            SELECT error_message FROM crawl_runs
             WHERE workflow_id = ? AND status IS NOT NULL AND status <> ?
               AND (trigger IS NULL OR trigger <> 'recollect')
             ORDER BY id DESC LIMIT 1
            """,
            (row["id"], SUCCESS),
        ).fetchone()
        detail = (
            str(last["error_message"])
            if last and last["error_message"]
            else "사유가 기록되지 않았다"
        )
        if row["status"] == "paused":
            detail = f"멈춤 · {detail}"
        items.append(Attention(f"{row['name']} 워크플로우", f"연속 실패 {streak}", "bad", detail))

    failed = conn.execute(
        """
        SELECT count(*) AS n,
               (SELECT last_error FROM spring_deliveries WHERE status = 'failed'
                 ORDER BY updated_at DESC LIMIT 1) AS last_error
          FROM spring_deliveries WHERE status = 'failed'
        """
    ).fetchone()
    if failed["n"]:
        items.append(
            Attention(
                "오공고 전송 실패", f"{failed['n']}건", "warn", str(failed["last_error"] or "")
            )
        )

    unready = spring.unready_count(conn, now)
    if unready:
        items.append(
            Attention(
                "필수 칸이 빈 공고",
                f"{unready}건",
                "idle",
                "분류는 끝났지만 오공고가 받는 칸이 비어 보내지 않았다",
            )
        )
    return items


def recent_sent(conn: sqlite3.Connection, limit: int = RECENT_LIMIT) -> list[sqlite3.Row]:
    """가장 최근에 오공고로 보낸 공고."""
    return conn.execute(
        """
        SELECT n.id, n.title, n.company_name, n.parent_company_name, d.sent_at
          FROM spring_deliveries d
          JOIN normalized_jobs n ON n.source_url = d.source_url
         WHERE d.status = 'sent' AND d.sent_at IS NOT NULL
         ORDER BY d.sent_at DESC, n.id DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()


@dataclass(frozen=True)
class WorkflowState:
    name: str
    word: str
    tone: str
    last_run_at: str


def workflow_states(conn: sqlite3.Connection) -> list[WorkflowState]:
    """워크플로우마다 마지막으로 끝난 주기 실행의 결과. 원문 다시 수집은 세지 않는다."""
    rows = conn.execute(
        """
        SELECT w.name, w.status, w.last_run_at,
               (SELECT r.status FROM crawl_runs r
                 WHERE r.workflow_id = w.id AND r.status IS NOT NULL
                   AND (r.trigger IS NULL OR r.trigger <> 'recollect')
                 ORDER BY r.id DESC LIMIT 1) AS last_status
          FROM workflows w
         ORDER BY w.id
        """
    ).fetchall()
    result: list[WorkflowState] = []
    for row in rows:
        if row["status"] == "paused":
            word, tone = "멈춤", "idle"
        elif row["last_status"] is None:
            word, tone = "기록 없음", "idle"
        elif row["last_status"] == SUCCESS:
            word, tone = "정상", "ok"
        else:
            word, tone = "실패", "bad"
        # 시각은 화면이 `as_time` 으로 그린다. 여기서 문자열로 바꾸면 표시 시간대 검사가 못 본다
        result.append(WorkflowState(str(row["name"]), word, tone, str(row["last_run_at"] or "")))
    return result


@router.get("/", response_class=HTMLResponse)
def dashboard_page(request: Request) -> HTMLResponse:
    return render_page(request, "pages/dashboard.html")


@router.get("/ui/dashboard", response_class=HTMLResponse)
def dashboard_summary_fragment(
    request: Request, conn: Annotated[sqlite3.Connection, Depends(get_connection)]
) -> HTMLResponse:
    days = trend(conn)
    now = _now_text()
    return render(
        request,
        "fragments/dashboard_summary.html",
        metrics=metrics(conn, days, now),
        trend_days=days,
        attention=attention(conn, now),
        recent=recent_sent(conn),
        workflows=workflow_states(conn),
    )


def _log_lines() -> list[LogLine]:
    """가장 최근 것이 먼저다. 3초마다 다시 그리는 조각이라, 새 줄이 스크롤 없이 늘
    같은 자리(맨 위)에서 보여야 훑어보기 편하다."""
    return list(reversed(_log_handler.tail()))


@router.get("/ui/dashboard/logs", response_class=HTMLResponse)
def dashboard_logs_fragment(request: Request) -> HTMLResponse:
    """최근 로그. DB 를 보지 않는다 — 이 프로세스 메모리의 버퍼 하나뿐이다."""
    return render(request, "fragments/dashboard_logs.html", lines=_log_lines())
