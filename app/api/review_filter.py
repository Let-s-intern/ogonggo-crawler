"""공고 목록의 조회 조건과 지우기.

한 화면(`/review`)이 좁혀서 보고, 좁힌 것을 지운다. 목록과 상세 패널은 `app/api/review.py` 다.
조건을 만드는 곳이 하나여야 표가 센 건수와 지우기가 지우는 행이 같다.

조건은 보는 데 필요한 것만 둔다 — 목록 보기(오늘 들어옴·확인 필요·보냄·전체), 검색, 사이트,
모집 여부, 직군, 그리고 잘못 수집된 중복을 지울 때 쓰는 중복 찾기.

## 확인 필요

운영자가 이 화면을 여는 이유는 "공고가 잘 들어왔는지 보기" 하나다 (2026-09-17 결정, LC-3344).
그래서 손이 가야 하는 공고를 한데 모은다. 아직 보내지 않았고 마감 전인 공고 중 넷 중 하나라도
걸린 것이다 — 오공고가 거절했다, 오공고가 반드시 받는 칸이 비었다, AI 분류가 아직 안 됐다,
본문이 거의 없다. 마감이 지난 공고는 어차피 보내지 않으므로 넣지 않는다.

## 조건은 화면에서 온 문자열로 조립하지 않는다

상태값과 중복 기준은 이 파일이 가진 표에 있는 것만 받는다. 검색어와 직군은 바인딩으로만 넣는다.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.api import crawlers
from app.api.ui import display_zone, render
from app.deliver.spring import OPEN_SQL, READY_SQL

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ui"], include_in_schema=False)

# 모집 여부. 마감일이 없거나 날짜로 읽히지 않는 값은 `마감일 없음` 에 모은다 — 그렇지 않으면
# 어느 조건에도 걸리지 않는 행이 조용히 생긴다
DEADLINE_STATES: dict[str, str] = {
    "open": "모집 중",
    "closed": "마감",
    "none": "마감일 없음",
}

# 오공고로 보냈는지. 오공고에 들어간 뒤의 상태는 크롤러가 알 수 없어 보냈는지만 가른다
DELIVERY_STATES: dict[str, str] = {
    "yes": "전송함",
    "no": "전송 안 함",
}

# 이 공고를 오공고로 보냈다는 조건. 목록의 칸, 조회 조건, 지우기 확인 창이 같은 조건을 쓴다
SENT_SQL = (
    "EXISTS (SELECT 1 FROM spring_deliveries d"
    " WHERE d.source_url = n.source_url AND d.status = 'sent')"
)

# 목록 보기. 위 칩과 숫자 카드가 고른다. 비어 있으면 전체다 — 화면은 늘 `today` 를 보내고,
# 조건 없이 부르는 API 호출과 테스트는 예전처럼 전체를 본다
VIEW_TODAY = "today"
VIEW_CHECK = "check"
VIEW_SENT = "sent"
VIEW_ALL = "all"
VIEWS: dict[str, str] = {
    VIEW_TODAY: "오늘 들어옴",
    VIEW_CHECK: "확인 필요",
    VIEW_SENT: "보냄",
    VIEW_ALL: "전체",
}

# 이보다 짧은 본문은 상세를 제대로 못 읽은 것으로 본다. 목록의 한 줄 요약만 들어온 경우다
SHORT_BODY_CHARS = 200

# 확인 필요를 이루는 넷. 목록의 행마다 어느 것에 걸렸는지 표시로도 쓴다. 바인딩이 없는 조각이다
FAILED_SQL = (
    "EXISTS (SELECT 1 FROM spring_deliveries d"
    " WHERE d.source_url = n.source_url AND d.status = 'failed')"
)
UNREADY_SQL = f"NOT {READY_SQL}"
UNCLASSIFIED_SQL = (
    "NOT EXISTS (SELECT 1 FROM job_classifications c WHERE c.raw_job_id = n.raw_job_id)"
)
SHORT_BODY_SQL = f"length(trim(coalesce(n.body, ''))) < {SHORT_BODY_CHARS}"

# 마감 전 조건(`OPEN_SQL`)에 바인딩 하나가 든다 — 표시 시간대의 지금이다
_CHECK_SQL = (
    f"(NOT {{sent}} AND {OPEN_SQL} AND ({FAILED_SQL} OR {UNREADY_SQL}"
    f" OR {UNCLASSIFIED_SQL} OR {SHORT_BODY_SQL}))"
)

# 같은 공고가 두 번 들어왔는지 보는 기준. 무엇을 중복으로 볼지가 상황마다 달라 고르게 둔다.
# 자동으로 지우지 않는다 — 삼성전자 DX부문과 삼성SDI가 각각 올린 `R&D분야 외국인 경력사원
# 채용` 은 제목이 같아도 다른 공고다. 화면은 묶음을 보여주기만 하고 지우는 것은 사람이 고른다
DUP_TITLE_COMPANY = "title_company"
DUP_TITLE = "title"
DUP_SOURCE_URL = "source_url"
DUP_CRITERIA: tuple[str, ...] = (DUP_TITLE_COMPANY, DUP_TITLE, DUP_SOURCE_URL)
DUP_LABELS: dict[str, str] = {
    DUP_TITLE_COMPANY: "제목 + 회사",
    DUP_TITLE: "제목",
    DUP_SOURCE_URL: "원본 주소",
}

# 기준마다 무엇을 잡는지. 넓은 기준일수록 진짜 중복이 아닌 것이 섞인다는 것을 화면에 적는다
DUP_NOTES: dict[str, str] = {
    DUP_TITLE_COMPANY: "같은 회사가 같은 제목으로 두 번 올린 것. 가장 좁고 확실하다",
    DUP_TITLE: "계열사가 나눠 올린 것까지 잡는다. 넓다 — 제목이 같아도 다른 공고일 수 있다",
    DUP_SOURCE_URL: "같은 주소를 두 번 저장한 것. 중복 판정이 고장 났을 때만 걸린다",
}

# 묶음을 이루는 값에 붙일 이름. 표에 값만 늘어놓으면 무엇이 같아서 묶였는지 읽히지 않는다
DUP_PART_LABELS: dict[str, tuple[str, ...]] = {
    DUP_TITLE_COMPANY: ("제목", "회사"),
    DUP_TITLE: ("제목",),
    DUP_SOURCE_URL: ("원본 주소",),
}

# 묶음 목록에 몇 개까지 적을지. 번호는 전부에 매기고 표에 적는 것만 끊는다
DUP_GROUP_PREVIEW = 20

# 묶음 키를 이룰 값들을 잇는 글자. 값에 들어갈 일이 없는 제어문자를 쓴다
_DUP_SEPARATOR = "char(31)"
_DUP_SEPARATOR_TEXT = "\x1f"

# 지울 대상을 무엇으로 고른 것인지. "이 페이지의 20건" 과 "조건에 걸린 148건" 이 같은 단추
# 뒤에 숨어 있으면 운영자는 20건인 줄 알고 148건을 지운다. 범위는 이름을 갖고, 화면은 그
# 이름과 건수를 늘 함께 적는다
SCOPE_SELECTED = "selected"
SCOPE_FILTERED = "filtered"
SCOPE_WORKFLOW = "workflow"
SCOPES: tuple[str, ...] = (SCOPE_SELECTED, SCOPE_FILTERED, SCOPE_WORKFLOW)

# 범위를 사람이 읽는 한 줄로. 확인 창의 첫 줄이고 로그에도 같은 문장이 남는다
SCOPE_LABELS: dict[str, str] = {
    SCOPE_SELECTED: "표에서 고른 공고",
    SCOPE_FILTERED: "지금 조회 조건에 걸린 전부",
    SCOPE_WORKFLOW: "워크플로우 {workflow} 가 모은 공고 전부",
}

# 표를 다시 부르라고 알리는 이벤트 이름. 지우고 나면 표에 없는 행이 남아 있다
TABLE_RELOAD_EVENT = "jobs-deleted"

# `IN (?, ?, ...)` 에 한 번에 넣을 id 수. SQLite 의 바인딩 개수 상한에 걸리지 않게 끊는다
_ID_CHUNK = 500

# 빈 값으로 볼 글자. 스페이스·탭·줄바꿈·캐리지리턴이다
_BLANK_CHARS = "' ' || char(9) || char(10) || char(13)"

# AI 채움률을 보여 줄 칸. (이름, SQL 식, 원문이 말하지 않으면 비는 것이 정상인가).
# 식은 이 표의 상수뿐이다 — 화면에서 온 값이 들어오지 않는다
FILL_FIELDS: tuple[tuple[str, str, bool], ...] = (
    ("회사명", "COALESCE(NULLIF(TRIM(n.company_name), ''), n.parent_company_name)", False),
    ("제목", "n.title", False),
    ("고용 형태", "n.employment_type", False),
    ("경력", "n.experience_type", False),
    ("학력", "n.education_level", False),
    ("직군", "n.job_field", False),
    ("직무", "n.job_role", False),
    ("산업", "n.industry", False),
    ("지원 방법", "n.application_method", False),
    ("주요 업무", "n.responsibilities", False),
    ("자격 요건", "n.qualifications", False),
    ("대표 이미지", "n.cover_image_url", False),
    ("모집 마감", "n.recruitment_end_at", True),
    ("모집 시작", "n.recruitment_start_at", True),
    ("근무 지역", "n.region", True),
    ("모집 인원", "n.recruitment_headcount", True),
    ("최소 경력 연수", "n.experience_min_years", True),
    ("우대 사항", "n.preferred_qualifications", True),
    ("회사·팀 소개", "n.company_and_team_introduction", True),
    ("급여·처우", "n.compensation", True),
    ("복지·혜택", "n.benefits", True),
    ("채용 절차", "n.hiring_process", True),
    ("채용 안내사항", "n.recruitment_notice", True),
)


def _today() -> str:
    """모집 여부를 가르는 오늘. 마감일은 날짜라 표시 시간대의 오늘과 비교한다."""
    return datetime.now(display_zone()).date().isoformat()


def today_start_utc() -> str:
    """표시 시간대 오늘 0시를 UTC 문자열로. 수집 시각과 보낸 시각은 UTC 로 저장돼 있다."""
    local = datetime.now(display_zone()).replace(hour=0, minute=0, second=0, microsecond=0)
    return local.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def now_local() -> str:
    """마감 일시와 견줄 지금. 모집 일시는 사이트의 한국 시각 그대로라 표시 시간대로 센다."""
    return datetime.now(display_zone()).strftime("%Y-%m-%d %H:%M:%S")


def check_sql() -> str:
    """확인 필요 조건. 바인딩 하나(`now_local()`)를 받는다."""
    return _CHECK_SQL.format(sent=SENT_SQL)


def _filled(expression: str) -> str:
    """그 식이 비어 있지 않다는 SQL 조각. 공백만 있는 값도 빈 것으로 본다."""
    return f"TRIM(COALESCE({expression}, ''), {_BLANK_CHARS}) <> ''"


@dataclass(frozen=True)
class JobFilter:
    """조회 조건 한 벌. 표와 지우기가 같은 조건을 본다."""

    workflow_id: int | None = None
    view: str = ""
    query: str = ""
    status: str = ""
    delivered: str = ""
    # 직군. 목록에 없는 이름이 와도 그대로 받는다 — 꺼진 직군으로 이미 분류된 공고를 조회할
    # 방법이 없어지면 안 된다
    job_field: str = ""
    dup: str = ""

    def as_form(self) -> dict[str, str]:
        """폼에 다시 실을 값. 지우기 요청과 페이지 이동이 표와 같은 조건을 들고 가게 한다."""
        return {
            "workflow_id": "" if self.workflow_id is None else str(self.workflow_id),
            "view": self.view,
            "q": self.query,
            "status": self.status,
            "delivered": self.delivered,
            "job_field": self.job_field,
            "dup": self.dup,
        }


def read_filter(
    workflow_id: str = "",
    view: str = "",
    q: str = "",
    status: str = "",
    delivered: str = "",
    job_field: str = "",
    dup: str = "",
) -> JobFilter:
    """화면이 보낸 값을 조건 한 벌로. 표에 없는 값은 조건을 걸지 않은 것으로 본다.

    빈 문자열로 받는 이유는 "전체" 를 고르면 빈 값이 오기 때문이다. 정수·열거 파라미터로 두면
    그 빈 값이 422 가 되어 표가 갱신되지 않는다.
    """
    return JobFilter(
        workflow_id=int(workflow_id) if workflow_id.strip().isdigit() else None,
        view=view if view in VIEWS and view != VIEW_ALL else "",
        query=q.strip(),
        status=status if status in DEADLINE_STATES else "",
        delivered=delivered if delivered in DELIVERY_STATES else "",
        job_field=job_field.strip(),
        dup=dup if dup in DUP_CRITERIA else "",
    )


def _company_or_parent() -> str:
    """중복 판정이 볼 회사. 자회사가 있으면 그것이고, 없으면 모회사다."""
    return f"COALESCE(NULLIF(TRIM(n.company_name, {_BLANK_CHARS}), ''), n.parent_company_name)"


def _dup_parts(kind: str) -> tuple[str, ...]:
    """그 기준이 무엇을 같다고 보는지, SQL 조각으로."""
    if kind == DUP_TITLE_COMPANY:
        return ("n.title", _company_or_parent())
    if kind == DUP_TITLE:
        return ("n.title",)
    return ("n.source_url",)


def _dup_key(kind: str) -> tuple[str, str]:
    """묶음 키와, 그 키를 믿어도 되는지 판정하는 조건. 빈 값끼리는 묶지 않는다."""
    parts = _dup_parts(kind)
    key = f" || {_DUP_SEPARATOR} || ".join(f"TRIM({part}, {_BLANK_CHARS})" for part in parts)
    usable = " AND ".join(_filled(part) for part in parts)
    return key, usable


def filter_sql(picked: JobFilter) -> tuple[str, list[Any]]:
    """조건을 `WHERE` 한 줄로. `normalized_jobs n` 과 `raw_jobs r` 이 붙어 있는 것을 전제한다."""
    clauses: list[str] = []
    params: list[Any] = []
    if picked.workflow_id is not None:
        clauses.append("r.workflow_id = ?")
        params.append(picked.workflow_id)
    if picked.view == VIEW_TODAY:
        clauses.append("r.crawled_at >= ?")
        params.append(today_start_utc())
    elif picked.view == VIEW_CHECK:
        clauses.append(check_sql())
        params.append(now_local())
    elif picked.view == VIEW_SENT:
        clauses.append(SENT_SQL)
    if picked.query:
        clauses.append("(n.title LIKE ? OR n.company_name LIKE ? OR n.parent_company_name LIKE ?)")
        params.extend([f"%{picked.query}%"] * 3)
    if picked.job_field:
        clauses.append("n.job_field = ?")
        params.append(picked.job_field)

    if picked.status == "open":
        clauses.append("date(n.recruitment_end_at) >= ?")
        params.append(_today())
    elif picked.status == "closed":
        clauses.append("date(n.recruitment_end_at) < ?")
        params.append(_today())
    elif picked.status == "none":
        clauses.append("date(n.recruitment_end_at) IS NULL")

    if picked.delivered == "yes":
        clauses.append(SENT_SQL)
    elif picked.delivered == "no":
        clauses.append(f"NOT {SENT_SQL}")

    # 중복은 나머지 조건 안에서 센다. `SK 안에서만 중복 찾기` 가 실제 쓰임이라, 전체에서 센
    # 묶음을 나중에 좁히면 짝을 잃은 한 건만 남아 중복이 아닌 것이 중복으로 보인다.
    # 여분만이 아니라 묶음 전체가 걸린다 — 짝을 봐야 어느 쪽을 지울지 정할 수 있다
    if picked.dup in DUP_CRITERIA:
        key, usable = _dup_key(picked.dup)
        inner = " WHERE " + " AND ".join([*clauses, usable])
        # 안쪽 질의가 바깥과 같은 조건을 그대로 쓴다. 바인딩도 같은 순서로 한 벌 더 간다
        params = [*params, *params]
        clauses.append(usable)
        clauses.append(
            f"({key}) IN (SELECT {key} FROM normalized_jobs n"
            f" JOIN raw_jobs r ON r.id = n.raw_job_id{inner}"
            " GROUP BY 1 HAVING count(*) > 1)"
        )

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _count(conn: sqlite3.Connection, where: str, params: list[Any]) -> int:
    row = conn.execute(
        f"SELECT count(*) AS total FROM normalized_jobs n"
        f" JOIN raw_jobs r ON r.id = n.raw_job_id{where}",
        params,
    ).fetchone()
    return int(row["total"]) if row is not None else 0


def count(conn: sqlite3.Connection, picked: JobFilter) -> int:
    """조건에 걸린 정규화 행 수. 표도 지우기도 이 함수를 쓴다."""
    where, params = filter_sql(picked)
    return _count(conn, where, params)


def order_clause(dup: str = "") -> str:
    """정렬 한 줄. 최근 수집한 것이 먼저다.

    중복 조건이 걸리면 묶음이 앞자리다. 짝이 흩어지면 어느 것과 어느 것이 짝인지 표에서 읽을
    수 없고, 페이지를 넘기면 짝이 다른 페이지로 갈라진다. 큰 묶음이 먼저 온다.
    """
    ordered = ["r.crawled_at DESC", "n.id DESC"]
    if dup in DUP_CRITERIA:
        ordered = ["dup_size DESC", "dup_key ASC", *ordered]
    return " ORDER BY " + ", ".join(ordered)


def dup_columns(dup: str) -> str:
    """중복 조건이 걸렸을 때만 목록 질의에 붙는 두 칸. 묶음 키와 그 묶음의 건수다."""
    if dup not in DUP_CRITERIA:
        return ""
    key, _ = _dup_key(dup)
    return f", ({key}) AS dup_key, count(*) OVER (PARTITION BY {key}) AS dup_size"


def dup_groups(conn: sqlite3.Connection, picked: JobFilter) -> list[dict[str, Any]]:
    """지금 조건에 걸린 묶음 목록. 큰 묶음이 먼저고, 표의 행에도 같은 번호가 붙는다."""
    if picked.dup not in DUP_CRITERIA:
        return []
    where, params = filter_sql(picked)
    key, _ = _dup_key(picked.dup)
    rows = conn.execute(
        f"SELECT ({key}) AS dup_key, count(*) AS size FROM normalized_jobs n"
        f" JOIN raw_jobs r ON r.id = n.raw_job_id{where}"
        " GROUP BY 1 ORDER BY size DESC, dup_key ASC",
        params,
    ).fetchall()
    labels = DUP_PART_LABELS[picked.dup]
    found: list[dict[str, Any]] = []
    for number, row in enumerate(rows, start=1):
        text = str(row["dup_key"])
        values = text.split(_DUP_SEPARATOR_TEXT)
        found.append(
            {
                "number": number,
                "key": text,
                "parts": list(zip(labels, values, strict=False)),
                "count": int(row["size"]),
            }
        )
    return found


def fill_rates(conn: sqlite3.Connection, picked: JobFilter) -> list[dict[str, Any]]:
    """지금 조건에 걸린 공고에서 칸마다 몇 %가 채워졌는지. AI 가 자주 못 채우는 칸을 본다."""
    where, params = filter_sql(picked)
    picks = ", ".join(
        f"SUM(CASE WHEN {_filled(expression)} THEN 1 ELSE 0 END) AS f{index}"
        for index, (_, expression, _) in enumerate(FILL_FIELDS)
    )
    row = conn.execute(
        f"SELECT count(*) AS total, {picks}"
        f"  FROM normalized_jobs n JOIN raw_jobs r ON r.id = n.raw_job_id{where}",
        params,
    ).fetchone()
    total = int(row["total"] or 0) if row is not None else 0
    found: list[dict[str, Any]] = []
    for index, (label, _, soft) in enumerate(FILL_FIELDS):
        filled = int(row[f"f{index}"] or 0) if row is not None else 0
        found.append(
            {
                "label": label,
                "filled": filled,
                "pct": round(filled / total * 100) if total else 0,
                "soft": soft,
            }
        )
    return found


def _chunks(ids: Sequence[int]) -> Iterator[tuple[list[int], str]]:
    """id 목록을 바인딩 가능한 크기로 끊는다. 묶음마다 물음표 자리도 함께 낸다."""
    for start in range(0, len(ids), _ID_CHUNK):
        part = list(ids[start : start + _ID_CHUNK])
        yield part, ",".join("?" for _ in part)


def _existing_ids(conn: sqlite3.Connection, ids: Sequence[int]) -> tuple[int, ...]:
    """받은 id 중 지금도 `raw_jobs` 에 있는 것. 이미 사라진 id 는 그 자리에서 떨어뜨린다."""
    wanted = list(dict.fromkeys(int(value) for value in ids))
    found: list[int] = []
    for part, marks in _chunks(wanted):
        found.extend(
            int(row["id"])
            for row in conn.execute(
                f"SELECT id FROM raw_jobs WHERE id IN ({marks})", part
            ).fetchall()
        )
    return tuple(sorted(found))


def _count_ids(conn: sqlite3.Connection, sql: str, ids: Sequence[int]) -> int:
    """id 묶음에 걸리는 행 수. 묶음을 끊어 세고 더한다."""
    total = 0
    for part, marks in _chunks(ids):
        row = conn.execute(sql.format(marks=marks), part).fetchone()
        total += int(row[0]) if row is not None else 0
    return total


def workflow_label(conn: sqlite3.Connection, workflow_id: int | None) -> str:
    """워크플로우를 번호와 이름으로. 고르지 않았으면 빈 문자열이다."""
    if workflow_id is None:
        return ""
    found = conn.execute("SELECT name FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
    return f"{workflow_id} - {found['name']}" if found else str(workflow_id)


def _describe(conn: sqlite3.Connection, picked: JobFilter, scope: str) -> str:
    """지우는 데 실제로 걸린 조건을 한 줄로. 확인 창에 적히고 로그에도 같은 문장이 남는다.

    워크플로우 범위는 나머지 조건을 보지 않으므로 그 조건들을 적지 않는다. 걸리지도 않는 조건이
    건수 옆에 적혀 있으면 그것만 지워지는 줄로 읽힌다.
    """
    workflow = workflow_label(conn, picked.workflow_id) or "전체"
    if scope == SCOPE_WORKFLOW:
        return f"워크플로우 {workflow} · 나머지 조건은 걸리지 않는다"
    return " · ".join(
        (
            f"워크플로우 {workflow}",
            f"보기 {VIEWS.get(picked.view, '전체')}",
            f"직군 {picked.job_field or '전체'}",
            f"모집 {DEADLINE_STATES.get(picked.status, '전체')}",
            f"오공고 {DELIVERY_STATES.get(picked.delivered, '전체')}",
            f"중복 {DUP_LABELS.get(picked.dup, '안 걸림')}",
            f"검색어 {picked.query or '없음'}",
        )
    )


def view_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """칩과 숫자 카드의 건수. 다른 조회 조건은 걸지 않은 전체 기준이다."""
    return {
        view: count(conn, JobFilter(view=view)) for view in (VIEW_TODAY, VIEW_CHECK, VIEW_SENT)
    } | {VIEW_ALL: count(conn, JobFilter())}


def sent_today(conn: sqlite3.Connection) -> int:
    """오늘(표시 시간대) 오공고로 보낸 공고 수."""
    row = conn.execute(
        "SELECT count(*) AS n FROM spring_deliveries WHERE status = 'sent' AND sent_at >= ?",
        (today_start_utc(),),
    ).fetchone()
    return int(row["n"])


def failing_sites(conn: sqlite3.Connection) -> list[str]:
    """켜져 있는데 마지막 수집이 실패한 사이트 이름. 공고 목록 위 알림 띠가 쓴다.

    멈춘 사이트는 넣지 않는다 — 수집을 안 하고 있으니 "오늘 공고가 안 들어온" 이유가 실패가 아니다.
    """
    rows = conn.execute(
        """
        SELECT w.name FROM workflows w
         WHERE w.status = 'active'
           AND (SELECT c.status FROM crawl_runs c
                 WHERE c.workflow_id = w.id AND c.status IS NOT NULL
                 ORDER BY c.id DESC LIMIT 1) IN ('failed', 'timeout')
         ORDER BY w.name
        """
    ).fetchall()
    return [str(row["name"]) for row in rows]


@dataclass(frozen=True)
class DeleteTarget:
    """지울 대상 한 묶음. 표마다 몇 행이 사라지는지와, 그중 오공고로 보낸 것이 몇 건인지까지 든다.

    건수를 화면이 아니라 서버가 낸다. 확인 창이 보여준 숫자와 실제로 지워지는 행이 다르면,
    `raw_jobs` 는 다시 만들 수 없으므로 되돌릴 방법이 없다.
    """

    scope: str
    label: str
    criteria: str
    picked: JobFilter
    raw_job_ids: tuple[int, ...]
    normalized: int
    overrides: int
    sent: int

    @property
    def raw(self) -> int:
        return len(self.raw_job_ids)


def _build_target(
    conn: sqlite3.Connection,
    *,
    scope: str,
    ids: Sequence[int],
    picked: JobFilter,
    resolve: bool = True,
) -> DeleteTarget:
    """범위를 실제 `raw_jobs.id` 목록으로 바꾸고, 사라질 행을 센다.

    `resolve` 를 끄면 범위를 다시 풀지 않고 받은 id 만 쓴다. 확인 창을 지나온 요청이 그렇다 —
    확인 창이 148건이라고 적었는데 그 사이 크롤이 한 번 더 돌아 160건을 지우면 되돌릴 수 없다.
    """
    if resolve and scope == SCOPE_FILTERED:
        where, params = filter_sql(picked)
        rows = conn.execute(
            f"SELECT DISTINCT r.id AS id FROM normalized_jobs n"
            f" JOIN raw_jobs r ON r.id = n.raw_job_id{where} ORDER BY r.id",
            params,
        ).fetchall()
        raw_job_ids = tuple(int(row["id"]) for row in rows)
    elif resolve and scope == SCOPE_WORKFLOW:
        # 그 워크플로우가 모은 전부다. 정규화되지 않은 수집 건도 고른다 — 남겨 두면 다음
        # 재정규화에서 지운 공고가 되살아난다
        rows = conn.execute(
            "SELECT id FROM raw_jobs WHERE workflow_id = ? ORDER BY id", (picked.workflow_id,)
        ).fetchall()
        raw_job_ids = tuple(int(row["id"]) for row in rows)
    else:
        raw_job_ids = _existing_ids(conn, ids)

    return DeleteTarget(
        scope=scope,
        label=SCOPE_LABELS[scope].format(
            workflow=workflow_label(conn, picked.workflow_id) or "고르지 않음"
        ),
        criteria=_describe(conn, picked, scope),
        picked=picked,
        raw_job_ids=raw_job_ids,
        normalized=_count_ids(
            conn,
            "SELECT count(*) FROM normalized_jobs WHERE raw_job_id IN ({marks})",
            raw_job_ids,
        ),
        overrides=_count_ids(
            conn,
            "SELECT count(*) FROM job_field_overrides WHERE raw_job_id IN ({marks})",
            raw_job_ids,
        ),
        sent=_count_ids(
            conn,
            f"SELECT count(*) FROM normalized_jobs n WHERE {SENT_SQL}"
            " AND n.raw_job_id IN ({marks})",
            raw_job_ids,
        ),
    )


def _form_ids(values: Sequence[Any]) -> list[int]:
    """체크박스가 보낸 id. 숫자가 아닌 값은 버린다."""
    found: list[int] = []
    for value in values:
        text = str(value).strip()
        if text.isdigit():
            found.append(int(text))
    return found


async def _delete_request(request: Request) -> tuple[str, list[int], JobFilter]:
    """지우기 폼 한 벌. 확인 창과 실제 삭제가 같은 폼을 읽는다."""
    form = await request.form()
    scope = str(form.get("scope") or "").strip()
    if scope not in SCOPES:
        # 범위를 따로 싣지 않으면 `조건 전체` 체크박스가 정한다
        scope = SCOPE_FILTERED if form.get("all_filtered") else SCOPE_SELECTED
    picked = read_filter(
        **{
            name: str(form.get(name) or "")
            for name in ("workflow_id", "q", "status", "delivered", "job_field", "dup")
        }
    )
    return scope, _form_ids(form.getlist("raw_job_id")), picked


def _delete_rows(conn: sqlite3.Connection, raw_job_ids: Sequence[int]) -> tuple[int, int, int]:
    """여섯 표를 한 트랜잭션으로, 외래키 순서대로 비운다.

    `job_field_overrides` -> `job_classifications` -> `job_field_suggestions` ->
    `normalized_jobs` -> `raw_job_history` -> `raw_jobs` 순이다. 거꾸로 지우면 외래키가 막는다.
    분류·제안은 원문에서 다시 만들 수 있는 파생값이라 반환값 개수에 넣지 않는다.

    한 트랜잭션인 이유는 절반만 지워진 상태를 운영자가 손으로 풀 수 없어서다.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        overrides = normalized = raw = 0
        for part, marks in _chunks(raw_job_ids):
            overrides += conn.execute(
                f"DELETE FROM job_field_overrides WHERE raw_job_id IN ({marks})", part
            ).rowcount
            conn.execute(f"DELETE FROM job_custom_values WHERE raw_job_id IN ({marks})", part)
            conn.execute(f"DELETE FROM job_classifications WHERE raw_job_id IN ({marks})", part)
            conn.execute(f"DELETE FROM job_field_suggestions WHERE raw_job_id IN ({marks})", part)
            normalized += conn.execute(
                f"DELETE FROM normalized_jobs WHERE raw_job_id IN ({marks})", part
            ).rowcount
            # 원문 다시 수집이 남긴 이전 값. 공고를 지우면 그 이력도 가리킬 곳이 없다
            conn.execute(f"DELETE FROM raw_job_history WHERE raw_job_id IN ({marks})", part)
            raw += conn.execute(f"DELETE FROM raw_jobs WHERE id IN ({marks})", part).rowcount
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return raw, normalized, overrides


