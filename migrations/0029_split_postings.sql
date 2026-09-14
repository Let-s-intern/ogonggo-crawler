-- 공고 하나를 여러 공고로 나눠 담을 자리를 만든다. 네 표에 "몇 번째 공고" 번호(`part`)를 더한다.
--
-- 채용 사이트 공고 한 건에 직무가 여럿 든 경우가 있다 — 삼성은 한 공고에 직무 열두 개, LG 는
-- 서른 개쯤이다. 분류가 직무마다 공고 하나로 나누면 수집 건 하나(`raw_jobs`)에 분류·정규화
-- 결과가 여럿 붙는다 (2026-09-11 결정).
--
-- | 표 | 바뀌는 것 |
-- |---|---|
-- | `job_classifications` | `part`·`part_role`·`part_lines` 를 더한다. UNIQUE 가 `(raw_job_id)` 에서 `(raw_job_id, part)` 로 |
-- | `normalized_jobs` | `part` 를 더한다. 원래 UNIQUE 가 없어 칸만 는다 |
-- | `job_field_overrides` | `part` 를 더한다. UNIQUE 가 `(raw_job_id, part, field_name)` 으로 |
-- | `job_field_suggestions` | 위와 같다 |
--
-- **나누지 않은 공고는 1번 하나다.** 기존 행은 전부 1번이 되고 값은 그대로다. 기본값이 1 이라
-- 번호를 적지 않는 INSERT 도 지금처럼 1번에 들어간다.
--
-- `part_role` 은 나눈 직무의 이름이고 나누지 않은 공고는 NULL 이다. `part_lines` 는 긴 공고를
-- 직무마다 따로 부를 때 그 직무에 보낸 원문 줄 번호(JSON)다 — 다시 분류할 때 같은 줄을 보내
-- 나눈 목록을 고정한다. 한 번에 부른 공고는 NULL 이다.
--
-- UNIQUE 는 `ALTER TABLE` 로 못 바꿔서 0028 처럼 표를 다시 만든다.
--
-- 되돌리기: 2번 이후 행은 옛 UNIQUE 에 담을 자리가 없어 네 표 모두에서 떨어진다. 이미 전달된
-- 정규화 행이어도 지운다 — 수집 건 하나에 정규화 행이 여럿이면 옛 코드는 어느 것이 그 공고인지
-- 모른다.

-- migrate:up
ALTER TABLE normalized_jobs ADD COLUMN part INTEGER NOT NULL DEFAULT 1 CHECK (part >= 1);

CREATE TABLE job_classifications_new (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_job_id                    INTEGER NOT NULL REFERENCES raw_jobs(id),
    part                          INTEGER NOT NULL DEFAULT 1 CHECK (part >= 1),
    part_role                     TEXT,
    part_lines                    TEXT,
    job_category                  TEXT,
    work_location                 TEXT,
    career_level                  TEXT,
    employment_type               TEXT,
    headcount                     TEXT,
    duties                        TEXT,
    preferred                     TEXT,
    hiring_process                TEXT,
    requirements                  TEXT,
    department                    TEXT,
    etc_info                      TEXT,
    dropped_fields                TEXT NOT NULL DEFAULT '',
    model                         TEXT NOT NULL,
    classified_at                 TEXT NOT NULL DEFAULT (datetime('now')),
    evidence_json                 TEXT NOT NULL DEFAULT '{}',
    job_role                      TEXT,
    job_major                     TEXT,
    job_minor                     TEXT,
    company_and_team_introduction TEXT,
    compensation                  TEXT,
    benefits                      TEXT,
    education_level               TEXT,
    recruitment_headcount         TEXT,
    UNIQUE (raw_job_id, part)
);

INSERT INTO job_classifications_new (
    id, raw_job_id, job_category, work_location, career_level, employment_type, headcount,
    duties, preferred, hiring_process, requirements, department, etc_info, dropped_fields, model,
    classified_at, evidence_json, job_role, job_major, job_minor, company_and_team_introduction,
    compensation, benefits, education_level, recruitment_headcount
)
SELECT id, raw_job_id, job_category, work_location, career_level, employment_type, headcount,
       duties, preferred, hiring_process, requirements, department, etc_info, dropped_fields, model,
       classified_at, evidence_json, job_role, job_major, job_minor, company_and_team_introduction,
       compensation, benefits, education_level, recruitment_headcount
  FROM job_classifications;

DROP TABLE job_classifications;

ALTER TABLE job_classifications_new RENAME TO job_classifications;

CREATE TABLE job_field_overrides_new (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_job_id INTEGER NOT NULL REFERENCES raw_jobs(id),
    part       INTEGER NOT NULL DEFAULT 1 CHECK (part >= 1),
    field_name TEXT NOT NULL CHECK (
                   field_name IN (
                       'company', 'title', 'department', 'deadline', 'body', 'requirements',
                       'start_date', 'job_category', 'employment_type', 'career_level',
                       'work_location', 'headcount', 'duties', 'preferred', 'hiring_process',
                       'etc_info', 'job_role', 'job_major', 'job_minor',
                       'company_and_team_introduction', 'compensation', 'benefits',
                       'education_level', 'recruitment_headcount'
                   )
               ),
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (raw_job_id, part, field_name)
);

