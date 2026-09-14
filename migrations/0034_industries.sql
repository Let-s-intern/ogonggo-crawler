-- 0034 산업 분류표 (2026-09-14 결정)
--
-- 오공고(Spring) `Job.industry` 에 보낼 산업을 분류가 공고마다 이 표에서 고른다. 회사 단위로 두지
-- 않는다 — 오공고가 공고마다 산업을 받고, 틀린 것은 검수 화면에서 그 공고만 고친다.
--
-- 한 단계다. 씨앗은 잡코리아 산업 필터 11개이고(`seeds/industries-jobkorea-20260914.json`), 표가 비어
-- 있을 때 산업 분류 화면의 불러오기 단추로 넣는다. 지우는 기능은 두지 않는다 — 지우면 그 산업으로
-- 분류된 공고가 목록 밖의 값을 갖는다. 켜기·끄기만 둔다 (`app/industries.py`).
--
-- 분류 결과와 정규화에 `industry` 칸을 더하고, 사람 보정·제안 표가 그 칸을 받게 CHECK 를 넓힌다.
-- 이미 분류된 공고는 다시 분류할 때 산업이 채워진다.
--
-- 되돌리기: 산업 보정·제안 행과 칸과 표를 지운다. 이 표의 이름은 공고에 글자로 복사될 뿐 외래키가
-- 아니다.

-- migrate:up

CREATE TABLE industries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    enabled    INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

ALTER TABLE job_classifications ADD COLUMN industry TEXT;
ALTER TABLE normalized_jobs ADD COLUMN industry TEXT;

CREATE TABLE job_field_overrides_next (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_job_id INTEGER NOT NULL REFERENCES raw_jobs(id),
    part       INTEGER NOT NULL DEFAULT 1 CHECK (part >= 1),
    field_name TEXT NOT NULL CHECK (
                   field_name IN (
                       'company_name', 'title', 'department', 'recruitment_end_at', 'body', 'qualifications',
                       'recruitment_start_at', 'job_category', 'employment_type', 'experience_type',
                       'region', 'headcount', 'responsibilities', 'preferred_qualifications', 'hiring_process',
                       'recruitment_notice', 'job_field', 'job_role', 'company_and_team_introduction',
                       'compensation', 'benefits', 'education_level', 'recruitment_headcount',
                       'experience_min_years', 'closes_when_filled', 'application_method',
                       'industry'
                   )
               ),
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (raw_job_id, part, field_name)
);

INSERT INTO job_field_overrides_next (id, raw_job_id, part, field_name, value, created_at, updated_at)
SELECT id, raw_job_id, part, field_name, value, created_at, updated_at
  FROM job_field_overrides;

DROP TABLE job_field_overrides;
ALTER TABLE job_field_overrides_next RENAME TO job_field_overrides;

CREATE TABLE job_field_suggestions_next (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_job_id INTEGER NOT NULL REFERENCES raw_jobs(id),
    part       INTEGER NOT NULL DEFAULT 1 CHECK (part >= 1),
    field_name TEXT NOT NULL CHECK (
                   field_name IN (
                       'company_name', 'title', 'recruitment_end_at', 'body', 'qualifications',
                       'recruitment_start_at', 'employment_type', 'experience_type', 'region', 'responsibilities',
                       'preferred_qualifications', 'hiring_process', 'recruitment_notice', 'job_field',
                       'job_role', 'company_and_team_introduction', 'compensation', 'benefits',
                       'education_level', 'recruitment_headcount',
                       'experience_min_years', 'closes_when_filled', 'application_method',
                       'industry'
                   )
               ),
    value      TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (raw_job_id, part, field_name)
);

INSERT INTO job_field_suggestions_next (id, raw_job_id, part, field_name, value, reason, created_at)
SELECT id, raw_job_id, part, field_name, value, reason, created_at
  FROM job_field_suggestions;

DROP TABLE job_field_suggestions;
ALTER TABLE job_field_suggestions_next RENAME TO job_field_suggestions;

-- migrate:down

DELETE FROM job_field_suggestions WHERE field_name = 'industry';
CREATE TABLE job_field_suggestions_prev (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_job_id INTEGER NOT NULL REFERENCES raw_jobs(id),
    part       INTEGER NOT NULL DEFAULT 1 CHECK (part >= 1),
    field_name TEXT NOT NULL CHECK (
                   field_name IN (
                       'company_name', 'title', 'recruitment_end_at', 'body', 'qualifications',
                       'recruitment_start_at', 'employment_type', 'experience_type', 'region', 'responsibilities',
                       'preferred_qualifications', 'hiring_process', 'recruitment_notice', 'job_field',
                       'job_role', 'company_and_team_introduction', 'compensation', 'benefits',
                       'education_level', 'recruitment_headcount',
                       'experience_min_years', 'closes_when_filled', 'application_method'
                   )
               ),
    value      TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (raw_job_id, part, field_name)
);

INSERT INTO job_field_suggestions_prev (id, raw_job_id, part, field_name, value, reason, created_at)
SELECT id, raw_job_id, part, field_name, value, reason, created_at
  FROM job_field_suggestions;

DROP TABLE job_field_suggestions;
ALTER TABLE job_field_suggestions_prev RENAME TO job_field_suggestions;

DELETE FROM job_field_overrides WHERE field_name = 'industry';
CREATE TABLE job_field_overrides_prev (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_job_id INTEGER NOT NULL REFERENCES raw_jobs(id),
    part       INTEGER NOT NULL DEFAULT 1 CHECK (part >= 1),
    field_name TEXT NOT NULL CHECK (
                   field_name IN (
                       'company_name', 'title', 'department', 'recruitment_end_at', 'body', 'qualifications',
                       'recruitment_start_at', 'job_category', 'employment_type', 'experience_type',
                       'region', 'headcount', 'responsibilities', 'preferred_qualifications', 'hiring_process',
                       'recruitment_notice', 'job_field', 'job_role', 'company_and_team_introduction',
                       'compensation', 'benefits', 'education_level', 'recruitment_headcount',
                       'experience_min_years', 'closes_when_filled', 'application_method'
                   )
               ),
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (raw_job_id, part, field_name)
);

INSERT INTO job_field_overrides_prev (id, raw_job_id, part, field_name, value, created_at, updated_at)
SELECT id, raw_job_id, part, field_name, value, created_at, updated_at
  FROM job_field_overrides;

DROP TABLE job_field_overrides;
ALTER TABLE job_field_overrides_prev RENAME TO job_field_overrides;

ALTER TABLE normalized_jobs DROP COLUMN industry;
ALTER TABLE job_classifications DROP COLUMN industry;
DROP TABLE industries;
