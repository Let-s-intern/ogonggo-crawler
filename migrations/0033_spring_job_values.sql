-- 0033 판정 값과 모집 일시를 오공고(Spring) 값으로 옮긴다 (2026-09-14 결정)
--
-- 크롤러가 모은 공고를 오공고로 보내므로 저장하는 값도 오공고 `Job` 이 받는 모양에 맞춘다.
--
-- | 칸 | 전 | 후 |
-- |---|---|---|
-- | 고용 형태·경력 구분·학력 | 한글(`정규직`) | 오공고 enum 이름(`FULL_TIME`) |
-- | 마감 일시 | `YYYY-MM-DD`, 없으면 `상시모집` | `YYYY-MM-DD HH:MM:SS`, 없으면 NULL |
-- | 시작 일시 | `YYYY-MM-DD` | `YYYY-MM-DD HH:MM:SS` |
--
-- 새 칸: 분류가 채우는 최소 경력 연수·채용 시 마감·지원 방법(사람 보정·제안 표도 받는다), 정규화가
-- 마감일에서 정하는 모집 유형·자동 종료. 새 칸은 이미 분류된 공고를 다시 분류하거나 재정규화할 때 채워진다. 모집 인원을
-- 숫자로 바꾸는 것도 재정규화가 한다 (`app/normalize/engine.py` 의 `settle_fields`).
--
-- 시각 없이 날짜만 있던 마감일은 그날 23:59:59, 시작일은 00:00:00 이다. 옛 값의 한글 목록 밖 값
-- (`Permanent` 같은 옛 매핑 값)은 그대로 둔다 — 무엇을 뜻하는지 이 마이그레이션이 정할 수 없다.
--
-- 정규화 규칙: 마감일 규칙 중 시각을 떼던 regex 를 끄고, 두 일시 칸의 date_parse 가 시각이 있는
-- 표기도 읽고 시각까지 쓰게 한다. 시작일 규칙이 하나도 없으면 마감일 규칙을 그대로 복사한다 —
-- 정규화가 마감일 칸의 기간(`A ~ B`) 앞쪽을 시작일 원문으로 쓴다. 옛 DB 파일을 가져올 때 같은
-- 번역을 하는 곳이 `app/field_values.py` 다.
--
-- 되돌리기는 손실이 있다. 한글 목록에 없던 PART_TIME 은 기타로, BOTH 는 무관으로 돌아간다. 새
-- 칸의 값과 보정은 지운다. 복사한 시작일 규칙은 마감일 규칙과 글자가 같은 것만 지운다.

-- migrate:up

-- 1. 새 칸
ALTER TABLE job_classifications ADD COLUMN experience_min_years TEXT;
ALTER TABLE job_classifications ADD COLUMN closes_when_filled TEXT;
ALTER TABLE job_classifications ADD COLUMN application_method TEXT;

ALTER TABLE normalized_jobs ADD COLUMN experience_min_years TEXT;
ALTER TABLE normalized_jobs ADD COLUMN closes_when_filled TEXT;
ALTER TABLE normalized_jobs ADD COLUMN application_method TEXT;
ALTER TABLE normalized_jobs ADD COLUMN recruitment_type TEXT;
ALTER TABLE normalized_jobs ADD COLUMN auto_close_enabled TEXT;

-- 2. 판정 값
UPDATE job_classifications
   SET employment_type = CASE employment_type
                             WHEN '정규직' THEN 'FULL_TIME'
                             WHEN '계약직' THEN 'CONTRACT'
                             WHEN '인턴' THEN 'INTERN'
                             WHEN '기타' THEN 'ETC'
                             ELSE employment_type END,
       experience_type = CASE experience_type
                             WHEN '신입' THEN 'NEWCOMER'
                             WHEN '경력' THEN 'EXPERIENCED'
                             WHEN '무관' THEN 'IRRELEVANT'
                             ELSE experience_type END,
       education_level = CASE education_level
                             WHEN '무관' THEN 'ANY'
                             WHEN '고졸' THEN 'HIGH_SCHOOL'
                             WHEN '전문학사' THEN 'ASSOCIATE'
                             WHEN '학사' THEN 'BACHELOR'
                             WHEN '석사' THEN 'MASTER'
                             WHEN '박사' THEN 'DOCTORATE'
                             ELSE education_level END;

