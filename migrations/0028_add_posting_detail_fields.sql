-- 공고 본문에서 다섯 칸을 더 나눈다: 회사·팀 소개, 급여·처우, 복지·혜택, 학력, 모집인원.
--
-- 오공고가 받는 칸인데 크롤러가 채우지 못하던 것들이다. 오공고 `Job` 의
-- `companyAndTeamIntroduction`·`compensation`·`benefits`·`educationLevel`·`recruitmentHeadcount`
-- 와 이름을 맞춘다 — 전달할 때 옮겨 담는 표가 단순해진다 (2026-09-11 결정).
--
-- | 칸 | 분류 방식 |
-- |---|---|
-- | `company_and_team_introduction` | 뽑는 칸. 공고에 "회사 소개"·"팀 소개" 같은 소제목 구역이 있을 때만 |
-- | `compensation` | 뽑는 칸 |
-- | `benefits` | 뽑는 칸 |
-- | `recruitment_headcount` | 뽑는 칸. 적힌 그대로(`O명` 도 그대로). 숫자로 바꾸는 것은 전달할 때 |
-- | `education_level` | 판정 칸. 지원 자격이 요구하는 최소 학력. 언급이 없으면 빈 칸 |
--
-- 칸이 네 자리에 는다. 0017 이 `job_role` 을 더한 방법과 같다.
--
-- | 자리 | 왜 |
-- |---|---|
-- | `normalized_jobs` | 소비 측이 읽는 칸 |
-- | `job_classifications` | 분류가 낸 값이 먼저 앉는 자리 |
-- | `job_field_overrides.field_name` 의 CHECK | 사람이 고칠 수 있어야 한다 |
-- | `job_field_suggestions.field_name` 의 CHECK | `NORMALIZED_FIELDS` 전부를 받는다는 0023 의 약속 |
--
-- **모집인원은 0016 이 지운 `headcount` 를 되살리지 않는다.** `job_classifications.headcount`
-- 에는 0016 이전 분류기가 넣은 값이 남아 있을 수 있고, 그 값은 원문 그대로 옮겼는지 확인하지
-- 않은 값이다. 그 컬럼과 값은 지우지 않고 그대로 두며, 어디서도 읽지 않는다.
--
-- SQLite 는 CHECK 를 `ALTER TABLE` 로 못 바꿔서 0012·0017·0026 처럼 표를 다시 만든다 —
-- 새 CHECK 를 단 표를 만들고, 있는 행을 그대로 옮기고, 옛 표를 지우고, 이름을 되돌린다.
--
-- 적용 직후 다섯 칸은 기존 행 전부에서 NULL 이다. 값은 분류를 다시 돌려야 들어온다 — 이
-- 파일에 `normalized_jobs` 나 `job_classifications` 를 대상으로 하는 UPDATE 가 없다.
--
-- 되돌리기: CHECK 를 0026 의 목록으로 되돌리고 컬럼을 지운다. 다섯 칸에 걸린 보정·제안 행은
-- 옛 CHECK 에 담을 자리가 없어 떨어진다 — 0026 의 역적용이 직무 분류 보정을 거르는 것과 같다.

-- migrate:up
ALTER TABLE normalized_jobs ADD COLUMN company_and_team_introduction TEXT;
ALTER TABLE normalized_jobs ADD COLUMN compensation TEXT;
ALTER TABLE normalized_jobs ADD COLUMN benefits TEXT;
ALTER TABLE normalized_jobs ADD COLUMN education_level TEXT;
ALTER TABLE normalized_jobs ADD COLUMN recruitment_headcount TEXT;

ALTER TABLE job_classifications ADD COLUMN company_and_team_introduction TEXT;
ALTER TABLE job_classifications ADD COLUMN compensation TEXT;
ALTER TABLE job_classifications ADD COLUMN benefits TEXT;
ALTER TABLE job_classifications ADD COLUMN education_level TEXT;
ALTER TABLE job_classifications ADD COLUMN recruitment_headcount TEXT;

CREATE TABLE job_field_overrides_new (
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

INSERT INTO job_field_overrides_new (id, raw_job_id, field_name, value, created_at, updated_at)
SELECT id, raw_job_id, field_name, value, created_at, updated_at FROM job_field_overrides;

DROP TABLE job_field_overrides;

ALTER TABLE job_field_overrides_new RENAME TO job_field_overrides;

CREATE TABLE job_field_suggestions_new (
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

INSERT INTO job_field_suggestions_new (id, raw_job_id, field_name, value, reason, created_at)
SELECT id, raw_job_id, field_name, value, reason, created_at FROM job_field_suggestions;

DROP TABLE job_field_suggestions;

ALTER TABLE job_field_suggestions_new RENAME TO job_field_suggestions;

-- migrate:down
CREATE TABLE job_field_overrides_old (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_job_id INTEGER NOT NULL REFERENCES raw_jobs(id),
    field_name TEXT NOT NULL CHECK (
                   field_name IN (
                       'company', 'title', 'department', 'deadline', 'body', 'requirements',
                       'start_date', 'job_category', 'employment_type', 'career_level',
                       'work_location', 'headcount', 'duties', 'preferred', 'hiring_process',
                       'etc_info', 'job_role', 'job_major', 'job_minor'
                   )
               ),
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (raw_job_id, field_name)
);

-- 다섯 칸에 걸린 보정은 옛 CHECK 에 담을 자리가 없어 여기서 떨어진다
INSERT INTO job_field_overrides_old (id, raw_job_id, field_name, value, created_at, updated_at)
SELECT id, raw_job_id, field_name, value, created_at, updated_at
  FROM job_field_overrides
 WHERE field_name NOT IN ('company_and_team_introduction', 'compensation', 'benefits',
                          'education_level', 'recruitment_headcount');

DROP TABLE job_field_overrides;

ALTER TABLE job_field_overrides_old RENAME TO job_field_overrides;

CREATE TABLE job_field_suggestions_old (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_job_id INTEGER NOT NULL REFERENCES raw_jobs(id),
    field_name TEXT NOT NULL CHECK (
                   field_name IN (
                       'company', 'title', 'job_role', 'deadline', 'body', 'requirements',
                       'start_date', 'employment_type', 'career_level', 'work_location',
                       'duties', 'preferred', 'hiring_process', 'etc_info', 'job_major',
                       'job_minor'
                   )
               ),
    value      TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (raw_job_id, field_name)
);

INSERT INTO job_field_suggestions_old (id, raw_job_id, field_name, value, reason, created_at)
SELECT id, raw_job_id, field_name, value, reason, created_at
  FROM job_field_suggestions
 WHERE field_name NOT IN ('company_and_team_introduction', 'compensation', 'benefits',
                          'education_level', 'recruitment_headcount');

DROP TABLE job_field_suggestions;

ALTER TABLE job_field_suggestions_old RENAME TO job_field_suggestions;

ALTER TABLE job_classifications DROP COLUMN recruitment_headcount;
ALTER TABLE job_classifications DROP COLUMN education_level;
ALTER TABLE job_classifications DROP COLUMN benefits;
ALTER TABLE job_classifications DROP COLUMN compensation;
ALTER TABLE job_classifications DROP COLUMN company_and_team_introduction;

ALTER TABLE normalized_jobs DROP COLUMN recruitment_headcount;
ALTER TABLE normalized_jobs DROP COLUMN education_level;
ALTER TABLE normalized_jobs DROP COLUMN benefits;
ALTER TABLE normalized_jobs DROP COLUMN compensation;
ALTER TABLE normalized_jobs DROP COLUMN company_and_team_introduction;
