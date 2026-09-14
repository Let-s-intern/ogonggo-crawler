-- 0032 분류 AI 규칙의 판 (2026-09-14 결정)
--
-- AI 규칙 화면에서 저장할 때마다 한 행이 쌓인다. 가장 최근 행이 지금 규칙이다. 되돌리기도 새 행으로
-- 저장하므로 행을 고치거나 지우는 경로가 없다 (`app/classify/prompt_rules.py`).
--
-- 분류 결과와 모델 호출 기록에 그 판 번호를 남긴다. 판을 하나도 저장하지 않았을 때 쓰는 코드의
-- 기본 규칙은 판 0 이다. 이 마이그레이션 전에 분류된 행과, 가리킬 판이 없는 호출(셀렉터 생성,
-- 저장하지 않은 규칙으로 돌린 시험)은 NULL 이다.

-- migrate:up

CREATE TABLE classify_rule_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rules_json TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

ALTER TABLE job_classifications ADD COLUMN rules_version INTEGER;
ALTER TABLE llm_calls ADD COLUMN rules_version INTEGER;

-- migrate:down

ALTER TABLE llm_calls DROP COLUMN rules_version;
ALTER TABLE job_classifications DROP COLUMN rules_version;
DROP TABLE classify_rule_versions;
