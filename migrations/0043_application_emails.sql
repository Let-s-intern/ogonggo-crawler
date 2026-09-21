-- 0043 지원 접수 이메일과 채용 문의 이메일을 수집한다 (2026-09-21 결정)
--
-- 오공고가 받는 칸이다 (`applicationEmail`, `inquiryEmail`, ogonggo-server LC-3318). 공고에 적힌
-- 이메일 주소를 분류가 원문에서 그대로 옮기고, 원문에 그 주소가 실제로 있을 때만 남긴다
-- (`app/classify/grounding.py`). 정규화는 분류 결과를 그대로 싣고, 사람이 검수 화면에서 고칠 수
-- 있다. 사람 보정·제안 표가 이 두 칸을 받게 CHECK 를 넓힌다 — 0035 와 같은 방법이다.
--
-- 이미 쌓인 공고는 다시 분류해야 채워진다.
--
-- 되돌리기: 두 칸의 보정·제안 행과 네 칸을 지운다.

-- migrate:up

ALTER TABLE job_classifications ADD COLUMN application_email TEXT;
ALTER TABLE job_classifications ADD COLUMN inquiry_email TEXT;
ALTER TABLE normalized_jobs ADD COLUMN application_email TEXT;
ALTER TABLE normalized_jobs ADD COLUMN inquiry_email TEXT;

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
                       'cover_image_url',
                       'application_email', 'inquiry_email'
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
                       'cover_image_url',
                       'application_email', 'inquiry_email'
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

DELETE FROM job_field_suggestions WHERE field_name IN ('application_email', 'inquiry_email');
DELETE FROM job_field_overrides WHERE field_name IN ('application_email', 'inquiry_email');

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
                       'industry',
                       'cover_image_url'
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
                       'industry',
                       'cover_image_url'
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

ALTER TABLE normalized_jobs DROP COLUMN inquiry_email;
ALTER TABLE normalized_jobs DROP COLUMN application_email;
ALTER TABLE job_classifications DROP COLUMN inquiry_email;
ALTER TABLE job_classifications DROP COLUMN application_email;
