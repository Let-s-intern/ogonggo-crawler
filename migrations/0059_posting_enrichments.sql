-- 0059 부실한 공고 다시 읽기 기록 (2026-10-21 결정)
--
-- 수집은 됐는데 본문이 거의 없는 공고(200자 미만)가 확인 필요에 쌓였다. 상세 셀렉터가 본문 영역을
-- 잘못 잡았거나, 본문을 JS 가 그리거나, 공고가 이미지뿐인 경우다. 그런 공고를 셀렉터 없이 페이지
-- 전체로 다시 읽고(글자가 모자라면 렌더, 본문이 짧으면 이미지 읽기) 더 긴 본문을 얻으면 갈아
-- 끼운다 (`app/crawler/enrich.py`).
--
-- 공고마다 한 번만 한다. 원래 짧은 공고가 있어 적어 두지 않으면 수집할 때마다 다시 연다.
--
-- | outcome | 무엇 |
-- |---|---|
-- | `enriched` | 더 긴 본문을 얻어 갈아 끼웠다. 이전 값은 `raw_job_history` 에 있다 |
-- | `unchanged` | 다시 읽어도 더 나은 본문이 없었다. 그대로 뒀다 |
-- | `error` | 페이지를 열지 못했다. 그대로 뒀다 |
--
-- 되돌리기: 표를 지운다.

-- migrate:up

CREATE TABLE posting_enrichments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_job_id    INTEGER NOT NULL UNIQUE REFERENCES raw_jobs (id) ON DELETE CASCADE,
    outcome       TEXT    NOT NULL CHECK (outcome IN ('enriched', 'unchanged', 'error')),
    before_chars  INTEGER NOT NULL,
    after_chars   INTEGER,
    message       TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- migrate:down

DROP TABLE posting_enrichments;