UPDATE normalized_jobs
   SET employment_type = CASE employment_type
                             WHEN '정규직' THEN 'FULL_TIME'
                             WHEN '계약직' THEN 'CONTRACT'
                             WHEN '인턴' THEN 'INTERN'
                             WHEN '기타' THEN 'ETC'
                             ELSE employment_type END,
       experience_type = CASE experience_type
                             WHEN '신입' THEN 'NEWCOMER'
                             WHEN '경력' THEN 'EXPERIENCED'
                             WHEN '무관' THEN 'IRRELEVANT'
                             ELSE experience_type END,
       education_level = CASE education_level
                             WHEN '무관' THEN 'ANY'
                             WHEN '고졸' THEN 'HIGH_SCHOOL'
                             WHEN '전문학사' THEN 'ASSOCIATE'
                             WHEN '학사' THEN 'BACHELOR'
                             WHEN '석사' THEN 'MASTER'
                             WHEN '박사' THEN 'DOCTORATE'
                             ELSE education_level END;

-- 3. 모집 일시와 모집 유형
UPDATE normalized_jobs SET recruitment_end_at = NULL WHERE recruitment_end_at = '상시모집';
UPDATE normalized_jobs
   SET recruitment_end_at = recruitment_end_at || ' 23:59:59'
 WHERE recruitment_end_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]';
UPDATE normalized_jobs
   SET recruitment_start_at = recruitment_start_at || ' 00:00:00'
 WHERE recruitment_start_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]';
UPDATE normalized_jobs
   SET recruitment_type = CASE WHEN recruitment_end_at IS NULL THEN 'ALWAYS_OPEN' ELSE 'PERIOD' END,
       auto_close_enabled = CASE WHEN recruitment_end_at IS NULL THEN 'false' ELSE 'true' END;

-- 4. 사람 보정. 새 칸을 고칠 수 있게 CHECK 를 넓히고, 같은 번역을 값에 한다
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
                       'experience_min_years', 'closes_when_filled', 'application_method'
                   )
               ),
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (raw_job_id, part, field_name)
);

INSERT INTO job_field_overrides_next (id, raw_job_id, part, field_name, value, created_at, updated_at)
SELECT id, raw_job_id, part, field_name,
       CASE
           WHEN field_name = 'employment_type' THEN CASE value
               WHEN '정규직' THEN 'FULL_TIME'
               WHEN '계약직' THEN 'CONTRACT'
               WHEN '인턴' THEN 'INTERN'
               WHEN '기타' THEN 'ETC'
               ELSE value END
           WHEN field_name = 'experience_type' THEN CASE value
               WHEN '신입' THEN 'NEWCOMER'
               WHEN '경력' THEN 'EXPERIENCED'
               WHEN '무관' THEN 'IRRELEVANT'
               ELSE value END
           WHEN field_name = 'education_level' THEN CASE value
               WHEN '무관' THEN 'ANY'
               WHEN '고졸' THEN 'HIGH_SCHOOL'
               WHEN '전문학사' THEN 'ASSOCIATE'
               WHEN '학사' THEN 'BACHELOR'
               WHEN '석사' THEN 'MASTER'
               WHEN '박사' THEN 'DOCTORATE'
               ELSE value END
           WHEN field_name = 'recruitment_end_at' AND value = '상시모집' THEN ''
           WHEN field_name = 'recruitment_end_at'
                AND value GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' THEN value || ' 23:59:59'
           WHEN field_name = 'recruitment_start_at'
                AND value GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' THEN value || ' 00:00:00'
           ELSE value
       END,
       created_at, updated_at
  FROM job_field_overrides;

DROP TABLE job_field_overrides;
ALTER TABLE job_field_overrides_next RENAME TO job_field_overrides;

-- 4-2. 제안. 정규화 칸이면 무엇이든 받는 표라 같은 칸을 더한다
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
                       'experience_min_years', 'closes_when_filled', 'application_method'
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

-- 5. 정규화 규칙
-- 5-1. 마감 시각을 떼던 regex 를 끈다
UPDATE normalization_rules
   SET enabled = 0
 WHERE field_name = 'recruitment_end_at'
   AND rule_type = 'regex'
   AND CASE WHEN json_valid(rule_config_json)
            THEN json_extract(rule_config_json, '$.pattern') = '\s*\d{1,2}\s*:\s*\d{2}(\s*:\s*\d{2})?\s*$'
            ELSE 0 END;

