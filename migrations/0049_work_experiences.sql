-- 0049 미래내일 일경험 프로그램을 수집해 오공고 채용공고로 보낸다 (2026-10-02 결정, LC-3432)
--
-- 청년일경험 포털(yw.work24.go.kr)의 미래내일 일경험 프로그램이다. 사이트가 하나이고 목록·상세가 POST 로만
-- 열려 공고 파이프라인(raw_jobs → 분류 → normalized_jobs → 전송)에 싣지 않고 새싹 부트캠프처럼 전용
-- 수집기(`app/work_experience/`)를 둔다. 프로그램 한 건이 행 하나다. 카드·상세에서 읽은 값, AI 가 채운
-- 칸, 오공고 전송 상태가 한 행에 있다. 오공고에는 고용 형태가 미래내일 일경험(`WORK_EXPERIENCE`)인
-- 채용공고로 들어간다.
--
-- 한 번 모아 AI 정리까지 끝난 프로그램은 상세를 다시 받지 않는다. 목록 카드의 값(card_hash)이 바뀌었을
-- 때만 — 모집기간이 늘었거나 인원이 바뀌었을 때 — 상세를 다시 받는다. page_hash 는 카드와 상세에서 읽은
-- 값 전체의 해시로, 정리(filled_hash)·전송(sent_hash)이 어느 판에 대한 것인지 가른다.
--
-- 되돌리기: 두 표를 지운다.

-- migrate:up

CREATE TABLE work_experiences (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url             TEXT NOT NULL UNIQUE,
    external_id            TEXT NOT NULL,
    type_code              TEXT NOT NULL DEFAULT '',
    type_label             TEXT NOT NULL DEFAULT '',
    title                  TEXT NOT NULL,
    company                TEXT NOT NULL DEFAULT '',
    operator               TEXT NOT NULL DEFAULT '',
    job                    TEXT NOT NULL DEFAULT '',
    region_label           TEXT NOT NULL DEFAULT '',
    headcount              INTEGER,
    recruitment_start_date TEXT,
    recruitment_end_date   TEXT NOT NULL,
    work_start_date        TEXT,
    work_end_date          TEXT,
    sections_json          TEXT NOT NULL DEFAULT '{}',
    card_hash              TEXT NOT NULL,
    page_hash              TEXT NOT NULL,
    fill_json              TEXT,
    filled_hash            TEXT,
    fill_error             TEXT NOT NULL DEFAULT '',
    spring_job_id          INTEGER,
    sent_hash              TEXT,
    sent_logo_url          TEXT,
    send_status            TEXT CHECK (send_status IN ('sent', 'failed')),
    send_attempts          INTEGER NOT NULL DEFAULT 0,
    send_error             TEXT NOT NULL DEFAULT '',
    sent_at                TEXT,
    first_seen_at          TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at             TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE work_experience_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger       TEXT NOT NULL CHECK (trigger IN ('schedule', 'manual')),
    status        TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    listed_count  INTEGER NOT NULL DEFAULT 0,
    new_count     INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    filled_count  INTEGER NOT NULL DEFAULT 0,
    sent_count    INTEGER NOT NULL DEFAULT 0,
    failed_count  INTEGER NOT NULL DEFAULT 0,
    notes         TEXT NOT NULL DEFAULT '',
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT
);

-- migrate:down

DROP TABLE work_experience_runs;
DROP TABLE work_experiences;
