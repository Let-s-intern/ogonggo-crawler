-- 0061 운영자가 지운 공고의 주소 (2026-10-21 결정)
--
-- 공고 목록에서 지운 공고가 다음 날 다시 수집됐다. 수집은 `raw_jobs` 에 같은 주소가 있는지로 아는
-- 공고를 가리는데, 지우기가 그 행을 함께 지워서다. 지운 공고는 폐기한 것이다 — 지울 때 주소를 여기
-- 남기고, 수집은 여기 든 주소를 아는 공고로 본다 (`app/crawler/runner.py` 의 `_is_known`). 대상 직군
-- 밖이라 거른 주소(`screened_jobs`, 0055)와 같은 방식이다.
--
-- 다시 받으려면 그 행을 지운다. 워크플로우를 지우면 함께 지워진다.
--
-- 되돌리기: 표를 지운다.

-- migrate:up

CREATE TABLE discarded_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows (id) ON DELETE CASCADE,
    source_url  TEXT    NOT NULL,
    title       TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (workflow_id, source_url)
);

-- migrate:down

DROP TABLE discarded_jobs;