-- 5-2. 두 일시 칸의 date_parse 가 시각까지 쓴다
UPDATE normalization_rules
   SET rule_config_json = json_set(rule_config_json, '$.output_format', '%Y-%m-%d %H:%M:%S')
 WHERE field_name IN ('recruitment_end_at', 'recruitment_start_at')
   AND rule_type = 'date_parse'
   AND json_valid(rule_config_json);

-- 5-3. 시각이 있는 표기를 읽는 형식을 뒤에 더한다. 이미 있는 형식은 더하지 않는다
UPDATE normalization_rules
   SET rule_config_json = json_insert(rule_config_json, '$.formats[#]', '%Y-%m-%d %H:%M')
 WHERE field_name IN ('recruitment_end_at', 'recruitment_start_at') AND rule_type = 'date_parse'
   AND CASE WHEN json_valid(rule_config_json)
            THEN json_type(rule_config_json, '$.formats') = 'array'
                 AND NOT EXISTS (SELECT 1 FROM json_each(rule_config_json, '$.formats') WHERE value = '%Y-%m-%d %H:%M')
            ELSE 0 END;
UPDATE normalization_rules
   SET rule_config_json = json_insert(rule_config_json, '$.formats[#]', '%Y-%m-%d %H:%M:%S')
 WHERE field_name IN ('recruitment_end_at', 'recruitment_start_at') AND rule_type = 'date_parse'
   AND CASE WHEN json_valid(rule_config_json)
            THEN json_type(rule_config_json, '$.formats') = 'array'
                 AND NOT EXISTS (SELECT 1 FROM json_each(rule_config_json, '$.formats') WHERE value = '%Y-%m-%d %H:%M:%S')
            ELSE 0 END;
UPDATE normalization_rules
   SET rule_config_json = json_insert(rule_config_json, '$.formats[#]', '%Y.%m.%d %H:%M')
 WHERE field_name IN ('recruitment_end_at', 'recruitment_start_at') AND rule_type = 'date_parse'
   AND CASE WHEN json_valid(rule_config_json)
            THEN json_type(rule_config_json, '$.formats') = 'array'
                 AND NOT EXISTS (SELECT 1 FROM json_each(rule_config_json, '$.formats') WHERE value = '%Y.%m.%d %H:%M')
            ELSE 0 END;
UPDATE normalization_rules
   SET rule_config_json = json_insert(rule_config_json, '$.formats[#]', '%Y.%m.%d %H:%M:%S')
 WHERE field_name IN ('recruitment_end_at', 'recruitment_start_at') AND rule_type = 'date_parse'
   AND CASE WHEN json_valid(rule_config_json)
            THEN json_type(rule_config_json, '$.formats') = 'array'
                 AND NOT EXISTS (SELECT 1 FROM json_each(rule_config_json, '$.formats') WHERE value = '%Y.%m.%d %H:%M:%S')
            ELSE 0 END;
UPDATE normalization_rules
   SET rule_config_json = json_insert(rule_config_json, '$.formats[#]', '%Y/%m/%d %H:%M')
 WHERE field_name IN ('recruitment_end_at', 'recruitment_start_at') AND rule_type = 'date_parse'
   AND CASE WHEN json_valid(rule_config_json)
            THEN json_type(rule_config_json, '$.formats') = 'array'
                 AND NOT EXISTS (SELECT 1 FROM json_each(rule_config_json, '$.formats') WHERE value = '%Y/%m/%d %H:%M')
            ELSE 0 END;
UPDATE normalization_rules
   SET rule_config_json = json_insert(rule_config_json, '$.formats[#]', '%Y.%m.%d. %H:%M')
 WHERE field_name IN ('recruitment_end_at', 'recruitment_start_at') AND rule_type = 'date_parse'
   AND CASE WHEN json_valid(rule_config_json)
            THEN json_type(rule_config_json, '$.formats') = 'array'
                 AND NOT EXISTS (SELECT 1 FROM json_each(rule_config_json, '$.formats') WHERE value = '%Y.%m.%d. %H:%M')
            ELSE 0 END;
