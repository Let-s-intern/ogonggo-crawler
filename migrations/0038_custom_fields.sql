-- 0038 운영자가 화면에서 더하는 수집 항목 (2026-09-17 결정, LC-3344)
--
-- 오공고로 보내는 칸은 코드와 오공고 서버가 함께 정한다. 그 밖에 운영자가 공고에서 더 보고 싶은 칸을
-- 배포 없이 더하는 자리다. 값은 크롤러 안에만 남고 오공고로 보내지 않는다 (`app/custom_fields.py`).
--
-- | 표 | 무엇 |
-- |---|---|
-- | `custom_fields` | 항목 정의. 이름·AI 에게 주는 설명·값 형식·보기(줄마다 하나)·켜짐 |
-- | `job_custom_values` | 공고(수집 건·번호)마다 항목 값. 나눈 공고는 번호마다 따로다 |
--
-- 되돌리기: 두 표를 지운다. 채운 값도 함께 사라진다.

-- migrate:up

CREATE TABLE custom_fields (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL UNIQUE,
    instruction TEXT NOT NULL DEFAULT '',
    value_type  TEXT NOT NULL DEFAULT 'text'
                CHECK (value_type IN ('text', 'long', 'date', 'number', 'choice')),
    choices     TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE job_custom_values (
    raw_job_id INTEGER NOT NULL REFERENCES raw_jobs(id),
    part       INTEGER NOT NULL DEFAULT 1 CHECK (part >= 1),
    field_id   INTEGER NOT NULL REFERENCES custom_fields(id),
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (raw_job_id, part, field_id)
);

-- migrate:down

DROP TABLE job_custom_values;

DROP TABLE custom_fields;
