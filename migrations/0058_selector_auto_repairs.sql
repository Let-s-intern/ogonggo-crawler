-- 0058 셀렉터 자동 수리 기록 (2026-10-21 결정)
--
-- 수집이 셀렉터 때문에 실패하면 운영자가 화면에서 AI 고치기 → 저장 → 시험 수집을 눌렀다. 그것을
-- 자동으로 잇는다 (`app/crawler/auto_repair.py`). 사이트 추가의 시험 수집이 실패했을 때와 매일
-- 수집이 셀렉터 문제로 실패했을 때 돈다. 고친 셀렉터로 시험 수집이 되면 남기고, 안 되면 원래
-- 셀렉터와 수집 모드로 되돌린다.
--
-- 셀렉터가 운영자 모르게 바뀌므로 무엇을 무엇으로 바꿨는지 행마다 남긴다. 되돌릴 때 이 행의
-- `before_json` 을 쓴다. 매일 수집은 크롤러마다 하루 한 번만 고친다 — 이 표로 센다.
--
-- | trigger | 언제 |
-- |---|---|
-- | `register` | 사이트 추가의 시험 수집이 실패했다 |
-- | `schedule` | 매일 수집이 셀렉터 문제로 실패했다 |
--
-- | outcome | 무엇 |
-- |---|---|
-- | `repaired` | 고친 셀렉터로 시험 수집이 됐다. 새 셀렉터가 남았다 |
-- | `reverted` | 고쳤지만 시험 수집이 안 됐다. 원래대로 되돌렸다 |
-- | `no_change` | AI 가 바꾼 셀렉터가 없다. 손대지 않았다 |
-- | `error` | 고치기나 시험 수집을 돌리지 못했다. 원래대로다 |
--
-- 되돌리기: 표를 지운다.

-- migrate:up

CREATE TABLE selector_auto_repairs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    crawler_id    INTEGER NOT NULL REFERENCES crawlers (id) ON DELETE CASCADE,
    trigger       TEXT    NOT NULL CHECK (trigger IN ('register', 'schedule')),
    error_class   TEXT,
    outcome       TEXT    NOT NULL CHECK (outcome IN ('repaired', 'reverted', 'no_change', 'error')),
    message       TEXT    NOT NULL DEFAULT '',
    before_json   TEXT,
    after_json    TEXT,
    before_mode   TEXT,
    after_mode    TEXT,
    test_run_id   INTEGER,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_selector_auto_repairs_crawler ON selector_auto_repairs (crawler_id, created_at);

-- migrate:down

DROP INDEX idx_selector_auto_repairs_crawler;
DROP TABLE selector_auto_repairs;
