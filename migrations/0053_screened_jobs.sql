-- 0053 수집에서 대상 직군 밖이라 거른 공고 (2026-10-08 결정, LC-3444)
--
-- 오공고에는 마케팅·인사·기획·영업·개발 공고만 올린다. 수집이 목록 제목으로 한 번, 상세 본문으로
-- 한 번 AI 에게 물어 대상 밖이면 `raw_jobs` 에 넣지 않는다 (`app/crawler/screen.py`).
--
-- 거른 주소를 어디에도 남기지 않으면 다음 실행이 같은 공고를 모르는 공고로 보고 상세를 다시 열고 AI 에게
-- 다시 묻는다. 쪽을 넘기는 목록은 아는 공고만 있는 쪽에서 멈추는데, 거른 공고가 모르는 공고면 매번 더
-- 깊이 내려간다. 그래서 여기 남긴 주소는 수집이 아는 공고로 본다.
--
-- `stage` 는 어디서 걸렀는가다. `title` 은 목록 제목으로, `body` 는 상세 본문으로 걸렀다.
-- 대상 직군을 바꿔도 여기 든 공고를 되살리지는 않는다. 다시 보려면 행을 지운다.
--
-- 되돌리기: 표를 지운다.

-- migrate:up

CREATE TABLE screened_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows (id) ON DELETE CASCADE,
    source_url  TEXT    NOT NULL,
    title       TEXT    NOT NULL DEFAULT '',
    stage       TEXT    NOT NULL CHECK (stage IN ('title', 'body')),
    reason      TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (workflow_id, source_url)
);

-- migrate:down

DROP TABLE screened_jobs;
