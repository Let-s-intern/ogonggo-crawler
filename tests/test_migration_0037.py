"""0037 은 AI 분류가 덮는 칸에 걸린 켜진 규칙만 끄고, 되돌리면 그것만 다시 켠다.

| 확인 | 깨지면 |
|---|---|
| 수집 칸의 규칙은 그대로다 | 날짜 읽기 같은 쓰는 규칙이 꺼진다 |
| 분류 칸의 켜진 규칙이 꺼지고 메모에 표시가 붙는다 | 왜 꺼졌는지 모른다 |
| 원래 꺼져 있던 규칙은 되돌려도 꺼진 채다 | 되돌리기가 사람이 끈 규칙까지 켠다 |
"""

from __future__ import annotations

import pathlib
import sqlite3

from app import db

MARK = "[0037: AI 분류가 덮는 칸이라 껐다]"


def _rules(conn: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    return {
        str(row["field_name"]): (int(row["enabled"]), str(row["note"]))
        for row in conn.execute("SELECT field_name, enabled, note FROM normalization_rules")
    }


def test_분류_칸의_켜진_규칙만_끄고_되돌리면_그것만_켠다(tmp_path: pathlib.Path) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    try:
        db.migrate_up(conn)
        db.migrate_down(conn, steps=1)
        conn.execute(
            """
            INSERT INTO normalization_rules (field_name, rule_type, rule_config_json, enabled, note)
            VALUES ('recruitment_end_at', 'trim', '{}', 1, ''),
                   ('qualifications', 'trim', '{}', 1, '자격요건 앞말 지우기'),
                   ('employment_type', 'trim', '{}', 0, '')
            """
        )

        db.migrate_up(conn)
        assert _rules(conn) == {
            "recruitment_end_at": (1, ""),
            "qualifications": (0, f"자격요건 앞말 지우기 {MARK}"),
            "employment_type": (0, ""),
        }

        db.migrate_down(conn, steps=1)
        assert _rules(conn) == {
            "recruitment_end_at": (1, ""),
            "qualifications": (1, "자격요건 앞말 지우기"),
            "employment_type": (0, ""),
        }
    finally:
        conn.close()
