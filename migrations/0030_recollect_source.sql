-- 이미 담은 공고의 원문을 다시 수집할 자리를 만든다 (2026-09-14 결정).
--
-- 수집 실행은 이미 아는 주소의 상세를 열지 않는다 (`app/crawler/runner.py` 의 `_collect`). 그래서
-- 원문을 만드는 방식이 바뀌어도 — 상세 API 응답 전체, 페이지 속 구조 데이터, 이미지 글 — 이미 담은
-- 공고에는 닿지 않는다. 원문 다시 수집(`app/crawler/recollect.py`)이 그 공고들의 상세를 다시
-- 가져와 **같은 행에 갈아 끼운다.**
--
-- | 바뀌는 것 | 왜 |
-- |---|---|
-- | `raw_job_history` 를 만든다 | 갈아 끼우기 전 값을 한 글자도 버리지 않는다. `raw_jobs` 는 지금까지 append-only 였고, 갈아 끼우는 곳은 이 기능 하나뿐이다 |
-- | `crawl_runs.trigger` 에 `recollect` 를 더한다 | 다시 수집한 실행을 주기·수동·테스트 실행과 가른다. 자동 중지의 연속 실패에 세지 않는다 |
--
-- 행을 새로 더하지 않고 갈아 끼우는 이유: 공고 번호(`raw_jobs.id`)에 사람 보정·제안·전달 표시·나눈
-- 공고가 붙어 있다. 새 행을 더하면 같은 공고의 정규화 행이 둘이 되어 소비 측에 중복으로 간다.
--
-- `trigger` 의 CHECK 는 ALTER 로 못 바꿔서 0010 의 `error_class` 처럼 새 칸을 만들어 옮긴다.
--
-- 되돌리기: 이력 표를 지운다. **갈아 끼운 원문은 되돌리지 않는다** — 그사이 사람이 새 원문에 맞춰
-- 고친 값이 있을 수 있어 어느 쪽이 맞는지 코드가 판단할 수 없다. 이력이 필요하면 되돌리기 전에 DB 를
-- 떠 둔다. `recollect` 로 남은 실행은 `manual` 로 적는다.

-- migrate:up
CREATE TABLE raw_job_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_job_id    INTEGER NOT NULL REFERENCES raw_jobs(id),
    -- 이 값을 갈아 끼운 원문 다시 수집 실행
    run_id        INTEGER REFERENCES crawl_runs(id),
    -- 갈아 끼우기 전 `raw_jobs` 의 세 칸 그대로
    raw_data_json TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    crawled_at    TEXT NOT NULL,
    replaced_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_raw_job_history_raw_job_id ON raw_job_history (raw_job_id);
CREATE INDEX idx_raw_job_history_run_id ON raw_job_history (run_id);

ALTER TABLE crawl_runs ADD COLUMN trigger_next TEXT CHECK (
    trigger_next IS NULL OR trigger_next IN ('schedule', 'manual', 'test', 'recollect')
);
UPDATE crawl_runs SET trigger_next = trigger;
ALTER TABLE crawl_runs DROP COLUMN trigger;
ALTER TABLE crawl_runs RENAME COLUMN trigger_next TO trigger;

-- migrate:down
UPDATE crawl_runs SET trigger = 'manual' WHERE trigger = 'recollect';
ALTER TABLE crawl_runs ADD COLUMN trigger_prev TEXT CHECK (
    trigger_prev IS NULL OR trigger_prev IN ('schedule', 'manual', 'test')
);
UPDATE crawl_runs SET trigger_prev = trigger;
ALTER TABLE crawl_runs DROP COLUMN trigger;
ALTER TABLE crawl_runs RENAME COLUMN trigger_prev TO trigger;

DROP INDEX idx_raw_job_history_run_id;
DROP INDEX idx_raw_job_history_raw_job_id;
DROP TABLE raw_job_history;
