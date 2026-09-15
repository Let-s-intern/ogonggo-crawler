-- 0035 대표 이미지 (2026-09-15 결정)
--
-- 오공고(Spring) `Job.coverImageUrl` 에 보낼 대표 이미지를 정규화가 정해 `normalized_jobs.cover_image_url`
-- 에 저장한다. 회사 로고(자회사 → 모회사)가 먼저이고, 없으면 수집이 상세 페이지에서 읽은 `og:image`
-- (`raw_data_json.og_image_url`)다 (`app/normalize/engine.py` 의 `cover_image`).
--
-- 보내는 순간 계산하지 않고 저장하는 것은, 오공고로 가는 값이 전부 정규화 표 한 곳에서 나가고 검수
-- 화면에서 보고 고칠 수 있게 하려는 것이다. 운영자가 회사 로고를 나중에 저장하면 그 회사 공고를
-- 다시 정규화한다 (`app/api/ui_companies.py`). 사람 보정·제안 표가 이 칸을 받게 CHECK 를 넓힌다.
--
-- 이미 쌓인 공고는 재정규화할 때 로고가 채워지고, og:image 는 원문을 다시 수집해야 붙는다.
--
-- 되돌리기: 대표 이미지 보정·제안 행과 칸을 지운다.

-- migrate:up

ALTER TABLE normalized_jobs ADD COLUMN cover_image_url TEXT;

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
                       'industry',
                       'cover_image_url'
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
                       'industry',
                       'cover_image_url'
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

DELETE FROM job_field_suggestions WHERE field_name = 'cover_image_url';
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
                       'experience_min_years', 'closes_when_filled', 'application_method',
                       'industry'
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

DELETE FROM job_field_overrides WHERE field_name = 'cover_image_url';
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
                       'experience_min_years', 'closes_when_filled', 'application_method',
                       'industry'
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

ALTER TABLE normalized_jobs DROP COLUMN cover_image_url;
