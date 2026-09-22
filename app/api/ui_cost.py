"""비용 화면. AI 호출 기록(`llm_calls`)으로 기간별 비용을 기능·모델·날짜로 나눠 본다.

2026-09-17 결정(LC-3344). 대시보드의 14일 추이는 비용이 새 공고·전송 숫자와 한 그림에 섞여 얼마가
어디에 나가는지 읽히지 않았다. 비용만 따로 떼어 위 메뉴 하나로 뒀다.

단가는 코드 표를 기본으로 하고 화면에서 넣은 값이 이긴다 (`app/llm/pricing.py`). 단가를 모르는
모델의 호출은 비용에 넣지 않고 따로 알린다 — 0원으로 더하면 비용이 적게 보인다.

사이트별 비용은 없다. 호출 기록에 어느 사이트·공고의 호출인지가 남지 않는다.
"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.api import crawlers
from app.api.ui import display_zone, render, render_page
from app.llm import pricing

router = APIRouter(tags=["ui"], include_in_schema=False)

PERIOD_TODAY = "today"
PERIOD_WEEK = "7d"
PERIOD_MONTH = "month"
PERIOD_LAST_MONTH = "last_month"
PERIODS: dict[str, str] = {
    PERIOD_TODAY: "오늘",
    PERIOD_WEEK: "최근 7일",
    PERIOD_MONTH: "이번 달",
    PERIOD_LAST_MONTH: "지난 달",
}

# 기능 이름을 화면 묶음으로. 셀렉터 생성과 수정은 운영자에게 같은 일이다
FEATURE_GROUPS: dict[str, str] = {
    "classify": "공고 분류",
    "selector_generate": "셀렉터 생성·수정",
    "selector_repair": "셀렉터 생성·수정",
    "image_read": "이미지 읽기",
    "bootcamp_fill": "부트캠프 정리",
}
# 막대 색. 묶음 이름 순서대로 쓴다
GROUP_COLORS: dict[str, str] = {
    "공고 분류": "bg-violet-500",
    "셀렉터 생성·수정": "bg-sky-500",
    "이미지 읽기": "bg-amber-400",
    "부트캠프 정리": "bg-emerald-500",
}
OTHER_COLOR = "bg-slate-400"


def _utc(moment: date) -> str:
    """표시 시간대 그날 0시를 UTC 문자열로. 호출 시각은 UTC 로 저장돼 있다."""
    local = datetime(moment.year, moment.month, moment.day, tzinfo=display_zone())
    return local.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def bounds(period: str, today: date) -> tuple[date, date]:
    """기간의 첫날과 끝 다음 날(표시 시간대)."""
    if period == PERIOD_TODAY:
        return today, today + timedelta(days=1)
    if period == PERIOD_WEEK:
        return today - timedelta(days=6), today + timedelta(days=1)
    first = today.replace(day=1)
    if period == PERIOD_LAST_MONTH:
        last_first = (first - timedelta(days=1)).replace(day=1)
        return last_first, first
    return first, today + timedelta(days=1)


@dataclass
class Bucket:
    calls: int = 0
    failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_total: int = 0
    usd: float = 0.0
    unpriced: int = 0

    @property
    def krw(self) -> int:
        return round(self.usd * pricing.KRW_PER_USD)

    @property
    def latency_seconds(self) -> float:
        return round(self.latency_total / self.calls / 1000, 1) if self.calls else 0.0


@dataclass
class Day:
    label: str
    groups: dict[str, float] = field(default_factory=dict)

    @property
    def usd(self) -> float:
        return sum(self.groups.values())


@dataclass
class Report:
    period: str
    total: Bucket
    previous_usd: float
    today: Bucket
    classified: int
    forecast_krw: int | None
    days: list[Day]
    peak_usd: float
    features: list[tuple[str, Bucket]]
    models: list[tuple[str, Bucket, bool]]
    unpriced_models: list[tuple[str, int]]

    @property
    def change_pct(self) -> int | None:
        if self.previous_usd <= 0:
            return None
        return round((self.total.usd - self.previous_usd) / self.previous_usd * 100)

    @property
    def per_job_krw(self) -> float | None:
        if not self.classified:
            return None
        return round(self.total.usd * pricing.KRW_PER_USD / self.classified, 1)


def _add(bucket: Bucket, row: sqlite3.Row, cost: float | None) -> None:
    calls = int(row["calls"])
    bucket.calls += calls
    bucket.failed += int(row["failed"] or 0)
    bucket.input_tokens += int(row["in_tokens"] or 0)
    bucket.output_tokens += int(row["out_tokens"] or 0)
    bucket.latency_total += int(row["latency"] or 0)
    if cost is None:
        bucket.unpriced += calls
    else:
        bucket.usd += cost


def _rows(conn: sqlite3.Connection, start: date, end: date) -> list[sqlite3.Row]:
    offset = datetime.now(display_zone()).utcoffset() or timedelta(0)
    hours = round(offset.total_seconds() / 3600)
    modifier = f"{'+' if hours >= 0 else '-'}{abs(hours)} hours"
    return conn.execute(
        """
        SELECT date(called_at, ?) AS day, feature, model, count(*) AS calls,
               sum(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed,
               coalesce(sum(input_tokens), 0) AS in_tokens,
               coalesce(sum(output_tokens), 0) AS out_tokens,
               coalesce(sum(latency_ms), 0) AS latency
          FROM llm_calls
         WHERE called_at >= ? AND called_at < ?
         GROUP BY day, feature, model
        """,
        (modifier, _utc(start), _utc(end)),
    ).fetchall()


def build_report(conn: sqlite3.Connection, period: str, today: date | None = None) -> Report:
    period = period if period in PERIODS else PERIOD_MONTH
    today = today or datetime.now(display_zone()).date()
    start, end = bounds(period, today)
    table = pricing.price_table(conn)

    total = Bucket()
    features: dict[str, Bucket] = {}
    models: dict[str, Bucket] = {}
    days: dict[str, Day] = {}
    current = start
    while current < end:
        days[current.isoformat()] = Day(f"{current.month}/{current.day}")
        current += timedelta(days=1)

    for row in _rows(conn, start, end):
        model = str(row["model"] or "")
        cost = pricing.cost_in(table, model, int(row["in_tokens"]), int(row["out_tokens"]))
        group = FEATURE_GROUPS.get(str(row["feature"]), str(row["feature"]))
        _add(total, row, cost)
        _add(features.setdefault(group, Bucket()), row, cost)
        _add(models.setdefault(model, Bucket()), row, cost)
        day = days.get(str(row["day"]))
        if day is not None and cost is not None:
            day.groups[group] = day.groups.get(group, 0.0) + cost

    length = (end - start).days
    previous = Bucket()
    for row in _rows(conn, start - timedelta(days=length), start):
        model = str(row["model"] or "")
        _add(
            previous,
            row,
            pricing.cost_in(table, model, int(row["in_tokens"]), int(row["out_tokens"])),
        )

    today_bucket = Bucket()
    for row in _rows(conn, today, today + timedelta(days=1)):
        model = str(row["model"] or "")
        _add(
            today_bucket,
            row,
            pricing.cost_in(table, model, int(row["in_tokens"]), int(row["out_tokens"])),
        )

    classified = int(
        conn.execute(
            "SELECT count(*) AS n FROM job_classifications"
            " WHERE classified_at >= ? AND classified_at < ?",
            (_utc(start), _utc(end)),
        ).fetchone()["n"]
    )

    forecast: int | None = None
    if period == PERIOD_MONTH:
        elapsed = today.day
        month_days = calendar.monthrange(today.year, today.month)[1]
        forecast = round(total.usd / elapsed * month_days * pricing.KRW_PER_USD)

    day_list = list(days.values())
    return Report(
        period=period,
        total=total,
        previous_usd=previous.usd,
        today=today_bucket,
        classified=classified,
        forecast_krw=forecast,
        days=day_list,
        peak_usd=max([day.usd for day in day_list] + [0.0]),
        features=sorted(features.items(), key=lambda item: -item[1].usd),
        models=sorted(
            ((name, bucket, name.strip() in table) for name, bucket in models.items()),
            key=lambda item: -item[1].usd,
        ),
        unpriced_models=sorted(
            (name, bucket.unpriced) for name, bucket in models.items() if bucket.unpriced
        ),
    )


@router.get("/cost", response_class=HTMLResponse)
def cost_page(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
    period: str = PERIOD_MONTH,
) -> HTMLResponse:
    """위 메뉴 `비용`. 기간은 주소의 `period` 로 고른다."""
    report = build_report(conn, period)
    return render_page(
        request,
        "pages/cost.html",
        report=report,
        periods=PERIODS,
        group_colors=GROUP_COLORS,
        other_color=OTHER_COLOR,
        krw_per_usd=pricing.KRW_PER_USD,
    )


def _price_rows(conn: sqlite3.Connection) -> list[tuple[str, pricing.KnownPrice | None, int]]:
    """단가 표에 올릴 모델. 단가를 아는 모델과 호출 기록에 나온 모델을 합친다.

    단가 없는 모델이 먼저다 — 넣어야 할 것이 위에 있어야 한다.
    """
    table = pricing.price_table(conn)
    called = {
        str(row["model"]): int(row["calls"])
        for row in conn.execute(
            "SELECT model, count(*) AS calls FROM llm_calls WHERE model <> '' GROUP BY model"
        )
    }
    names = sorted(set(table) | set(called))
    rows = [(name, table.get(name), called.get(name, 0)) for name in names]
    return sorted(rows, key=lambda row: (row[1] is not None, -row[2], row[0]))


def _prices(request: Request, conn: sqlite3.Connection, message: str = "") -> HTMLResponse:
    return render(
        request,
        "fragments/price_table.html",
        rows=_price_rows(conn),
        stored=pricing.SOURCE_STORED,
        message=message,
    )


@router.get("/ui/prices", response_class=HTMLResponse)
def price_table_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """설정 > AI 의 모델 단가 표. 비용 화면이 이 단가로 센다."""
    return _prices(request, conn)


@router.post("/ui/prices", response_class=HTMLResponse)
def price_save_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
    model: Annotated[str, Form()] = "",
    input_usd: Annotated[str, Form()] = "",
    output_usd: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """모델 하나의 단가를 넣거나 고친다. 둘 다 비우면 넣은 단가를 지우고 코드 표로 돌아간다."""
    name = model.strip()
    if not name:
        return _prices(request, conn, "모델 이름이 비었다")
    if not input_usd.strip() and not output_usd.strip():
        pricing.delete_price(conn, name)
        return _prices(request, conn, f"{name} 의 직접 입력 단가를 지웠다")
    try:
        pricing.save_price(conn, name, float(input_usd), float(output_usd))
    except ValueError:
        return _prices(request, conn, "단가는 0 이상의 숫자여야 한다")
    return _prices(request, conn, f"{name} 단가를 저장했다")
