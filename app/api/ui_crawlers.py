"""크롤러 등록·셀렉터 편집 화면의 조각 라우트.

`app/api/crawlers.py` 를 그대로 부른다. 화면에서만 되는 등록 경로를 따로 만들지 않는다.

생성된 셀렉터는 가설이다. 실패한 필드가 있어도 행은 남고, 화면은 어느 필드가 몇 개 매칭됐는지
그대로 보여준다. 그 다음은 운영자가 손으로 고치는 것이지 다시 생성하는 것이 아니다
(`.claude/rules/llm.md`).

## 실패를 200 으로 돌려주는 이유

HTMX 는 4xx 응답을 기본적으로 갈아 끼우지 않는다. 조각 라우트가 422 를 돌려주면 화면에는
아무 일도 일어나지 않고, 운영자는 저장이 됐는지 안 됐는지 알 수 없다. 그래서 조각은 거절
사유를 담은 HTML 을 200 으로 돌려주고, 사유와 메시지를 화면에 그대로 적는다. 상태 코드로
실패를 알려야 하는 쪽은 `/api/...` 이고 그 라우터는 그대로다.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.api import crawlers
from app.api.ui import describe_path, render

# 마지막으로 성공한 실행이 언제 끝났는가. 테스트 실행은 `crawl_runs.crawler_id`, 주기·수동
# 실행은 워크플로우를 거쳐 크롤러에 붙는다. 실패로 끝난 실행은 경로를 확인해 주지 않으므로
# 세지 않는다 — 여기 적히는 시각은 "그때는 이 경로로 실제로 가져왔다" 는 뜻이다
_CHECKED_AT = """
    (SELECT r.finished_at
       FROM crawl_runs r
       LEFT JOIN workflows w ON w.id = r.workflow_id
      WHERE r.status = 'success'
        AND COALESCE(w.crawler_id, r.crawler_id) = c.id
      ORDER BY r.id DESC LIMIT 1)
