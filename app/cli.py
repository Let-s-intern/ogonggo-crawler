"""운영 명령. 마이그레이션, 오공고에 이미 보낸 공고의 원문 채우기, 짧은 본문 다시 읽기가 있다.

python -m app.cli migrate up
python -m app.cli migrate down --steps 1
python -m app.cli migrate status
python -m app.cli spring backfill-source-html [--after-job-id 0]
python -m app.cli enrich backlog [--run] [--limit 30]
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3

from app import db
from app.classify.batch import ClassifyProgress, classify_ids
from app.crawler import enrich
from app.deliver import spring


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="group", required=True)
    migrate = sub.add_parser("migrate", help="스키마 마이그레이션")
    migrate_sub = migrate.add_subparsers(dest="action", required=True)
    migrate_sub.add_parser("up", help="미적용 마이그레이션을 전부 적용")
    down = migrate_sub.add_parser("down", help="마지막 마이그레이션부터 역적용")
    down.add_argument("--steps", type=int, default=1)
    migrate_sub.add_parser("status", help="버전별 적용 여부")
    spring_parser = sub.add_parser("spring", help="오공고 전송")
    spring_sub = spring_parser.add_subparsers(dest="action", required=True)
    backfill = spring_sub.add_parser(
        "backfill-source-html", help="이미 보낸 공고에 원문만 채운다 (LC-3456)"
    )
    backfill.add_argument("--after-job-id", type=int, default=0, help="이 오공고 id 다음부터")
    enrich_parser = sub.add_parser("enrich", help="짧은 본문 다시 읽기 (app/crawler/enrich.py)")
    enrich_sub = enrich_parser.add_subparsers(dest="action", required=True)
    pending = enrich_sub.add_parser(
        "backlog", help="본문이 짧은 공고를 센다. --run 이면 다시 읽고 다시 분류한다"
    )
    pending.add_argument("--run", action="store_true", help="세기만 하지 않고 다시 읽는다")
    pending.add_argument("--limit", type=int, default=30, help="--run 이 한 번에 여는 공고 수")
    parser.add_argument("--database", default=None, help="기본값은 DATABASE_PATH")

    args = parser.parse_args(argv)
    conn = db.connect(args.database)
    try:
        if args.group == "spring":
            return _backfill(conn, args.after_job_id)
        if args.group == "enrich":
            return _enrich_backlog(conn, run=args.run, limit=args.limit)
        if args.action == "up":
            applied = db.migrate_up(conn)
            print("적용됨: " + (", ".join(applied) if applied else "없음 (이미 최신)"))
        elif args.action == "down":
            reverted = db.migrate_down(conn, steps=args.steps)
            print("역적용됨: " + (", ".join(reverted) if reverted else "없음"))
        else:
            for version, state in db.migration_status(conn):
                print(f"{version}\t{state}")
    finally:
        conn.close()
    return 0


def _backfill(conn: sqlite3.Connection, after_job_id: int) -> int:
    result = asyncio.run(spring.backfill_source_html(conn, after_job_id=after_job_id))
    if result.reason:
        print(result.reason)
        return 1
    print(
        f"보냄 {result.sent}건, 원문 없음 {result.empty}건, 오공고에 없음 {result.missing}건,"
        f" 실패 {result.failed}건"
    )
    for error in result.errors:
        print(error)
    return 1 if result.failed else 0


def _enrich_backlog(conn: sqlite3.Connection, *, run: bool, limit: int) -> int:
    """쌓인 짧은 공고를 다시 읽고, 갈아 끼운 공고를 다시 분류한다. 새 공고는 매일 수집이 본다.

    서버의 분류 자리(`ClassifyRun`)는 다른 프로세스라 잡지 못한다. 서버가 분류하는 동안 돌리면
    같은 공고를 두 번 분류할 수 있다.
    """
    picked = enrich.short_ids(conn)
    print(f"본문이 짧은 공고 {len(picked)}건")
    if not run or not picked:
        return 0
    results = asyncio.run(enrich.enrich_ids(conn, picked[: max(1, limit)]))
    for outcome in (enrich.ENRICHED, enrich.UNCHANGED, enrich.ERROR):
        print(f"  {outcome}: {sum(result.outcome == outcome for result in results)}건")
    enriched = [result.raw_job_id for result in results if result.outcome == enrich.ENRICHED]
    if not enriched:
        return 0
    progress = asyncio.run(classify_ids(conn, enriched, ClassifyProgress()))
    print(f"다시 분류 {progress.processed}/{len(enriched)}건, 실패 {progress.failed}건")
    for error in progress.errors:
        print(error)
    return 1 if progress.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
