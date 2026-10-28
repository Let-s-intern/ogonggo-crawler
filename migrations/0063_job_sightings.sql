-- 0063 상시 채용 공고를 목록에서 본 기록 (2026-10-28 결정)
--
-- 상시 채용에는 마감일이 없어 마감된 공고가 계속 모집 중으로 남는다. 매일 수집이 목록을 읽을 때
-- 상시 채용 공고가 거기 있는지 적고, 연달아 안 보이면 마감으로 본다 (`app/crawler/sightings.py`).
--
-- 셋을 더한다.
--
-- - `crawlers.list_complete`: 목록 한 장에 진행 중인 공고가 다 나오는 사이트인가. 첫 쪽만 읽는
--   사이트에서 안 보이는 것은 마감이 아니라 다음 쪽으로 밀린 것이라 판정하지 않는다. 기본은
--   꺼짐이고, 목록을 쪽으로 나누지 않는 greetinghr 만 켜서 시작한다. 2026-10-23 운영 조사에서
--   greetinghr 는 한 장에 150건 넘게도 나왔고, recruiter.co.kr 은 8~10건씩 잘렸다
-- - `crawl_runs.matched`: 목록에서 읽은 항목 수. 로그에만 있던 값이다. 평소보다 크게 줄어든 날은
--   셀렉터가 반쯤 깨진 날일 수 있어 판정하지 않는다
-- - `job_sightings`: 공고마다 마지막으로 본 때와 연달아 안 보인 횟수. `raw_jobs` 는 추가만
--   하므로(`.claude/rules/data-safety.md`) 따로 둔다. `source_url` 은 목록에서 읽은 링크 그대로라
--   `raw_jobs.source_url` 과 같다
--
-- 되돌리기: 표를 지우고 두 칸을 지운다.

-- migrate:up

ALTER TABLE crawlers ADD COLUMN list_complete INTEGER NOT NULL DEFAULT 0;

UPDATE crawlers SET list_complete = 1 WHERE list_url LIKE '%.career.greetinghr.com%';

ALTER TABLE crawl_runs ADD COLUMN matched INTEGER;

CREATE TABLE job_sightings (
    workflow_id   INTEGER NOT NULL REFERENCES workflows (id) ON DELETE CASCADE,
    source_url    TEXT    NOT NULL,
    last_seen_at  TEXT,
    missed_runs   INTEGER NOT NULL DEFAULT 0,
    -- 연달아 안 보여 마감으로 본 때. 다시 보이면 비운다
    closed_at     TEXT,
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (workflow_id, source_url)
);

-- migrate:down

DROP TABLE job_sightings;

ALTER TABLE crawl_runs DROP COLUMN matched;

ALTER TABLE crawlers DROP COLUMN list_complete;