UPDATE normalization_rules
   SET rule_config_json = json_insert(rule_config_json, '$.formats[#]', '%Y년 %m월 %d일 %H:%M')
 WHERE field_name IN ('recruitment_end_at', 'recruitment_start_at') AND rule_type = 'date_parse'
   AND CASE WHEN json_valid(rule_config_json)
            THEN json_type(rule_config_json, '$.formats') = 'array'
                 AND NOT EXISTS (SELECT 1 FROM json_each(rule_config_json, '$.formats') WHERE value = '%Y년 %m월 %d일 %H:%M')
            ELSE 0 END;

-- 5-4. 시작일 규칙이 없으면 마감일 규칙을 복사한다
INSERT INTO normalization_rules (field_name, rule_type, rule_config_json, priority, enabled, note)
SELECT 'recruitment_start_at', rule_type, rule_config_json, priority, enabled, note
  FROM normalization_rules
 WHERE field_name = 'recruitment_end_at'
   AND NOT EXISTS (SELECT 1 FROM normalization_rules WHERE field_name = 'recruitment_start_at')
 ORDER BY id;

-- migrate:down

-- 5. 정규화 규칙. 복사한 시작일 규칙부터 지운다 — 마감일 규칙을 되돌리면 글자가 달라져 찾지 못한다
DELETE FROM normalization_rules
 WHERE field_name = 'recruitment_start_at'
   AND EXISTS (
       SELECT 1 FROM normalization_rules AS e
        WHERE e.field_name = 'recruitment_end_at'
          AND e.rule_type = normalization_rules.rule_type
          AND e.rule_config_json = normalization_rules.rule_config_json
          AND e.priority = normalization_rules.priority
   );

UPDATE normalization_rules
   SET rule_config_json = json_set(
           json_set(rule_config_json, '$.output_format', '%Y-%m-%d'),
           '$.formats',
           json((SELECT json_group_array(value)
                   FROM json_each(rule_config_json, '$.formats')
                  WHERE value NOT IN ('%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%Y.%m.%d %H:%M',
                                      '%Y.%m.%d %H:%M:%S', '%Y/%m/%d %H:%M', '%Y.%m.%d. %H:%M',
                                      '%Y년 %m월 %d일 %H:%M')))
       )
 WHERE field_name IN ('recruitment_end_at', 'recruitment_start_at')
   AND rule_type = 'date_parse'
   AND CASE WHEN json_valid(rule_config_json)
            THEN json_type(rule_config_json, '$.formats') = 'array'
            ELSE 0 END;

UPDATE normalization_rules
   SET enabled = 1
 WHERE field_name = 'recruitment_end_at'
   AND rule_type = 'regex'
   AND CASE WHEN json_valid(rule_config_json)
            THEN json_extract(rule_config_json, '$.pattern') = '\s*\d{1,2}\s*:\s*\d{2}(\s*:\s*\d{2})?\s*$'
            ELSE 0 END;

-- 4-2. 제안
DELETE FROM job_field_suggestions
 WHERE field_name IN ('experience_min_years', 'closes_when_filled', 'application_method');

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
                       'education_level', 'recruitment_headcount'
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

-- 4. 사람 보정
DELETE FROM job_field_overrides
 WHERE field_name IN ('experience_min_years', 'closes_when_filled', 'application_method');

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
                       'compensation', 'benefits', 'education_level', 'recruitment_headcount'
                   )
               ),
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (raw_job_id, part, field_name)
);

