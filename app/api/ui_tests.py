"""테스트 실행 결과 화면의 조각 라우트.

실행은 `app/api/crawlers.py` 의 test-run 라우트를 그대로 부른다. 화면 전용 실행 경로를 따로
만들지 않는다 — 그러면 화면에서 통과한 셀렉터가 API 에서는 다르게 도는 상태가 생긴다.

## 무엇을 보여주는가

운영자가 이 화면을 여는 이유는 "어느 필드가 왜 비었나" 하나다. 그래서 실행 요약보다 필드별
표가 먼저 온다. 실패한 필드에는 사유를 같이 적는다.

판정 낱말은 셋이다 — `성공`, `실패`, `건너뜀`. 판정하지 않은 필드를 셀렉터 생성 화면은
`건너뜀`, 이 화면은 `해당 없음` 이라고 적던 것을 `건너뜀` 하나로 맞췄다 (Push 28).

| 화면에 적는 사유 | 판정 근거 |
|---|---|
| `셀렉터 없음` | 셀렉터가 빈 문자열이다. 사이트에 그 항목이 없다는 뜻이라 실패가 아니다 |
| `상세를 따라가지 않는다` | 목록 전용 크롤러다. 상세 필드에 값이 없는 것이 정상이다 |
| `selector_miss` | 셀렉터는 있는데 어느 항목에서도 값을 찾지 못했다 |
| `parse` | 일부 항목에서만 값을 읽었다 |

목록 전용은 `list.link` 와 `list.link_template` 이 둘 다 비어 있는 크롤러다
(`app/crawler/parser.py` 의 `list_only()`). 상세 페이지를 아예 열지 않으므로 상세 셀렉터가
무엇이든 값이 채워질 수 없고, 그것을 실패로 적으면 운영자가 고칠 수 없는 것을 고치려 든다.
`detail.title` 과 `detail.recruitment_end_at` 에 값이 있는 것은 실행이 목록에서 읽은 값을 그 자리에
넣기 때문이다 (`app/crawler/runner.py` 의 `_record`).

실행 전체가 실패한 경우의 사유는 `crawl_runs.error_class` 그대로다. 놓친 공고 하나하나는
`crawl_run_failures` 에서 읽는다 — 실행이 끝난 뒤에도 남아 있어서 워크플로우 화면이 같은
조각으로 같은 실패를 본다 (`app/api/ui_runs.py`).

## 모드를 바꾸는 것과 한 번 시험하는 것

이 화면은 두 가지를 따로 준다.

| 조작 | 하는 일 |
|---|---|
| 저장 모드 전환 | `crawlers.render_mode` 를 바꾼다. 워크플로우 실행이 이 값을 읽는다 |
| 이번 실행만 | 저장값을 그대로 두고 이 실행 한 번만 다른 경로로 돈다 |

정적으로 되는지 렌더가 필요한지 비교하는 것이 이 화면의 일이라, 시험할 때마다 저장값이 따라
바뀌면 비교가 안 된다. 필드별 매칭 수를 양쪽으로 뽑아 보고, 정할 때 저장 모드를 옮긴다.

## 결과에서 바로 고친다

실패는 주기 실행에서 나고, 확인은 이 화면에서 한다. 고치는 수단이 등록 화면에만 있으면
운영자는 방금 본 실패를 두고 화면을 옮겨야 하고, 돌아오면 그 실패가 화면에서 사라져 있다.
그래서 실패한 필드가 있으면 결과 아래에 AI 수정·셀렉터 편집·다시 실행이 함께 나온다.

고치기와 저장은 등록 화면과 같은 함수를 부른다(`app/api/crawlers.py`). 화면 전용 고치기
경로를 만들지 않는다 — 그러면 한쪽에서 저장한 셀렉터가 다른 쪽 판정과 어긋난다. 화면에
보이는 마크업도 같은 매크로다(`app/templates/fragments/selector_repair_macro.html`).

AI 수정은 저장하지 않는다. 전/후를 보여주고, 반영하는 것은 운영자가 누르는 "셀렉터 저장"
이다 (`.claude/rules/llm.md`).

상태는 단어로만 적는다. 아이콘·이모지를 쓰지 않는다 (`.claude/rules/writing.md`).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.api import crawlers
from app.api.ui import mode_word, render
from app.api.ui_crawlers import crawler_rows, error_detail, pretty_selectors
from app.api.ui_runs import run_failures
from app.crawler.fetcher import Fetcher
from app.crawler.parser import list_only
from app.crawler.runner import KNOWN
from app.selector.schema import SelectorSchemaError, validate_selectors

router = APIRouter(tags=["ui"], include_in_schema=False)

# 미리보기 표의 열 순서. `app/crawler/runner.py` 의 `_record()` 가 만드는 키 그대로다
FIELDS: tuple[tuple[str, str], ...] = (
    ("list_title", "list.title"),
    ("list_date", "list.date"),
    ("title", "detail.title"),
    ("body", "detail.body"),
    ("qualifications", "detail.qualifications"),
    ("recruitment_end_at", "detail.recruitment_end_at"),
    ("department", "detail.department"),
)

# 값이 길면 표가 읽히지 않는다. 자른 자리는 화면에 표시한다
PREVIEW_LIMIT = 120


def _selector_of(selectors: Any, path: str) -> str:
    """`list.title` 같은 경로로 저장된 셀렉터를 꺼낸다. 없으면 빈 문자열."""
    if selectors is None:
        return ""
    section, _, name = path.partition(".")
    return str(getattr(getattr(selectors, section), name, "") or "")


def _saved_selectors(conn: sqlite3.Connection, crawler_id: int) -> Any:
    """저장된 셀렉터. 읽을 수 없으면 None — 그 사유는 실행 결과가 이미 말한다."""
    row = conn.execute("SELECT selectors_json FROM crawlers WHERE id = ?", (crawler_id,)).fetchone()
    if row is None or not row["selectors_json"]:
        return None
    try:
        return validate_selectors(json.loads(row["selectors_json"]))
    except (json.JSONDecodeError, SelectorSchemaError):
        return None


def _field_report(items: list[Any], selectors: Any) -> list[dict[str, Any]]:
    """필드별 판정. 값이 있는 항목 수와 실패 사유를 함께 만든다."""
    counted = [item for item in items if item.state != KNOWN]
    total = len(counted)
    # 상세로 갈 길이 없는 크롤러다. 상세 필드가 비는 것이 정상이라 실패로 적지 않는다
    detail_skipped = selectors is not None and list_only(selectors.list)
    report: list[dict[str, Any]] = []

    for key, path in FIELDS:
        selector = _selector_of(selectors, path)
        filled = sum(1 for item in counted if item.fields.get(key, "").strip())

        if detail_skipped and path.startswith("detail."):
            state = "건너뜀"
            reason = "상세 페이지를 따라가지 않는 사이트다. 상세 셀렉터는 쓰이지 않는다"
            if filled:
                # 실행이 목록에서 읽은 값을 이 자리에 넣었다 (`app/crawler/runner.py`)
                reason = "상세 페이지를 따라가지 않는 사이트다. 이 값은 목록에서 읽은 것이다"
        elif not selector:
            state, reason = "건너뜀", "셀렉터 없음 (사이트에 그 항목이 없다)"
        elif total == 0:
            state, reason = "실패", "판정할 항목이 없다"
        elif filled == 0:
            state, reason = "실패", "selector_miss — 어느 항목에서도 값을 찾지 못했다"
        elif filled < total:
            state, reason = "실패", f"parse — {total - filled}건에서 값을 읽지 못했다"
        else:
            state, reason = "성공", ""

        report.append(
            {
                "path": path,
                "selector": selector,
                "filled": filled,
                "total": total,
                "state": state,
                "reason": reason,
            }
        )
    return report


@router.get("/ui/test-targets", response_class=HTMLResponse)
def test_targets_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """실행할 크롤러 목록. 등록 화면과 같은 목록을 읽는다."""
    return render(
        request, "fragments/test_targets.html", crawlers=crawler_rows(conn), mode_word=mode_word
    )


@router.put("/ui/test-targets/{crawler_id}/render-mode", response_class=HTMLResponse)
def switch_render_mode_fragment(
    request: Request,
    crawler_id: int,
    render_mode: Annotated[str, Form()],
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """저장된 모드를 바꾼다. 등록 화면으로 돌아가지 않아도 되게 이 표에서 부른다.

    셀렉터는 그대로 둔다. 렌더된 DOM 이 정적 HTML 과 다를 수 있어서, 바꾼 뒤에는 그 모드로
    한 번 실행해 봐야 안다. 한 번만 시험하는 것은 이 경로가 아니라 실행 폼의 모드 선택이다.
    """
    try:
        saved = crawlers.update_render_mode(
            crawler_id, crawlers.RenderModeUpdate(render_mode=render_mode), conn
        )
    except HTTPException as exc:
        detail = error_detail(exc)
        return render(
            request,
            "fragments/test_targets.html",
            crawlers=crawler_rows(conn),
            mode_word=mode_word,
            notice=f"저장 모드를 바꾸지 못했다: {detail['message']}",
        )

    return render(
        request,
        "fragments/test_targets.html",
        crawlers=crawler_rows(conn),
        mode_word=mode_word,
        notice=(
            f"크롤러 {saved.id} 의 저장 모드를 {mode_word(saved.render_mode)}로 바꿨다. "
            "그 모드로 한 번 실행해 확인한다."
        ),
    )


@router.post("/ui/crawlers/{crawler_id}/test-run", response_class=HTMLResponse)
async def test_run_fragment(
    request: Request,
    crawler_id: int,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
    fetcher: Annotated[Fetcher, Depends(crawlers.get_crawl_fetcher)],
    limit: Annotated[int, Form()] = 3,
    render_mode: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """저장된 셀렉터로 1회 실행하고 결과 영역만 갈아 끼운다.

    `render_mode` 가 비어 있으면 저장된 모드로 돈다. 값이 있으면 이번 실행만 그 경로로 돌고
    저장값은 그대로다.
    """
    if not 1 <= limit <= 20:
        return render(
            request,
            "fragments/test_result.html",
            error={"reason": "invalid_limit", "message": "상세 건수는 1 이상 20 이하여야 한다"},
        )

    try:
        result = await crawlers.test_run(crawler_id, conn, fetcher, limit, render_mode)
    except HTTPException as exc:
        return render(request, "fragments/test_result.html", error=error_detail(exc))

    report = _field_report(result.items, _saved_selectors(conn, crawler_id))
    return render(
        request,
        "fragments/test_result.html",
        result=result,
        # 놓친 공고는 실행 기록에서 읽는다. 워크플로우 화면이 여는 것과 같은 조각·같은 함수다
        # (`app/api/ui_runs.py`)
        failures=run_failures(conn, result.run_id),
        fields=FIELDS,
        report=report,
        preview_limit=PREVIEW_LIMIT,
        targets=crawler_rows(conn),
        mode_word=mode_word,
        # 수정 자리는 늘 붙는다. 실행이 성공이어도 잡히는 값이 틀릴 수 있고, 그때 고칠 길이
        # 없으면 운영자는 화면에서 막힌다. 고칠 대상이 없으면 그 사실을 조각이 적는다
        crawler_id=crawler_id,
        status=_crawler_status(conn, crawler_id),
        failed_fields=failed_fields_of(report),
        run_failed=run_failed(result),
        selectors_json=_stored_selectors_json(conn, crawler_id),
        limit=limit,
    )


def failed_fields_of(report: list[dict[str, Any]]) -> list[str]:
    """필드별 판정에서 실패로 적힌 이름. `건너뜀` 은 실패가 아니라 빠진다."""
    return [row["path"] for row in report if row["state"] == "실패"]


def run_failed(result: crawlers.TestRunOut) -> bool:
    """실행 자체가 실패했는가. 필드 표에 안 나오는 실패가 여기서 보인다.

    `list.item` 과 `list.link` 가 그렇다 — 항목을 하나도 못 잡거나 상세 URL 을 만들지
    못하면 표에는 빈 줄만 남고 사유는 실행 요약과 항목별 실패에 적힌다.
    """
    return bool(result.matched == 0 or result.fail_count or result.status != "success")


def _stored_selectors_json(conn: sqlite3.Connection, crawler_id: int) -> str:
    """편집기에 올릴 지금 저장된 셀렉터. 없으면 빈 문자열이다."""
    row = conn.execute("SELECT selectors_json FROM crawlers WHERE id = ?", (crawler_id,)).fetchone()
    if row is None:
        return ""
    return pretty_selectors(str(row["selectors_json"] or ""))


def _crawler_status(conn: sqlite3.Connection, crawler_id: int) -> str:
    row = conn.execute("SELECT status FROM crawlers WHERE id = ?", (crawler_id,)).fetchone()
    return str(row["status"]) if row is not None else ""


def repair_panel(
    request: Request,
    conn: sqlite3.Connection,
    crawler_id: int,
    *,
    failed_fields: list[str] | None = None,
    run_failed: bool = False,
    repair: dict[str, Any] | None = None,
    notice: str = "",
    error: dict[str, str] | None = None,
    selectors_json: str | None = None,
    limit: int = 3,
) -> HTMLResponse:
    """결과 아래의 수정 자리 하나. 고치기·저장·다시 실행이 전부 이 조각에 있다."""
    return render(
        request,
        "fragments/test_repair.html",
        crawler_id=crawler_id,
        status=_crawler_status(conn, crawler_id),
        failed_fields=failed_fields or [],
        run_failed=run_failed,
        repair=repair,
        notice=notice,
        error=error,
        selectors_json=(
            _stored_selectors_json(conn, crawler_id) if selectors_json is None else selectors_json
        ),
        limit=limit,
    )


@router.post("/ui/tests/{crawler_id}/repair", response_class=HTMLResponse)
async def repair_fragment(
    request: Request,
    crawler_id: int,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
    repair: Annotated[crawlers.RepairFn, Depends(crawlers.get_repairer)],
    hint: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """결과 화면의 AI 수정. 등록 화면과 같은 함수를 부르고 같은 표를 그린다.

    `hint` 는 운영자가 브라우저에서 보고 준 단서다. 비워 두면 힌트 없이 돈다. 무엇을 넣든
    그대로 저장되지 않는다 — 위치 단서로 프롬프트에 실릴 뿐이고, 고친 셀렉터는 지금 가져온
    HTML 에 다시 돌려 판정한다 (`app/selector/repair.py`).

    저장하지 않는다. 전/후를 보여주고 편집기에 올려 둘 뿐이다.
    """
    try:
        result = await crawlers.repair_selectors(
            crawler_id, conn, repair, crawlers.RepairIn(hint=hint)
        )
    except HTTPException as exc:
        # 편집기를 유지한다. 못 고쳤다고 손으로 고칠 자리까지 사라지면 안 된다
        return repair_panel(request, conn, crawler_id, error=error_detail(exc))

    return repair_panel(
        request,
        conn,
        crawler_id,
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
        failed_fields=result.failed_fields,
        notice=_repair_notice(result, hint),
        # 고친 셀렉터를 편집기에 올린다. 저장은 아직이다
        selectors_json=json.dumps(result.selectors.model_dump(), ensure_ascii=False, indent=2),
    )


def _repair_notice(result: crawlers.RepairOut, hint: str) -> str:
    """무엇을 고쳤고 무엇이 남았는지. 저장 전이라는 사실을 매번 적는다."""
    if result.mode == "hinted":
        # 실패한 필드가 없었다. 고친 것은 운영자가 힌트로 지적한 자리다
        parts = [f"크롤러 {result.id} 에 실패한 필드는 없었다. 힌트가 가리킨 자리를 물었다."]
    else:
        parts = [
            f"크롤러 {result.id} 의 실패한 필드 "
            f"{len(result.failed_targets)}개를 모델에게 다시 물었다."
        ]
        if hint.strip():
            parts.append("운영자 힌트를 함께 보냈다.")
    if result.repaired:
        parts.append(f"고쳐진 필드: {', '.join(result.repaired)}.")
    if result.unresolved:
        parts.append(f"고친 뒤에도 실패로 남은 필드: {', '.join(result.unresolved)}.")
    if not result.changes:
        parts.append("모델이 바꾼 셀렉터가 없다.")
    parts.append("아직 저장하지 않았다. 반영하려면 아래에서 셀렉터 저장을 누른다.")
    return " ".join(parts)


@router.put("/ui/tests/{crawler_id}/selectors", response_class=HTMLResponse)
def save_selectors_fragment(
    request: Request,
    crawler_id: int,
    selectors_json: Annotated[str, Form()],
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """이 화면에서 저장한다. 등록 화면과 같은 함수를 부른다.

    저장한 뒤에도 자리를 옮기지 않는다. 같은 조각이 다시 들어오고, 그 안의 "테스트 다시 실행"
    으로 방금 저장한 셀렉터를 바로 돌려 볼 수 있다.
    """
    try:
        payload = json.loads(selectors_json)
    except json.JSONDecodeError as exc:
        return repair_panel(
            request,
            conn,
            crawler_id,
            selectors_json=selectors_json,
            error={"reason": "unparsable", "message": f"JSON 으로 읽을 수 없다: {exc}"},
        )

    try:
        saved = crawlers.update_selectors(crawler_id, payload, conn)
    except HTTPException as exc:
        return repair_panel(
            request, conn, crawler_id, selectors_json=selectors_json, error=error_detail(exc)
        )

    return repair_panel(
        request,
        conn,
        crawler_id,
        selectors_json=json.dumps(saved.selectors.model_dump(), ensure_ascii=False, indent=2),
        notice=(
            f"크롤러 {saved.id} 의 셀렉터를 저장했다. "
            "아래에서 테스트를 다시 실행해 이 셀렉터가 맞는지 확인한다."
        ),
    )
