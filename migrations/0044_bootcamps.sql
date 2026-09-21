-- 0044 새싹(SeSAC) 부트캠프를 수집해 오공고로 보낸다 (2026-09-22 결정, LC-3364)
--
-- 공고 파이프라인(raw_jobs → normalized_jobs → 분류 → 전송)은 오공고 Job 칸에 맞춰져 있어 부트캠프를
-- 싣지 않는다. 부트캠프 사이트는 새싹 하나라 전용 수집기(`app/bootcamp/`)를 따로 두고, 과정 한 건을
-- 행 하나에 담는다. 파서가 읽은 값, AI 가 채운 글 칸, 오공고 전송 상태가 한 행에 있다.
--
-- page_hash 는 파서가 읽은 값 전체의 해시다. 바뀌면 AI 가 다시 채우고(filled_hash) 오공고에 다시
-- 보낸다(sent_hash). 새싹은 모집기간을 늘리며 교육개요 이미지를 갈아 끼우는 일이 있다.
--
-- 되돌리기: 두 표를 지운다.

-- migrate:up

CREATE TABLE bootcamps (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url             TEXT NOT NULL UNIQUE,
    site                   TEXT NOT NULL DEFAULT 'sesac',
    external_id            TEXT NOT NULL,
    title                  TEXT NOT NULL,
    campus                 TEXT NOT NULL DEFAULT '',
    category               TEXT NOT NULL DEFAULT '',
    status_label           TEXT NOT NULL DEFAULT '',
    recruitment_start_date TEXT,
    recruitment_end_date   TEXT,
    program_start_date     TEXT NOT NULL,
    program_end_date       TEXT NOT NULL,
    hours                  INTEGER,
    thumbnail_url          TEXT NOT NULL DEFAULT '',
    overview_text          TEXT NOT NULL DEFAULT '',
    overview_images_json   TEXT NOT NULL DEFAULT '[]',
    curriculum_json        TEXT NOT NULL DEFAULT '[]',
    page_hash              TEXT NOT NULL,
    short_description      TEXT,
    content                TEXT,
    eligibility            TEXT,
    capacity               INTEGER,
    manager_email          TEXT,
    filled_hash            TEXT,
    fill_error             TEXT NOT NULL DEFAULT '',
    spring_bootcamp_id     INTEGER,
    sent_hash              TEXT,
    send_status            TEXT CHECK (send_status IN ('sent', 'failed')),
    send_attempts          INTEGER NOT NULL DEFAULT 0,
    send_error             TEXT NOT NULL DEFAULT '',
    sent_at                TEXT,
    first_seen_at          TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at             TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE bootcamp_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    site          TEXT NOT NULL DEFAULT 'sesac',
    trigger       TEXT NOT NULL CHECK (trigger IN ('schedule', 'manual')),
    status        TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    listed_count  INTEGER NOT NULL DEFAULT 0,
    new_count     INTEGER NOT NULL DEFAULT 0,
    changed_count INTEGER NOT NULL DEFAULT 0,
    filled_count  INTEGER NOT NULL DEFAULT 0,
    sent_count    INTEGER NOT NULL DEFAULT 0,
    failed_count  INTEGER NOT NULL DEFAULT 0,
    notes         TEXT NOT NULL DEFAULT '',
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT
);

-- migrate:down

DROP TABLE bootcamp_runs;
DROP TABLE bootcamps;