@router.post("/ui/review/delete/confirm", response_class=HTMLResponse)
async def job_delete_confirm_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """지우기 전에 무엇이 몇 건 사라지는지 보여주는 모달.

    브라우저 `confirm()` 을 쓰지 않는다. 적어야 하는 것이 한 줄로 끝나지 않아서다.
    GET 이 아니라 POST 로 받는다. 고른 id 가 백 개를 넘으면 주소에 실을 수 없다.
    """
    scope, ids, picked = await _delete_request(request)
    target = _build_target(conn, scope=scope, ids=ids, picked=picked)
    return render(request, "fragments/review_delete.html", target=target, done=None)


@router.post("/ui/review/delete", response_class=HTMLResponse)
async def job_delete_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """확인 창이 보여준 그 목록을 지운다. 범위를 여기서 다시 풀지 않는다.

    모달은 닫지 않고 결과를 그 자리에 적는다. 표는 `jobs-deleted` 를 받아 스스로 다시 그린다.
    """
    scope, ids, picked = await _delete_request(request)
    target = _build_target(conn, scope=scope, ids=ids, picked=picked, resolve=False)
    if target.raw == 0:
        return render(request, "fragments/review_delete.html", target=target, done=None)

    raw, normalized, overrides = _delete_rows(conn, target.raw_job_ids)
    # 요청자를 남긴다. 계정이 없는 단일 운영자라 남길 수 있는 것은 어디서 왔는지뿐이다
    client = request.client.host if request.client is not None else "알 수 없음"
    logger.info(
        "공고 목록에서 공고를 지웠다: 범위=%s(%s), 조건=%s,"
        " raw_jobs=%d, normalized_jobs=%d, job_field_overrides=%d,"
        " 오공고로 보냈던 공고=%d, 요청=%s",
        target.scope,
        target.label,
        target.criteria,
        raw,
        normalized,
        overrides,
        target.sent,
        client,
    )
    done = DeleteTarget(
        scope=target.scope,
        label=target.label,
        criteria=target.criteria,
        picked=picked,
        raw_job_ids=target.raw_job_ids[:raw],
        normalized=normalized,
        overrides=overrides,
        sent=target.sent,
    )
    response = render(request, "fragments/review_delete.html", target=target, done=done)
    # 설정(settle) 뒤에 표를 다시 부른다. 모달은 열어 둔 채다
    response.headers["HX-Trigger-After-Settle"] = TABLE_RELOAD_EVENT
    return response
