"""회사 화면의 조각 라우트.

로고를 회사마다 한 번 넣는 자리다. 잇는 값은 회사명이라, 여기서 넣은 주소 하나가 그 이름을
가진 공고 전부에 붙는다 (`migrations/0020_companies.sql`). 공고마다 로고를 넣는 길은 만들지
않는다 — 그 길이 있으면 한 회사의 로고가 공고 수만큼 갈라진다.

행을 만드는 것은 정규화다 (`app/normalize/engine.py`). 이 화면은 있는 행을 고치기만 한다.
운영자가 회사명을 손으로 치게 두면 오타 하나로 그 로고는 어느 공고에도 붙지 않는다.

## 공고 수는 여기서 센다

`app/companies.py` 는 `companies` 하나만 읽는다. 공고 수는 `normalized_jobs` 를 함께 읽어야
하고, 그 셈이 저장소 모듈에 들어가면 회사 한 행을 고치는 일과 공고를 세는 일이 한 자리에
섞인다. 그래서 세는 SQL 이 이 파일에 있다.

## 기본 정렬이 공고 많은 순이다

이름 순이 아니다. 로고 하나가 몇 건에 붙는지가 무엇을 먼저 등록할지를 정한다. 공고 한
건짜리 회사를 먼저 등록하느라 백 건짜리가 뒤에 서면 이 화면은 일을 늘리기만 한다
(`.claude/tasks/todo/prd-fields-and-logo.md` 4장).

잇는 값이 이름이므로 세는 것도 이름으로 잇는다. 외래키가 없어서가 아니라, 로고가 실제로
붙는 경로가 그것이라 그 경로로 세야 화면의 숫자와 붙는 건수가 같다.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse

from app import companies
from app.api.settings import get_connection
from app.api.ui import render, render_error
from app.normalize.backfill import rewrite_one
from app.normalize.engine import NormalizeError, RawJobMissingError, load_rules
from app.storage import s3
from app.storage import settings as store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ui"], include_in_schema=False)


@dataclass(frozen=True)
class CompanyRow:
    """화면이 그리는 회사 한 줄. 저장된 행에 그 이름을 가진 공고 수를 얹은 것이다."""

    name: str
    parent_name: str | None
    logo_url: str | None
    job_count: int


# 공고 많은 순, 같으면 이름 순. `LEFT JOIN` 이라 공고가 하나도 없는 회사도 0건으로 남는다 —
# 빠지면 로고를 지울 회사를 화면에서 찾을 수 없다
_ROWS_SQL = """
SELECT c.name AS name,
       c.parent_name AS parent_name,
       c.logo_url AS logo_url,
       COUNT(j.id) AS job_count