"""

_LIST_QUERY = (
    "SELECT c.id AS id, c.name AS name, c.status AS status, c.list_url AS list_url, "
    "c.detail_url AS detail_url, c.default_company AS default_company, "
    "c.list_mode AS list_mode, c.detail_mode AS detail_mode, "
    "c.api_config_json AS api_config_json, c.selectors_json AS selectors_json, "
    "c.created_at AS created_at, "
    f"{_CHECKED_AT} AS checked_at "
    # 직접 넣은 공고의 크롤러는 셀렉터가 없는 자리라 빼 둔다 (`app/crawler/manual.py`)
    "FROM crawlers c WHERE NOT EXISTS (SELECT 1 FROM workflows w"
    " WHERE w.crawler_id = c.id AND w.kind = 'manual') ORDER BY c.id DESC"
)

router = APIRouter(tags=["ui"], include_in_schema=False)


def error_detail(exc: HTTPException) -> dict[str, str]:
    """HTTPException 의 detail 을 화면이 읽는 모양으로 옮긴다."""
    detail = exc.detail
    if isinstance(detail, dict):
        return {
            "reason": str(detail.get("reason", exc.status_code)),
            "message": str(detail.get("message", detail)),
        }
    return {"reason": str(exc.status_code), "message": str(detail)}


def crawler_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """등록된 크롤러 전부. 테스트 실행 화면도 같은 목록을 쓴다.

    행마다 `path` 가 붙는다. 어떤 방식으로 도는지를 낱말로 옮긴 것이고, 판정은 여기서 끝난다 —
    템플릿은 낱말을 그리기만 한다 (`.claude/agents/ui-worker.md`).
    """
    return [_with_path(row) for row in conn.execute(_LIST_QUERY).fetchall()]


def _with_path(row: sqlite3.Row) -> dict[str, Any]:
    """크롤러 한 행 + 경로 낱말. 화면이 읽는 값은 전부 이 딕셔너리 안에 있다."""
    checked_at = str(row["checked_at"] or "")
    return {
        **{key: row[key] for key in row.keys()},
        "path": describe_path(
            list_mode=str(row["list_mode"]),
            detail_mode=str(row["detail_mode"]),
            list_url=str(row["list_url"] or ""),
            api_config_json=row["api_config_json"],
            selectors_json=row["selectors_json"],
            checked_at=checked_at,
            checked_note=(
                "성공한 실행이 마지막으로 이 경로로 가져온 때다"
                if checked_at
                else "성공한 실행이 아직 없다. 테스트 실행으로 확인한다"
            ),
        ),
    }


def pretty_selectors(selectors_json: str) -> str:
    """저장된 JSON 을 사람이 고칠 수 있게 편다. 못 읽는 값은 그대로 보여준다.

    테스트 실행 화면의 편집기도 이 함수를 쓴다. 두 화면의 편집기에 다른 모양이 뜨면
    같은 값을 보고 있는지 화면에서 알 수 없다.
    """
    try:
        return json.dumps(json.loads(selectors_json), ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return selectors_json


def _result(
    request: Request,
    *,
    conn: sqlite3.Connection | None = None,
    crawler_id: int | None = None,
    status: str = "",
    selectors_json: str = "",
    notice: str = "",
    error: dict[str, str] | None = None,
    generation: dict[str, Any] | None = None,
    repair: dict[str, Any] | None = None,
    path: dict[str, Any] | None = None,
) -> HTMLResponse:
    """결과 영역 하나를 렌더한다. `conn` 을 주면 크롤러 목록도 함께 갱신한다(OOB)."""
    return render(
        request,
        "fragments/crawler_result.html",
        crawler_id=crawler_id,
        status=status,
        selectors_json=selectors_json,
        notice=notice,
        error=error,
        generation=generation,
        repair=repair,
        path=path,
        crawlers=crawler_rows(conn) if conn is not None else None,
    )


def _path_panel(conn: sqlite3.Connection, created: crawlers.CrawlerOut) -> dict[str, Any]:
    """등록할 때 정해진 경로와 그 근거. 낱말은 표와 같은 함수가 만든다.

    저장된 행을 다시 읽는 이유는 알아낸 상세 API 설정이 그 행에만 있기 때문이다. 응답의 모드만
    보고 적으면 `상세 API` 라고 써 놓고 어느 엔드포인트인지는 말하지 못한다.
    """
    row = conn.execute(
        "SELECT list_url, api_config_json, selectors_json FROM crawlers WHERE id = ?",
        (created.id,),
    ).fetchone()
    view = describe_path(
        list_mode=created.list_mode,
        detail_mode=created.detail_mode,
        list_url=str(row["list_url"]) if row is not None else "",
        api_config_json=row["api_config_json"] if row is not None else None,
        selectors_json=row["selectors_json"] if row is not None else None,
    )
    return {
        **asdict(view),
        # 무엇을 보고 그렇게 정했는지. 판정만 남기면 다음 사람이 처음부터 다시 잰다
        "evidence": created.path_evidence,
        "reason": created.path_reason,
        "failure": created.path_failure,
    }


def _created_notice(created: crawlers.CrawlerOut) -> str:
    notice = f"크롤러 {created.id}({created.name}) 를 등록했다."
    if created.default_company:
        notice = f"{notice} 회사명 {created.default_company} 를 함께 저장했다."
    if not created.detail_url:
        notice = f"{notice} 상세 URL 없이 목록만 보고 만들었다."
    return notice


@router.get("/ui/crawlers", response_class=HTMLResponse)
def crawler_list_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """등록된 크롤러 표. 페이지가 로드될 때와 등록 직후에 갈린다."""
    return render(request, "fragments/crawler_list.html", crawlers=crawler_rows(conn))


@router.post("/ui/crawlers", response_class=HTMLResponse)
async def create_crawler_fragment(
    request: Request,
    list_url: Annotated[str, Form()],
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
    generate: Annotated[crawlers.GenerateFn, Depends(crawlers.get_generator)],
    discover: Annotated[crawlers.DiscoverFn, Depends(crawlers.get_discoverer)],
    detail_url: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    default_company: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """생성 요청. 성공하면 결과 요약과 편집기가, 실패하면 사유가 결과 영역에 들어간다.

    **화면은 가져오는 방식을 묻지 않는다.** 정적으로 목록이 나오면 정적으로, 안 나오면 렌더로
    등록이 스스로 정하고, 무엇을 보고 그렇게 정했는지는 결과의 근거 문장에 적힌다
    (`app/api/crawlers.py` 의 `get_generator`, `app/selector/discovery.py`).

    정해진 경로는 제안이라 표에서 운영자가 바꿀 수 있고, 바꾼 값을 판정이 다시 덮어쓰지
    않는다 (`switch_collect_modes_fragment`).

    `detail_url` 은 선택이다. 상세를 JS 로 그려 주소가 따로 없는 사이트가 있고, 비우면 목록
    페이지만 보고 만든다. 그때 상세 셀렉터는 실패가 아니라 건너뜀으로 표시된다.

    `default_company` 는 모회사 이름이고 이 화면은 필수로 받는다(2026-08-29 결정). 비었으면
    셀렉터 생성(브라우저·모델 호출)을 시작하기 전에 여기서 거절한다 — 어차피 거절할 요청에
    그 비용을 쓰지 않는다. `crawlers.create_crawler` 자체는 이 값을 막지 않는다 — 그 함수를
    직접 쓰는 테스트·가져오기 경로까지 막으면 파급이 너무 크다(`app/api/crawlers.py`).
    """
    if not default_company.strip():
        return _result(
            request,
            error={
                "reason": "empty_parent_company",
                "message": (
                    "모회사 이름은 비울 수 없다. 계열사가 하나뿐인 사이트도 그 크롤러 이름을 "
                    "그대로 적는다 — 시스템이 대신 짐작하지 않는다"
                ),
            },
        )
    payload = crawlers.CrawlerCreate(
        list_url=list_url,
        detail_url=detail_url,
        name=name,
        default_company=default_company,
    )
    try:
        created = await crawlers.create_crawler(payload, conn, generate, discover)
    except HTTPException as exc:
        return _result(request, error=error_detail(exc))

    return _result(
        request,
        conn=conn,
        crawler_id=created.id,
        status=created.status,
        selectors_json=json.dumps(created.selectors.model_dump(), ensure_ascii=False, indent=2),
        notice=_created_notice(created),
        path=_path_panel(conn, created),
        generation={
            "matches": created.matches,
            "failed_fields": created.failed_fields,
            "skipped_fields": created.skipped_fields,
            "notes": created.notes,
            "usage": created.usage,
        },
    )


@router.post("/ui/crawlers/{crawler_id}/repair", response_class=HTMLResponse)
async def repair_selectors_fragment(
    request: Request,
    crawler_id: int,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
    repair: Annotated[crawlers.RepairFn, Depends(crawlers.get_repairer)],
    hint: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """AI 수정 버튼. 실패한 필드만 모델에게 다시 고르게 한다.

    `hint` 는 운영자가 브라우저에서 보고 준 단서다. 테스트 실행 화면의 입력칸과 같은
    매크로를 쓰고 같은 경로로 들어간다. 비워 두면 힌트 없이 돈다.

    붙여 넣은 경로가 그대로 저장되지 않는다. 모델은 그 자리를 찾는 데만 쓰고, 고친 셀렉터는
    지금 가져온 HTML 에 다시 돌려 판정한다 (`app/selector/repair.py`).

    저장하지 않는다. 고치기 전과 후를 나란히 보여주고, 고친 셀렉터를 편집기에 올려 둘 뿐이다.
    반영하는 것은 운영자가 누르는 "셀렉터 저장" 이고, 그 버튼이 `.claude/rules/llm.md` 가
    말하는 "요청" 이다. 누르기 전까지 `crawlers.selectors_json` 은 그대로다.

    실패하면 사유가 결과 영역에 남는다. Gemini 한도 초과(429)도 메시지에 코드가 들어 있어
    화면에 그대로 보인다.
    """
    try:
        result = await crawlers.repair_selectors(
            crawler_id, conn, repair, crawlers.RepairIn(hint=hint)
        )
    except HTTPException as exc:
        # 편집기를 유지하려면 지금 저장된 셀렉터를 다시 올려야 한다. 실패했다고 편집기가
        # 사라지면 운영자는 손으로 고칠 자리를 잃는다
        row = conn.execute(
            "SELECT status, selectors_json FROM crawlers WHERE id = ?", (crawler_id,)
        ).fetchone()
        return _result(
            request,
            crawler_id=crawler_id if row is not None else None,
            status=str(row["status"]) if row is not None else "",
            selectors_json=pretty_selectors(row["selectors_json"] or "") if row is not None else "",
            error=error_detail(exc),
        )

    return _result(
        request,
        crawler_id=result.id,
        status=result.status,
        # 고친 셀렉터를 편집기에 올린다. 저장은 아직이다
        selectors_json=json.dumps(result.selectors.model_dump(), ensure_ascii=False, indent=2),
        notice=_repair_notice(result),
        repair={
            "before_matches": result.before_matches,
            "after_matches": result.after_matches,
            "targets": result.targets,
            "repaired": result.repaired,
            "unresolved": result.unresolved,
            "failed_fields": result.failed_fields,
            "skipped_fields": result.skipped_fields,
            "changes": result.changes,
            "notes": result.notes,
            "usage": result.usage,
        },
    )


def _repair_notice(result: crawlers.RepairOut) -> str:
    """무엇을 고쳤고 무엇이 남았는지. 저장 전이라는 사실을 매번 적는다."""
    if result.mode == "hinted":
        # 실패한 필드가 없었다. 고친 것은 운영자가 힌트로 지적한 자리다
        parts = [f"크롤러 {result.id} 에 실패한 필드는 없었다. 힌트가 가리킨 자리를 물었다."]
    else:
        parts = [
            f"크롤러 {result.id} 의 실패한 필드 "
            f"{len(result.failed_targets)}개를 모델에게 다시 물었다."
        ]
    if result.repaired:
        parts.append(f"고쳐진 필드: {', '.join(result.repaired)}.")
    if result.unresolved:
        parts.append(f"고친 뒤에도 실패로 남은 필드: {', '.join(result.unresolved)}.")
    if not result.changes:
        parts.append("모델이 바꾼 셀렉터가 없다.")
    parts.append("아직 저장하지 않았다. 반영하려면 아래에서 셀렉터 저장을 누른다.")
    return " ".join(parts)


@router.put("/ui/crawlers/{crawler_id}/name", response_class=HTMLResponse)
def rename_crawler_fragment(
    request: Request,
    crawler_id: int,
    name: Annotated[str, Form()],
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """표에서 고친 이름을 저장한다.

    등록이 이름을 안 받으면 리스트 URL 의 호스트가 그대로 이름이 된다. 그 행을 다시 등록하면
    경로 판정이 다시 돌아 브라우저와 모델을 쓰므로, 이름만 고치는 길이 여기다
    (`app/api/crawlers.py` 의 `update_name`).
    """
    try:
        saved = crawlers.update_name(crawler_id, crawlers.NameUpdate(name=name), conn)
    except HTTPException as exc:
        return _result(request, conn=conn, error=error_detail(exc))

    return _result(
        request,
        conn=conn,
        notice=(
            f"크롤러 {saved.id} 의 이름을 {saved.name} 로 저장했다. "
            "이미 만들어진 워크플로우의 이름은 그대로다."
        ),
    )


@router.put("/ui/crawlers/{crawler_id}/collect-modes", response_class=HTMLResponse)
def switch_collect_modes_fragment(
    request: Request,
    crawler_id: int,
    list_mode: Annotated[str, Form()],
    detail_mode: Annotated[str, Form()],
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """표에서 고른 경로를 저장한다. 목록과 상세를 따로 정한다.

    운영자가 정한 값이다. 등록할 때의 자동 판정은 그 행을 만들 때 한 번 쓰이고, 저장된 뒤에
    다시 덮어쓰는 경로는 없다 (`app/api/crawlers.py` 의 `update_collect_modes`).
    """
    try:
        saved = crawlers.update_collect_modes(
            crawler_id,
            crawlers.CollectModesUpdate(list_mode=list_mode, detail_mode=detail_mode),
            conn,
        )
    except HTTPException as exc:
        return _result(request, conn=conn, error=error_detail(exc))

    # 낱말은 저장된 행 전체를 보고 정해진다. 모드 두 값만 보면 상세로 가는 법이 링크인지
    # 항목 속성인지 알 수 없어 `알 수 없음` 이 찍힌다
    rows = [row for row in crawler_rows(conn) if row["id"] == saved.id]
    view = (
        rows[0]["path"]
        if rows
        else describe_path(list_mode=saved.list_mode, detail_mode=saved.detail_mode)
    )
    return _result(
        request,
        conn=conn,
        notice=(
            f"크롤러 {saved.id} 의 경로를 {view.list_word} / {view.detail_word} 로 저장했다. "
            "그 경로로 실제로 가져와지는지 테스트 실행으로 확인한다."
        ),
    )


@router.delete("/ui/crawlers/{crawler_id}", response_class=HTMLResponse)
def delete_crawler_fragment(
    request: Request,
    crawler_id: int,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """표의 삭제 버튼. 되돌릴 수 없으므로 누르기 전 확인은 버튼의 `hx-confirm` 이 받는다.

    거절(409)도 여기서는 200 으로 나간다. 화면이 다시 판정하지 않고 API 가 돌려준 사유를
    그대로 적는다 — 승격 여부를 목록이 스스로 판정하면 표가 낡았을 때 실제와 어긋난다.
    성공이든 거절이든 목록을 다시 그려 지금 남아 있는 크롤러를 보여준다.
    """
    try:
        deleted = crawlers.delete_crawler(crawler_id, conn)
    except HTTPException as exc:
        return _result(request, conn=conn, error=error_detail(exc))

    notice = f"크롤러 {deleted.id}({deleted.name}) 를 지웠다. 되돌릴 수 없다."
    if deleted.deleted_test_runs:
        notice = f"{notice} 승격 전 테스트 실행 기록 {deleted.deleted_test_runs}건도 함께 지웠다."
    return _result(request, conn=conn, notice=notice)


@router.get("/ui/crawlers/{crawler_id}/editor", response_class=HTMLResponse)
def selector_editor_fragment(
    request: Request,
    crawler_id: int,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """저장된 셀렉터를 편집기에 그대로 올린다. 여는 것만으로 다시 생성하지 않는다."""
    row = conn.execute(
        "SELECT id, status, selectors_json FROM crawlers WHERE id = ?", (crawler_id,)
    ).fetchone()
    if row is None:
        return _result(
            request,
            error={"reason": "not_found", "message": f"크롤러 {crawler_id} 가 없다"},
        )
    return _result(
        request,
        crawler_id=int(row["id"]),
        status=str(row["status"]),
        selectors_json=pretty_selectors(row["selectors_json"] or ""),
    )


@router.put("/ui/crawlers/{crawler_id}/selectors", response_class=HTMLResponse)
def save_selectors_fragment(
    request: Request,
    crawler_id: int,
    selectors_json: Annotated[str, Form()],
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """손으로 고친 셀렉터를 그대로 저장한다. 거절되면 고친 내용을 편집기에 그대로 남긴다."""
    try:
        payload = json.loads(selectors_json)
    except json.JSONDecodeError as exc:
        return _result(
            request,
            crawler_id=crawler_id,
            selectors_json=selectors_json,
            error={"reason": "unparsable", "message": f"JSON 으로 읽을 수 없다: {exc}"},
        )

    try:
        saved = crawlers.update_selectors(crawler_id, payload, conn)
    except HTTPException as exc:
        return _result(
            request,
            crawler_id=crawler_id,
            selectors_json=selectors_json,
            error=error_detail(exc),
        )

    return _result(
        request,
        conn=conn,
        crawler_id=saved.id,
        status=saved.status,
        selectors_json=json.dumps(saved.selectors.model_dump(), ensure_ascii=False, indent=2),
        notice=f"크롤러 {saved.id} 의 셀렉터를 저장했다.",
    )