INSERT INTO job_field_overrides_new (id, raw_job_id, field_name, value, created_at, updated_at)
SELECT id, raw_job_id, field_name, value, created_at, updated_at FROM job_field_overrides;

DROP TABLE job_field_overrides;

ALTER TABLE job_field_overrides_new RENAME TO job_field_overrides;

CREATE TABLE job_field_suggestions_new (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_job_id INTEGER NOT NULL REFERENCES raw_jobs(id),
    part       INTEGER NOT NULL DEFAULT 1 CHECK (part >= 1),
    field_name TEXT NOT NULL CHECK (
                   field_name IN (
                       'company', 'title', 'job_role', 'deadline', 'body', 'requirements',
                       'start_date', 'employment_type', 'career_level', 'work_location',
                       'duties', 'preferred', 'hiring_process', 'etc_info', 'job_major',
                       'job_minor', 'company_and_team_introduction', 'compensation',
                       'benefits', 'education_level', 'recruitment_headcount'
                   )
               ),
    value      TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (raw_job_id, part, field_name)
);

INSERT INTO job_field_suggestions_new (id, raw_job_id, field_name, value, reason, created_at)
SELECT id, raw_job_id, field_name, value, reason, created_at FROM job_field_suggestions;

DROP TABLE job_field_suggestions;

ALTER TABLE job_field_suggestions_new RENAME TO job_field_suggestions;

-- migrate:down
CREATE TABLE job_field_suggestions_old (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_job_id INTEGER NOT NULL REFERENCES raw_jobs(id),
    field_name TEXT NOT NULL CHECK (
                   field_name IN (
                       'company', 'title', 'job_role', 'deadline', 'body', 'requirements',
                       'start_date', 'employment_type', 'career_level', 'work_location',
                       'duties', 'preferred', 'hiring_process', 'etc_info', 'job_major',
                       'job_minor', 'company_and_team_introduction', 'compensation',
                       'benefits', 'education_level', 'recruitment_headcount'
                   )
               ),
    value      TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (raw_job_id, field_name)
);

-- 2번 이후 제안은 옛 UNIQUE 에 담을 자리가 없어 여기서 떨어진다
INSERT INTO job_field_suggestions_old (id, raw_job_id, field_name, value, reason, created_at)
SELECT id, raw_job_id, field_name, value, reason, created_at
  FROM job_field_suggestions
 WHERE part = 1;

DROP TABLE job_field_suggestions;

ALTER TABLE job_field_suggestions_old RENAME TO job_field_suggestions;

CREATE TABLE job_field_overrides_old (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_job_id INTEGER NOT NULL REFERENCES raw_jobs(id),
    field_name TEXT NOT NULL CHECK (
                   field_name IN (
                       'company', 'title', 'department', 'deadline', 'body', 'requirements',
                       'start_date', 'job_category', 'employment_type', 'career_level',
                       'work_location', 'headcount', 'duties', 'preferred', 'hiring_process',
                       'etc_info', 'job_role', 'job_major', 'job_minor',
                       'company_and_team_introduction', 'compensation', 'benefits',
                       'education_level', 'recruitment_headcount'
                   )
               ),
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (raw_job_id, field_name)
);

INSERT INTO job_field_overrides_old (id, raw_job_id, field_name, value, created_at, updated_at)
SELECT id, raw_job_id, field_name, value, created_at, updated_at
  FROM job_field_overrides
 WHERE part = 1;

DROP TABLE job_field_overrides;

ALTER TABLE job_field_overrides_old RENAME TO job_field_overrides;

CREATE TABLE job_classifications_old (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_job_id      INTEGER NOT NULL UNIQUE REFERENCES raw_jobs(id),
    job_category    TEXT,
    work_location   TEXT,
    career_level    TEXT,
    employment_type TEXT,
    headcount       TEXT,
    duties          TEXT,
    preferred       TEXT,
    hiring_process  TEXT,
    requirements    TEXT,
    department      TEXT,
    etc_info        TEXT,
    dropped_fields  TEXT NOT NULL DEFAULT '',
    model           TEXT NOT NULL,
    classified_at   TEXT NOT NULL DEFAULT (datetime('now')),
    evidence_json   TEXT NOT NULL DEFAULT '{}',
    job_role        TEXT,
    job_major       TEXT,
    job_minor       TEXT,
    company_and_team_introduction TEXT,
    compensation    TEXT,
    benefits        TEXT,
    education_level TEXT,
    recruitment_headcount TEXT
);

INSERT INTO job_classifications_old (
    id, raw_job_id, job_category, work_location, career_level, employment_type, headcount,
    duties, preferred, hiring_process, requirements, department, etc_info, dropped_fields, model,
    classified_at, evidence_json, job_role, job_major, job_minor, company_and_team_introduction,
    compensation, benefits, education_level, recruitment_headcount
)
SELECT id, raw_job_id, job_category, work_location, career_level, employment_type, headcount,
       duties, preferred, hiring_process, requirements, department, etc_info, dropped_fields, model,
       classified_at, evidence_json, job_role, job_major, job_minor, company_and_team_introduction,
       compensation, benefits, education_level, recruitment_headcount
  FROM job_classifications
 WHERE part = 1;

DROP TABLE job_classifications;

ALTER TABLE job_classifications_old RENAME TO job_classifications;

DELETE FROM normalized_jobs WHERE part > 1;

ALTER TABLE normalized_jobs DROP COLUMN part;