FROM companies c
LEFT JOIN normalized_jobs j ON j.company_name = c.name
{where}
GROUP BY c.id
{having}
ORDER BY job_count DESC, c.name
"""

# 로고가 비었다고 볼 값. NULL 로 지우는 것이 정상 경로지만(`app/companies.py`), 빈 문자열이
# 들어온 행이 `로고 있음` 으로 걸러지면 그 회사는 이 목록에서 영영 사라진다
_NO_LOGO = "c.logo_url IS NULL OR c.logo_url = ''"

# 다른 회사 행이 이 이름을 모회사로 적어 뒀으면 "진짜 모회사" 행이다. 자회사가 없는 사이트의
# 회사(예: 토스)는 자기 이름으로만 행이 생기고 아무도 그 이름을 모회사로 적지 않으니 여기 걸리지
# 않는다 — 그런 행은 자회사 목록 쪽에 그대로 남는다(`app/companies.py::register`)
_IS_PARENT_GROUP = "EXISTS (SELECT 1 FROM companies c2 WHERE c2.parent_name = c.name)"


def _select(
    conn: sqlite3.Connection, where: str, having: str, params: tuple[object, ...]
) -> list[CompanyRow]:
    """세는 SQL 한 벌. 조건만 갈아 끼운다 — 목록과 한 줄이 같은 셈을 쓴다."""
    return [
        CompanyRow(
            name=str(row["name"]),
            parent_name=None if row["parent_name"] is None else str(row["parent_name"]),
            logo_url=None if row["logo_url"] is None else str(row["logo_url"]),
            job_count=int(row["job_count"]),
        )
        for row in conn.execute(_ROWS_SQL.format(where=where, having=having), params)
    ]


def rows(
    conn: sqlite3.Connection,
    *,
    no_logo: bool = False,
    min_jobs: int = 0,
    parent_only: bool = False,
) -> list[CompanyRow]:
    """조건에 걸린 회사. 공고 많은 순이다. 읽기 전용이다.

    `parent_only` 가 참이면 다른 회사가 모회사로 가리키는 행만, 거짓이면(기본) 그런 행을 뺀
    나머지(자회사 + 자회사가 없는 단독 회사)만 나온다 — 화면의 두 탭이 이 값으로 갈린다.

    나머지 조건은 함께 걸린다. `로고 없음` 과 `공고 N건 이상` 을 같이 걸면 로고가 없으면서
    공고가 여러 개인 회사만 남고, 그것이 곧 등록할 목록이다.
    """
    threshold = max(min_jobs, 0)
    conditions = [_IS_PARENT_GROUP if parent_only else f"NOT {_IS_PARENT_GROUP}"]
    if no_logo:
        conditions.append(_NO_LOGO)
    return _select(
        conn,
        "WHERE " + " AND ".join(conditions),
        "HAVING job_count >= ?" if threshold else "",
        (threshold,) if threshold else (),
    )


def read_row(conn: sqlite3.Connection, name: str) -> CompanyRow | None:
    """회사 한 줄. 없으면 None 이다. 목록과 같은 셈으로 공고 수를 얹는다."""
    found = _select(conn, "WHERE c.name = ?", "", (name.strip(),))
    return found[0] if found else None


def _row_context(config: store.StorageConfig) -> dict[str, object]:
    """줄 하나를 그릴 때 저장소에서 오는 값. 목록도 저장 결과도 같은 것을 받는다.

    `public_base` 로 시작하지 않는 로고는 화면에 `옛 저장소` 로 적힌다. 엔드포인트를 바꾸면
    이미 올린 파일은 따라가지 않고 주소만 옛 저장소를 가리킨 채 남는데
    (`.claude/tasks/todo/prd-fields-and-logo.md` 5장), 표시가 없으면 무엇을 다시 올려야
    하는지 알 방법이 없다. 밖에 올려 둔 주소를 붙여넣은 행도 같은 표시를 받는다 — 어디서 온
    주소인지는 저장하지 않아 둘을 가릴 수 없고, 그 사실을 화면에 적는다.

    `storage_ready` 가 거짓이면 화면은 파일 고르기를 내지 않고 무엇을 채워야 하는지 적는다.
    고르기가 조용히 실패하면 운영자는 로고가 왜 안 붙는지 알 방법이 없다.
    """
    return {
        "public_base": config.public_base.rstrip("/"),
        "storage_ready": config.configured,
    }


@router.get("/ui/companies", response_class=HTMLResponse)
def company_list_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    no_logo: Annotated[str, Query()] = "",
    min_jobs: Annotated[str, Query()] = "",
    group: Annotated[str, Query()] = "subsidiary",
) -> HTMLResponse:
    """조건에 걸린 회사 목록. 행이 없으면 무엇을 하면 생기는지 적는다.

    `no_logo` 는 체크박스라 켜졌을 때만 값이 온다. 문자열로 받는 것은 브라우저가 보내는
    `on` 을 그대로 참으로 읽기 위해서다.

    `min_jobs` 도 문자열이다. 숫자 칸을 비우면 빈 값이 오는데, 정수로 받으면 그것이 422 가
    되어 조건을 지우려던 조작이 오류 조각으로 돌아온다. 빈 값은 조건 없음이다.

    `group` 은 라디오 버튼이다. `parent` 가 아니면 전부(오타 포함) 자회사 탭으로 본다 —
    잘못된 값으로 모회사 행이 섞여 나오는 쪽보다 안전하다.
    """
    threshold = int(min_jobs) if min_jobs.strip().isdigit() else 0
    parent_only = group == "parent"
    matched = rows(conn, no_logo=bool(no_logo), min_jobs=threshold, parent_only=parent_only)
    return render(
        request,
        "fragments/company_list.html",
        rows=matched,
        total_jobs=sum(row.job_count for row in matched),
        filtered=bool(no_logo) or threshold > 0,
        parent_only=parent_only,
        **_row_context(store.read_config(conn)),
    )


def refresh_postings(conn: sqlite3.Connection, name: str) -> str:
    """그 회사가 자회사나 모회사로 적힌 공고를 다시 정규화한다. 알림 뒤에 붙일 문장을 돌려준다.

    대표 이미지는 정규화 때 회사 로고로 정해 저장한다 (2026-09-15 결정). 로고만 적고 다시 정규화하지
    않으면 이미 쌓인 공고는 옛 대표 이미지로 오공고에 간다. 모회사 로고는 자회사 로고가 없는 공고에
    쓰이므로 모회사 칸이 같은 공고도 다시 본다.

    한 건이 실패해도 나머지는 계속한다. 실패한 건은 전과 같은 값을 갖고, 문장에 건수를 남긴다.
    """
    raw_job_ids = [
        int(row[0])
        for row in conn.execute(
            "SELECT DISTINCT raw_job_id FROM normalized_jobs"
            " WHERE company_name = ? OR parent_company_name = ? ORDER BY raw_job_id",
            (name, name),
        )
    ]
    if not raw_job_ids:
        return ""
    try:
        rules = load_rules(conn)
    except NormalizeError as exc:
        logger.warning("로고를 적었지만 정규화 규칙을 읽지 못했다: %s", exc)
        return f". 정규화 규칙을 읽지 못해 공고 {len(raw_job_ids)}건의 대표 이미지는 그대로다"
    failed = 0
    for raw_job_id in raw_job_ids:
        try:
            rewrite_one(conn, raw_job_id, rules)
        except (NormalizeError, RawJobMissingError) as exc:
            logger.warning(
                "대표 이미지를 바꾸려 다시 정규화하다 실패했다 raw_jobs %s: %s", raw_job_id, exc
            )
            failed += 1
    note = f". 공고 {len(raw_job_ids) - failed}건을 다시 정규화해 대표 이미지를 바꿨다"
    if failed:
        note += f" — {failed}건은 정규화에 실패해 그대로다"
    return note


def attach_note(row: CompanyRow, *, cleared: bool) -> str:
    """저장 뒤에 적는 문장. 이 로고가 몇 건에 붙는지가 그 문장의 전부다.

    건수를 적는 이유는 공고마다 넣는 자리가 없기 때문이다. 숫자가 없으면 운영자는 방금 한
    일이 한 건에 붙은 것인지 백 건에 붙은 것인지 알 수 없고, 그것을 확인하러 검수 화면으로
    간다.
    """
    if cleared:
        if row.job_count == 0:
            return "로고를 지웠다. 이 회사명을 가진 공고는 아직 없다"
        return f"로고를 지웠다. 이 회사명을 가진 공고 {row.job_count}건에서 함께 빠진다"
    if row.job_count == 0:
        return (
            "저장했다. 지금은 이 회사명을 가진 공고가 없다 — 그 이름으로 들어오는 공고부터 붙는다"
        )
    return (
        f"저장했다. 이 로고는 회사명이 같은 공고 {row.job_count}건에 붙는다 — "
        "공고마다 따로 넣지 않는다"
    )


@dataclass(frozen=True)
class Refusal:
    """받지 않은 값 하나. 낱말과 문장을 갖는다 — 사유마다 고치는 자리가 다르다."""

    reason: str
    message: str
    title: str = "저장하지 못했다"


# 받는 주소 형식. 저장소 설정의 공개 주소와 같은 규칙이다 (`app/storage/settings.py`)
_SCHEMES = ("http://", "https://")


@router.put("/ui/companies/logo", response_class=HTMLResponse)
def save_logo_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    name: Annotated[str, Form()],
    logo_url: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """이미 어딘가에 올려 둔 주소를 그 회사 행에 적는다. 고친 줄 하나만 돌려준다.

    **우리 저장소를 거치지 않는다.** 파일은 남의 곳에 있고 우리는 주소만 갖는다. 그래서 그
    주소가 사라지면 로고도 사라지고, 그것을 알 방법은 화면에서 미리보기가 깨지는 것뿐이다.

    형식을 서버에서 본다. 브라우저의 `type="url"` 은 화면을 지나는 값만 막고, `javascript:`
    로 시작하는 값이 저장되면 그것이 곧 목록의 링크가 된다.

    회사명을 주소가 아니라 폼 값으로 받는다. 이름에 슬래시나 물음표가 든 회사가 하나라도
    생기면 주소에 넣은 이름은 다른 경로로 읽힌다.

    목록을 통째로 다시 부르지 않는다. 그러면 방금 적은 문장이 사라지고, 걸어 둔 조회 조건에
    따라 그 회사가 목록에서 빠져 무엇이 저장됐는지 확인할 자리가 없어진다.
    """
    cleaned = logo_url.strip()
    row = read_row(conn, name)
    if row is None:
        return render_error(request, "not_found", f"회사 행이 없다: {name.strip()!r}")
    if cleaned and not cleaned.startswith(_SCHEMES):
        return _row(
            request,
            conn,
            row,
            error=Refusal(
                "invalid_input",
                f"로고 주소는 http:// 나 https:// 로 시작해야 한다: {cleaned!r}",
                "주소를 받지 않았다",
            ),
        )
    companies.set_logo_url(conn, row.name, cleaned)
    refreshed = refresh_postings(conn, row.name)
    saved = read_row(conn, row.name) or row
    logger.info("회사 로고를 적었다: %s -> %r (공고 %d건)", saved.name, cleaned, saved.job_count)
    return _row(request, conn, saved, message=attach_note(saved, cleared=not cleaned) + refreshed)


def _row(
    request: Request,
    conn: sqlite3.Connection,
    row: CompanyRow,
    *,
    message: str = "",
    error: Refusal | None = None,
) -> HTMLResponse:
    """줄 하나를 돌려준다. 성공도 실패도 그 자리에 남는다."""
    return render(
        request,
        "fragments/company_row.html",
        row=row,
        message=message,
        error=error,
        **_row_context(store.read_config(conn)),
    )


# 올린 파일이 들어갈 자리. `_check/` 와 섞이지 않게 접두어를 둔다 (`app/storage/s3.py`)
UPLOAD_PREFIX = "company_name/"


@router.post("/ui/companies/logo/upload", response_class=HTMLResponse)
def upload_logo_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    name: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
) -> HTMLResponse:
    """고른 파일을 저장소에 올리고 그 공개 주소를 회사 행에 적는다.

    객체 이름에 회사명을 넣지 않는다. 이름이 한글이라 공개 주소가 퍼센트 인코딩으로 덮이고,
    회사명이 바뀌면 파일 이름과 어긋난다. 무엇이 어느 회사 것인지는 `companies.logo_url` 이
    안다.

    같은 회사에 두 번 올리면 앞 파일은 저장소에 남는다. 지우는 동작은 만들지 않는다 —
    회사가 열몇 곳이고, 지우다 잘못 지운 파일은 되살릴 방법이 없다.
    """
    row = read_row(conn, name)
    if row is None:
        return render_error(request, "not_found", f"회사 행이 없다: {name.strip()!r}")

    # 상한보다 한 바이트만 더 읽는다. 다 읽고 나서 재면 이미 다 쓴 뒤다 (`_spool` 과 같다)
    data = file.file.read(s3.MAX_IMAGE_BYTES + 1)
    config = store.read_config(conn)
    try:
        public_url = s3.upload_image(config, data=data, name=f"{UPLOAD_PREFIX}{uuid4().hex}")
    except s3.StorageError as exc:
        logger.info("로고를 올리지 못했다: %s / %s", exc.reason, exc.message)
        return _row(request, conn, row, error=Refusal(exc.reason, exc.message, "올리지 못했다"))

    companies.set_logo_url(conn, row.name, public_url)
    refreshed = refresh_postings(conn, row.name)
    saved = read_row(conn, row.name) or row
    logger.info("회사 로고를 올렸다: %s -> %s (공고 %d건)", saved.name, public_url, saved.job_count)
    return _row(request, conn, saved, message=attach_note(saved, cleared=False) + refreshed)


# 모회사를 여기서 사람이 고치는 라우트(`PUT /ui/companies/parent`)는 2026-08-29 에 뺐다.
# 크롤러 등록 화면이 모회사 이름을 필수로 받게 되면서(`app/api/crawlers.py`) 정규화가 모회사를
# 못 정해 사람이 바로잡을 일이 없어졌다 — 고칠 곳은 크롤러 등록 화면이지 이 화면이 아니다.
# `app/companies.py` 의 `set_parent_name` 자체는 남아 있다.