INSERT INTO job_field_overrides_prev (id, raw_job_id, part, field_name, value, created_at, updated_at)
SELECT id, raw_job_id, part, field_name,
       CASE
           WHEN field_name = 'employment_type' THEN CASE value
               WHEN 'FULL_TIME' THEN '정규직'
               WHEN 'CONTRACT' THEN '계약직'
               WHEN 'INTERN' THEN '인턴'
               WHEN 'PART_TIME' THEN '기타'
               WHEN 'ETC' THEN '기타'
               ELSE value END
           WHEN field_name = 'experience_type' THEN CASE value
               WHEN 'NEWCOMER' THEN '신입'
               WHEN 'EXPERIENCED' THEN '경력'
               WHEN 'BOTH' THEN '무관'
               WHEN 'IRRELEVANT' THEN '무관'
               ELSE value END
           WHEN field_name = 'education_level' THEN CASE value
               WHEN 'ANY' THEN '무관'
               WHEN 'HIGH_SCHOOL' THEN '고졸'
               WHEN 'ASSOCIATE' THEN '전문학사'
               WHEN 'BACHELOR' THEN '학사'
               WHEN 'MASTER' THEN '석사'
               WHEN 'DOCTORATE' THEN '박사'
               ELSE value END
           WHEN field_name IN ('recruitment_end_at', 'recruitment_start_at')
                AND value GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'
                THEN substr(value, 1, 10)
           ELSE value
       END,
       created_at, updated_at
  FROM job_field_overrides;

DROP TABLE job_field_overrides;
ALTER TABLE job_field_overrides_prev RENAME TO job_field_overrides;

-- 3. 모집 일시
UPDATE normalized_jobs
   SET recruitment_end_at = substr(recruitment_end_at, 1, 10)
 WHERE recruitment_end_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]';
UPDATE normalized_jobs
   SET recruitment_start_at = substr(recruitment_start_at, 1, 10)
 WHERE recruitment_start_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]';
UPDATE normalized_jobs SET recruitment_end_at = '상시모집' WHERE recruitment_end_at IS NULL;

-- 2. 판정 값
UPDATE normalized_jobs
   SET employment_type = CASE employment_type
                             WHEN 'FULL_TIME' THEN '정규직'
                             WHEN 'CONTRACT' THEN '계약직'
                             WHEN 'INTERN' THEN '인턴'
                             WHEN 'PART_TIME' THEN '기타'
                             WHEN 'ETC' THEN '기타'
                             ELSE employment_type END,
       experience_type = CASE experience_type
                             WHEN 'NEWCOMER' THEN '신입'
                             WHEN 'EXPERIENCED' THEN '경력'
                             WHEN 'BOTH' THEN '무관'
                             WHEN 'IRRELEVANT' THEN '무관'
                             ELSE experience_type END,
       education_level = CASE education_level
                             WHEN 'ANY' THEN '무관'
                             WHEN 'HIGH_SCHOOL' THEN '고졸'
                             WHEN 'ASSOCIATE' THEN '전문학사'
                             WHEN 'BACHELOR' THEN '학사'
                             WHEN 'MASTER' THEN '석사'
                             WHEN 'DOCTORATE' THEN '박사'
                             ELSE education_level END;

UPDATE job_classifications
   SET employment_type = CASE employment_type
                             WHEN 'FULL_TIME' THEN '정규직'
                             WHEN 'CONTRACT' THEN '계약직'
                             WHEN 'INTERN' THEN '인턴'
                             WHEN 'PART_TIME' THEN '기타'
                             WHEN 'ETC' THEN '기타'
                             ELSE employment_type END,
       experience_type = CASE experience_type
                             WHEN 'NEWCOMER' THEN '신입'
                             WHEN 'EXPERIENCED' THEN '경력'
                             WHEN 'BOTH' THEN '무관'
                             WHEN 'IRRELEVANT' THEN '무관'
                             ELSE experience_type END,
       education_level = CASE education_level
                             WHEN 'ANY' THEN '무관'
                             WHEN 'HIGH_SCHOOL' THEN '고졸'
                             WHEN 'ASSOCIATE' THEN '전문학사'
                             WHEN 'BACHELOR' THEN '학사'
                             WHEN 'MASTER' THEN '석사'
                             WHEN 'DOCTORATE' THEN '박사'
                             ELSE education_level END;

-- 1. 새 칸
ALTER TABLE normalized_jobs DROP COLUMN auto_close_enabled;
ALTER TABLE normalized_jobs DROP COLUMN recruitment_type;
ALTER TABLE normalized_jobs DROP COLUMN application_method;
ALTER TABLE normalized_jobs DROP COLUMN closes_when_filled;
ALTER TABLE normalized_jobs DROP COLUMN experience_min_years;

ALTER TABLE job_classifications DROP COLUMN application_method;
ALTER TABLE job_classifications DROP COLUMN closes_when_filled;
ALTER TABLE job_classifications DROP COLUMN experience_min_years;
