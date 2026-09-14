-- 0031 칸 이름을 오공고(Spring) Job 엔티티의 칼럼 이름에 맞춘다 (2026-09-14 결정)
--
-- 크롤러가 모은 공고를 오공고로 보내므로 이름을 한 벌로 맞춘다. 저장 표만이 아니라 수집 층까지다 —
-- 사이트별 셀렉터(`crawlers.selectors_json`, `crawlers.api_config_json`)의 칸 이름, 수집 원본
-- (`raw_jobs.raw_data_json`, `raw_job_history.raw_data_json`)의 키, 분류 결과, 사람 보정, 제안,
-- 정규화 규칙의 대상 칸이 모두 새 이름을 쓴다.
--
-- | 옛 이름 | 새 이름 |
-- |---|---|
-- | company | company_name |
-- | parent_company | parent_company_name |
-- | deadline | recruitment_end_at |
-- | start_date | recruitment_start_at |
-- | requirements | qualifications |
-- | career_level | experience_type |
-- | work_location | region |
-- | duties | responsibilities |
-- | preferred | preferred_qualifications |
-- | etc_info | recruitment_notice |
-- | job_major | job_field (직군) |
-- | job_minor | job_role (직무) |
--
-- 옛 `job_role`(제목에서 옮긴 자유 글자)은 오공고에 받을 칸이 없어 `normalized_jobs` 에서 지운다.
-- 사람이 그 칸에 적은 보정·제안·규칙도 갈 곳이 없어 지운다. 분류 결과의 그 칸은 직무별로 나눈 공고의
-- 제목에 붙이는 이름으로만 남아 `position_name` 이 된다.
--
-- 값은 한 글자도 바꾸지 않는다. 키 이름만 옮기므로 `content_hash` 도 그대로다. 옛 이름의 키가 없거나
-- JSON 이 깨진 행은 건드리지 않는다.

-- migrate:up

-- 1. 정규화 결과
ALTER TABLE normalized_jobs DROP COLUMN job_role;
ALTER TABLE normalized_jobs RENAME COLUMN company TO company_name;
ALTER TABLE normalized_jobs RENAME COLUMN parent_company TO parent_company_name;
ALTER TABLE normalized_jobs RENAME COLUMN deadline TO recruitment_end_at;
ALTER TABLE normalized_jobs RENAME COLUMN start_date TO recruitment_start_at;
ALTER TABLE normalized_jobs RENAME COLUMN requirements TO qualifications;
ALTER TABLE normalized_jobs RENAME COLUMN career_level TO experience_type;
ALTER TABLE normalized_jobs RENAME COLUMN work_location TO region;
ALTER TABLE normalized_jobs RENAME COLUMN duties TO responsibilities;
ALTER TABLE normalized_jobs RENAME COLUMN preferred TO preferred_qualifications;
ALTER TABLE normalized_jobs RENAME COLUMN etc_info TO recruitment_notice;
ALTER TABLE normalized_jobs RENAME COLUMN job_major TO job_field;
ALTER TABLE normalized_jobs RENAME COLUMN job_minor TO job_role;

-- 2. 분류 결과. 판정 근거 JSON 의 키와 버린 칸 목록도 같은 이름을 쓴다
ALTER TABLE job_classifications RENAME COLUMN job_role TO position_name;
ALTER TABLE job_classifications RENAME COLUMN career_level TO experience_type;
ALTER TABLE job_classifications RENAME COLUMN work_location TO region;
ALTER TABLE job_classifications RENAME COLUMN duties TO responsibilities;
ALTER TABLE job_classifications RENAME COLUMN preferred TO preferred_qualifications;
ALTER TABLE job_classifications RENAME COLUMN requirements TO qualifications;
ALTER TABLE job_classifications RENAME COLUMN etc_info TO recruitment_notice;
ALTER TABLE job_classifications RENAME COLUMN job_major TO job_field;
ALTER TABLE job_classifications RENAME COLUMN job_minor TO job_role;
UPDATE job_classifications
   SET evidence_json = json_remove(json_set(evidence_json, '$.position_name', json(evidence_json -> '$.job_role')), '$.job_role')
 WHERE CASE WHEN json_valid(evidence_json) THEN json_type(evidence_json, '$.job_role') IS NOT NULL ELSE 0 END;
UPDATE job_classifications
   SET evidence_json = json_remove(json_set(evidence_json, '$.experience_type', json(evidence_json -> '$.career_level')), '$.career_level')
 WHERE CASE WHEN json_valid(evidence_json) THEN json_type(evidence_json, '$.career_level') IS NOT NULL ELSE 0 END;
UPDATE job_classifications
   SET evidence_json = json_remove(json_set(evidence_json, '$.experience_type_evidence', json(evidence_json -> '$.career_level_evidence')), '$.career_level_evidence')
 WHERE CASE WHEN json_valid(evidence_json) THEN json_type(evidence_json, '$.career_level_evidence') IS NOT NULL ELSE 0 END;
UPDATE job_classifications
   SET evidence_json = json_remove(json_set(evidence_json, '$.job_field', json(evidence_json -> '$.job_major')), '$.job_major')
 WHERE CASE WHEN json_valid(evidence_json) THEN json_type(evidence_json, '$.job_major') IS NOT NULL ELSE 0 END;
UPDATE job_classifications
   SET evidence_json = json_remove(json_set(evidence_json, '$.job_field_evidence', json(evidence_json -> '$.job_major_evidence')), '$.job_major_evidence')
 WHERE CASE WHEN json_valid(evidence_json) THEN json_type(evidence_json, '$.job_major_evidence') IS NOT NULL ELSE 0 END;
UPDATE job_classifications
   SET evidence_json = json_remove(json_set(evidence_json, '$.job_role', json(evidence_json -> '$.job_minor')), '$.job_minor')
 WHERE CASE WHEN json_valid(evidence_json) THEN json_type(evidence_json, '$.job_minor') IS NOT NULL ELSE 0 END;
UPDATE job_classifications
   SET evidence_json = json_remove(json_set(evidence_json, '$.job_role_evidence', json(evidence_json -> '$.job_minor_evidence')), '$.job_minor_evidence')
 WHERE CASE WHEN json_valid(evidence_json) THEN json_type(evidence_json, '$.job_minor_evidence') IS NOT NULL ELSE 0 END;
UPDATE job_classifications
   SET dropped_fields = trim(replace(replace(replace(replace(replace(replace(replace(replace(replace((', ' || dropped_fields || ', '), ', job_role, ', ', position_name, '), ', career_level, ', ', experience_type, '), ', work_location, ', ', region, '), ', duties, ', ', responsibilities, '), ', preferred, ', ', preferred_qualifications, '), ', requirements, ', ', qualifications, '), ', etc_info, ', ', recruitment_notice, '), ', job_major, ', ', job_field, '), ', job_minor, ', ', job_role, '), ', ')
 WHERE dropped_fields <> '';

-- 3. 사람 보정. 허용 이름이 CHECK 에 박혀 있어 표를 다시 만든다
DELETE FROM job_field_overrides WHERE field_name = 'job_role';
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
                       'compensation', 'benefits', 'education_level', 'recruitment_headcount'
                   )
               ),
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (raw_job_id, part, field_name)
);

INSERT INTO job_field_overrides_next (id, raw_job_id, part, field_name, value, created_at, updated_at)
SELECT id, raw_job_id, part,
       CASE field_name
                WHEN 'company' THEN 'company_name'
                WHEN 'deadline' THEN 'recruitment_end_at'
                WHEN 'start_date' THEN 'recruitment_start_at'
                WHEN 'requirements' THEN 'qualifications'
                WHEN 'career_level' THEN 'experience_type'
                WHEN 'work_location' THEN 'region'
                WHEN 'duties' THEN 'responsibilities'
                WHEN 'preferred' THEN 'preferred_qualifications'
                WHEN 'etc_info' THEN 'recruitment_notice'
                WHEN 'job_major' THEN 'job_field'
                WHEN 'job_minor' THEN 'job_role'
                ELSE field_name END,
       value, created_at, updated_at
  FROM job_field_overrides;

DROP TABLE job_field_overrides;
ALTER TABLE job_field_overrides_next RENAME TO job_field_overrides;

-- 4. 제안. 사람 보정과 같은 이유로 다시 만든다
DELETE FROM job_field_suggestions WHERE field_name = 'job_role';
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
                       'education_level', 'recruitment_headcount'
                   )
               ),
    value      TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (raw_job_id, part, field_name)
);

INSERT INTO job_field_suggestions_next (id, raw_job_id, part, field_name, value, reason, created_at)
SELECT id, raw_job_id, part,
       CASE field_name
                WHEN 'company' THEN 'company_name'
                WHEN 'deadline' THEN 'recruitment_end_at'
                WHEN 'start_date' THEN 'recruitment_start_at'
                WHEN 'requirements' THEN 'qualifications'
                WHEN 'career_level' THEN 'experience_type'
                WHEN 'work_location' THEN 'region'
                WHEN 'duties' THEN 'responsibilities'
                WHEN 'preferred' THEN 'preferred_qualifications'
                WHEN 'etc_info' THEN 'recruitment_notice'
                WHEN 'job_major' THEN 'job_field'
                WHEN 'job_minor' THEN 'job_role'
                ELSE field_name END,
       value, reason, created_at
  FROM job_field_suggestions;

DROP TABLE job_field_suggestions;
ALTER TABLE job_field_suggestions_next RENAME TO job_field_suggestions;

-- 5. 정규화 규칙의 대상 칸
DELETE FROM normalization_rules WHERE field_name = 'job_role';
UPDATE normalization_rules
   SET field_name = CASE field_name
                WHEN 'company' THEN 'company_name'
                WHEN 'deadline' THEN 'recruitment_end_at'
                WHEN 'start_date' THEN 'recruitment_start_at'
                WHEN 'requirements' THEN 'qualifications'
                WHEN 'career_level' THEN 'experience_type'
                WHEN 'work_location' THEN 'region'
                WHEN 'duties' THEN 'responsibilities'
                WHEN 'preferred' THEN 'preferred_qualifications'
                WHEN 'etc_info' THEN 'recruitment_notice'
                WHEN 'job_major' THEN 'job_field'
                WHEN 'job_minor' THEN 'job_role'
                ELSE field_name END;

-- 6. 사이트별 셀렉터와 API 설정의 칸 이름
UPDATE crawlers
   SET selectors_json = json_remove(json_set(selectors_json, '$.list.company_name', json(selectors_json -> '$.list.company')), '$.list.company')
 WHERE CASE WHEN json_valid(selectors_json) THEN json_type(selectors_json, '$.list.company') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.list.fields.company_name', json(api_config_json -> '$.list.fields.company')), '$.list.fields.company')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.list.fields.company') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET selectors_json = json_remove(json_set(selectors_json, '$.detail.company_name', json(selectors_json -> '$.detail.company')), '$.detail.company')
 WHERE CASE WHEN json_valid(selectors_json) THEN json_type(selectors_json, '$.detail.company') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.detail.fields.company_name', json(api_config_json -> '$.detail.fields.company')), '$.detail.fields.company')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.detail.fields.company') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET selectors_json = json_remove(json_set(selectors_json, '$.detail.recruitment_end_at', json(selectors_json -> '$.detail.deadline')), '$.detail.deadline')
 WHERE CASE WHEN json_valid(selectors_json) THEN json_type(selectors_json, '$.detail.deadline') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.list.fields.recruitment_end_at', json(api_config_json -> '$.list.fields.deadline')), '$.list.fields.deadline')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.list.fields.deadline') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.detail.fields.recruitment_end_at', json(api_config_json -> '$.detail.fields.deadline')), '$.detail.fields.deadline')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.detail.fields.deadline') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET selectors_json = json_remove(json_set(selectors_json, '$.detail.recruitment_start_at', json(selectors_json -> '$.detail.start_date')), '$.detail.start_date')
 WHERE CASE WHEN json_valid(selectors_json) THEN json_type(selectors_json, '$.detail.start_date') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.list.fields.recruitment_start_at', json(api_config_json -> '$.list.fields.start_date')), '$.list.fields.start_date')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.list.fields.start_date') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.detail.fields.recruitment_start_at', json(api_config_json -> '$.detail.fields.start_date')), '$.detail.fields.start_date')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.detail.fields.start_date') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET selectors_json = json_remove(json_set(selectors_json, '$.detail.qualifications', json(selectors_json -> '$.detail.requirements')), '$.detail.requirements')
 WHERE CASE WHEN json_valid(selectors_json) THEN json_type(selectors_json, '$.detail.requirements') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.list.fields.qualifications', json(api_config_json -> '$.list.fields.requirements')), '$.list.fields.requirements')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.list.fields.requirements') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.detail.fields.qualifications', json(api_config_json -> '$.detail.fields.requirements')), '$.detail.fields.requirements')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.detail.fields.requirements') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET selectors_json = json_remove(json_set(selectors_json, '$.detail.experience_type', json(selectors_json -> '$.detail.career_level')), '$.detail.career_level')
 WHERE CASE WHEN json_valid(selectors_json) THEN json_type(selectors_json, '$.detail.career_level') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.list.fields.experience_type', json(api_config_json -> '$.list.fields.career_level')), '$.list.fields.career_level')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.list.fields.career_level') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.detail.fields.experience_type', json(api_config_json -> '$.detail.fields.career_level')), '$.detail.fields.career_level')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.detail.fields.career_level') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET selectors_json = json_remove(json_set(selectors_json, '$.detail.region', json(selectors_json -> '$.detail.work_location')), '$.detail.work_location')
 WHERE CASE WHEN json_valid(selectors_json) THEN json_type(selectors_json, '$.detail.work_location') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.list.fields.region', json(api_config_json -> '$.list.fields.work_location')), '$.list.fields.work_location')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.list.fields.work_location') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.detail.fields.region', json(api_config_json -> '$.detail.fields.work_location')), '$.detail.fields.work_location')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.detail.fields.work_location') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET selectors_json = json_remove(json_set(selectors_json, '$.detail.responsibilities', json(selectors_json -> '$.detail.duties')), '$.detail.duties')
 WHERE CASE WHEN json_valid(selectors_json) THEN json_type(selectors_json, '$.detail.duties') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.list.fields.responsibilities', json(api_config_json -> '$.list.fields.duties')), '$.list.fields.duties')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.list.fields.duties') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.detail.fields.responsibilities', json(api_config_json -> '$.detail.fields.duties')), '$.detail.fields.duties')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.detail.fields.duties') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET selectors_json = json_remove(json_set(selectors_json, '$.detail.preferred_qualifications', json(selectors_json -> '$.detail.preferred')), '$.detail.preferred')
 WHERE CASE WHEN json_valid(selectors_json) THEN json_type(selectors_json, '$.detail.preferred') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.list.fields.preferred_qualifications', json(api_config_json -> '$.list.fields.preferred')), '$.list.fields.preferred')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.list.fields.preferred') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.detail.fields.preferred_qualifications', json(api_config_json -> '$.detail.fields.preferred')), '$.detail.fields.preferred')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.detail.fields.preferred') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET selectors_json = json_remove(json_set(selectors_json, '$.detail.recruitment_notice', json(selectors_json -> '$.detail.etc_info')), '$.detail.etc_info')
 WHERE CASE WHEN json_valid(selectors_json) THEN json_type(selectors_json, '$.detail.etc_info') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.list.fields.recruitment_notice', json(api_config_json -> '$.list.fields.etc_info')), '$.list.fields.etc_info')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.list.fields.etc_info') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.detail.fields.recruitment_notice', json(api_config_json -> '$.detail.fields.etc_info')), '$.detail.fields.etc_info')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.detail.fields.etc_info') IS NOT NULL ELSE 0 END;

-- 7. 수집 원본과 그 이력의 키
UPDATE raw_jobs
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.company_name', json(raw_data_json -> '$.company')), '$.company')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.company') IS NOT NULL ELSE 0 END;
UPDATE raw_job_history
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.company_name', json(raw_data_json -> '$.company')), '$.company')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.company') IS NOT NULL ELSE 0 END;
UPDATE raw_jobs
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.recruitment_end_at', json(raw_data_json -> '$.deadline')), '$.deadline')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.deadline') IS NOT NULL ELSE 0 END;
UPDATE raw_job_history
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.recruitment_end_at', json(raw_data_json -> '$.deadline')), '$.deadline')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.deadline') IS NOT NULL ELSE 0 END;
UPDATE raw_jobs
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.recruitment_start_at', json(raw_data_json -> '$.start_date')), '$.start_date')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.start_date') IS NOT NULL ELSE 0 END;
UPDATE raw_job_history
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.recruitment_start_at', json(raw_data_json -> '$.start_date')), '$.start_date')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.start_date') IS NOT NULL ELSE 0 END;
UPDATE raw_jobs
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.qualifications', json(raw_data_json -> '$.requirements')), '$.requirements')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.requirements') IS NOT NULL ELSE 0 END;
UPDATE raw_job_history
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.qualifications', json(raw_data_json -> '$.requirements')), '$.requirements')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.requirements') IS NOT NULL ELSE 0 END;
UPDATE raw_jobs
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.experience_type', json(raw_data_json -> '$.career_level')), '$.career_level')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.career_level') IS NOT NULL ELSE 0 END;
UPDATE raw_job_history
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.experience_type', json(raw_data_json -> '$.career_level')), '$.career_level')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.career_level') IS NOT NULL ELSE 0 END;
UPDATE raw_jobs
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.region', json(raw_data_json -> '$.work_location')), '$.work_location')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.work_location') IS NOT NULL ELSE 0 END;
UPDATE raw_job_history
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.region', json(raw_data_json -> '$.work_location')), '$.work_location')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.work_location') IS NOT NULL ELSE 0 END;
UPDATE raw_jobs
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.responsibilities', json(raw_data_json -> '$.duties')), '$.duties')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.duties') IS NOT NULL ELSE 0 END;
UPDATE raw_job_history
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.responsibilities', json(raw_data_json -> '$.duties')), '$.duties')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.duties') IS NOT NULL ELSE 0 END;
UPDATE raw_jobs
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.preferred_qualifications', json(raw_data_json -> '$.preferred')), '$.preferred')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.preferred') IS NOT NULL ELSE 0 END;
UPDATE raw_job_history
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.preferred_qualifications', json(raw_data_json -> '$.preferred')), '$.preferred')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.preferred') IS NOT NULL ELSE 0 END;
UPDATE raw_jobs
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.recruitment_notice', json(raw_data_json -> '$.etc_info')), '$.etc_info')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.etc_info') IS NOT NULL ELSE 0 END;
UPDATE raw_job_history
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.recruitment_notice', json(raw_data_json -> '$.etc_info')), '$.etc_info')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.etc_info') IS NOT NULL ELSE 0 END;

-- migrate:down
-- 지운 job_role 과 그 칸의 보정·제안·규칙은 되살리지 못한다. 칼럼만 빈 채로 되돌린다
UPDATE raw_jobs
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.etc_info', json(raw_data_json -> '$.recruitment_notice')), '$.recruitment_notice')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.recruitment_notice') IS NOT NULL ELSE 0 END;
UPDATE raw_job_history
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.etc_info', json(raw_data_json -> '$.recruitment_notice')), '$.recruitment_notice')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.recruitment_notice') IS NOT NULL ELSE 0 END;
UPDATE raw_jobs
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.preferred', json(raw_data_json -> '$.preferred_qualifications')), '$.preferred_qualifications')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.preferred_qualifications') IS NOT NULL ELSE 0 END;
UPDATE raw_job_history
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.preferred', json(raw_data_json -> '$.preferred_qualifications')), '$.preferred_qualifications')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.preferred_qualifications') IS NOT NULL ELSE 0 END;
UPDATE raw_jobs
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.duties', json(raw_data_json -> '$.responsibilities')), '$.responsibilities')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.responsibilities') IS NOT NULL ELSE 0 END;
UPDATE raw_job_history
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.duties', json(raw_data_json -> '$.responsibilities')), '$.responsibilities')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.responsibilities') IS NOT NULL ELSE 0 END;
UPDATE raw_jobs
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.work_location', json(raw_data_json -> '$.region')), '$.region')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.region') IS NOT NULL ELSE 0 END;
UPDATE raw_job_history
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.work_location', json(raw_data_json -> '$.region')), '$.region')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.region') IS NOT NULL ELSE 0 END;
UPDATE raw_jobs
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.career_level', json(raw_data_json -> '$.experience_type')), '$.experience_type')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.experience_type') IS NOT NULL ELSE 0 END;
UPDATE raw_job_history
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.career_level', json(raw_data_json -> '$.experience_type')), '$.experience_type')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.experience_type') IS NOT NULL ELSE 0 END;
UPDATE raw_jobs
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.requirements', json(raw_data_json -> '$.qualifications')), '$.qualifications')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.qualifications') IS NOT NULL ELSE 0 END;
UPDATE raw_job_history
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.requirements', json(raw_data_json -> '$.qualifications')), '$.qualifications')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.qualifications') IS NOT NULL ELSE 0 END;
UPDATE raw_jobs
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.start_date', json(raw_data_json -> '$.recruitment_start_at')), '$.recruitment_start_at')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.recruitment_start_at') IS NOT NULL ELSE 0 END;
UPDATE raw_job_history
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.start_date', json(raw_data_json -> '$.recruitment_start_at')), '$.recruitment_start_at')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.recruitment_start_at') IS NOT NULL ELSE 0 END;
UPDATE raw_jobs
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.deadline', json(raw_data_json -> '$.recruitment_end_at')), '$.recruitment_end_at')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.recruitment_end_at') IS NOT NULL ELSE 0 END;
UPDATE raw_job_history
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.deadline', json(raw_data_json -> '$.recruitment_end_at')), '$.recruitment_end_at')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.recruitment_end_at') IS NOT NULL ELSE 0 END;
UPDATE raw_jobs
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.company', json(raw_data_json -> '$.company_name')), '$.company_name')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.company_name') IS NOT NULL ELSE 0 END;
UPDATE raw_job_history
   SET raw_data_json = json_remove(json_set(raw_data_json, '$.company', json(raw_data_json -> '$.company_name')), '$.company_name')
 WHERE CASE WHEN json_valid(raw_data_json) THEN json_type(raw_data_json, '$.company_name') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET selectors_json = json_remove(json_set(selectors_json, '$.detail.etc_info', json(selectors_json -> '$.detail.recruitment_notice')), '$.detail.recruitment_notice')
 WHERE CASE WHEN json_valid(selectors_json) THEN json_type(selectors_json, '$.detail.recruitment_notice') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.list.fields.etc_info', json(api_config_json -> '$.list.fields.recruitment_notice')), '$.list.fields.recruitment_notice')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.list.fields.recruitment_notice') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.detail.fields.etc_info', json(api_config_json -> '$.detail.fields.recruitment_notice')), '$.detail.fields.recruitment_notice')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.detail.fields.recruitment_notice') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET selectors_json = json_remove(json_set(selectors_json, '$.detail.preferred', json(selectors_json -> '$.detail.preferred_qualifications')), '$.detail.preferred_qualifications')
 WHERE CASE WHEN json_valid(selectors_json) THEN json_type(selectors_json, '$.detail.preferred_qualifications') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.list.fields.preferred', json(api_config_json -> '$.list.fields.preferred_qualifications')), '$.list.fields.preferred_qualifications')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.list.fields.preferred_qualifications') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.detail.fields.preferred', json(api_config_json -> '$.detail.fields.preferred_qualifications')), '$.detail.fields.preferred_qualifications')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.detail.fields.preferred_qualifications') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET selectors_json = json_remove(json_set(selectors_json, '$.detail.duties', json(selectors_json -> '$.detail.responsibilities')), '$.detail.responsibilities')
 WHERE CASE WHEN json_valid(selectors_json) THEN json_type(selectors_json, '$.detail.responsibilities') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.list.fields.duties', json(api_config_json -> '$.list.fields.responsibilities')), '$.list.fields.responsibilities')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.list.fields.responsibilities') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.detail.fields.duties', json(api_config_json -> '$.detail.fields.responsibilities')), '$.detail.fields.responsibilities')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.detail.fields.responsibilities') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET selectors_json = json_remove(json_set(selectors_json, '$.detail.work_location', json(selectors_json -> '$.detail.region')), '$.detail.region')
 WHERE CASE WHEN json_valid(selectors_json) THEN json_type(selectors_json, '$.detail.region') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.list.fields.work_location', json(api_config_json -> '$.list.fields.region')), '$.list.fields.region')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.list.fields.region') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.detail.fields.work_location', json(api_config_json -> '$.detail.fields.region')), '$.detail.fields.region')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.detail.fields.region') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET selectors_json = json_remove(json_set(selectors_json, '$.detail.career_level', json(selectors_json -> '$.detail.experience_type')), '$.detail.experience_type')
 WHERE CASE WHEN json_valid(selectors_json) THEN json_type(selectors_json, '$.detail.experience_type') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.list.fields.career_level', json(api_config_json -> '$.list.fields.experience_type')), '$.list.fields.experience_type')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.list.fields.experience_type') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.detail.fields.career_level', json(api_config_json -> '$.detail.fields.experience_type')), '$.detail.fields.experience_type')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.detail.fields.experience_type') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET selectors_json = json_remove(json_set(selectors_json, '$.detail.requirements', json(selectors_json -> '$.detail.qualifications')), '$.detail.qualifications')
 WHERE CASE WHEN json_valid(selectors_json) THEN json_type(selectors_json, '$.detail.qualifications') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.list.fields.requirements', json(api_config_json -> '$.list.fields.qualifications')), '$.list.fields.qualifications')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.list.fields.qualifications') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.detail.fields.requirements', json(api_config_json -> '$.detail.fields.qualifications')), '$.detail.fields.qualifications')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.detail.fields.qualifications') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET selectors_json = json_remove(json_set(selectors_json, '$.detail.start_date', json(selectors_json -> '$.detail.recruitment_start_at')), '$.detail.recruitment_start_at')
 WHERE CASE WHEN json_valid(selectors_json) THEN json_type(selectors_json, '$.detail.recruitment_start_at') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.list.fields.start_date', json(api_config_json -> '$.list.fields.recruitment_start_at')), '$.list.fields.recruitment_start_at')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.list.fields.recruitment_start_at') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.detail.fields.start_date', json(api_config_json -> '$.detail.fields.recruitment_start_at')), '$.detail.fields.recruitment_start_at')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.detail.fields.recruitment_start_at') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET selectors_json = json_remove(json_set(selectors_json, '$.detail.deadline', json(selectors_json -> '$.detail.recruitment_end_at')), '$.detail.recruitment_end_at')
 WHERE CASE WHEN json_valid(selectors_json) THEN json_type(selectors_json, '$.detail.recruitment_end_at') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.list.fields.deadline', json(api_config_json -> '$.list.fields.recruitment_end_at')), '$.list.fields.recruitment_end_at')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.list.fields.recruitment_end_at') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.detail.fields.deadline', json(api_config_json -> '$.detail.fields.recruitment_end_at')), '$.detail.fields.recruitment_end_at')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.detail.fields.recruitment_end_at') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET selectors_json = json_remove(json_set(selectors_json, '$.list.company', json(selectors_json -> '$.list.company_name')), '$.list.company_name')
 WHERE CASE WHEN json_valid(selectors_json) THEN json_type(selectors_json, '$.list.company_name') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.list.fields.company', json(api_config_json -> '$.list.fields.company_name')), '$.list.fields.company_name')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.list.fields.company_name') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET selectors_json = json_remove(json_set(selectors_json, '$.detail.company', json(selectors_json -> '$.detail.company_name')), '$.detail.company_name')
 WHERE CASE WHEN json_valid(selectors_json) THEN json_type(selectors_json, '$.detail.company_name') IS NOT NULL ELSE 0 END;
UPDATE crawlers
   SET api_config_json = json_remove(json_set(api_config_json, '$.detail.fields.company', json(api_config_json -> '$.detail.fields.company_name')), '$.detail.fields.company_name')
 WHERE CASE WHEN json_valid(api_config_json) THEN json_type(api_config_json, '$.detail.fields.company_name') IS NOT NULL ELSE 0 END;
UPDATE normalization_rules
   SET field_name = CASE field_name
                WHEN 'job_role' THEN 'job_minor'
                WHEN 'job_field' THEN 'job_major'
                WHEN 'recruitment_notice' THEN 'etc_info'
                WHEN 'preferred_qualifications' THEN 'preferred'
                WHEN 'responsibilities' THEN 'duties'
                WHEN 'region' THEN 'work_location'
                WHEN 'experience_type' THEN 'career_level'
                WHEN 'qualifications' THEN 'requirements'
                WHEN 'recruitment_start_at' THEN 'start_date'
                WHEN 'recruitment_end_at' THEN 'deadline'
                WHEN 'company_name' THEN 'company'
                ELSE field_name END;
CREATE TABLE job_field_suggestions_next (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_job_id INTEGER NOT NULL REFERENCES raw_jobs(id),
    part       INTEGER NOT NULL DEFAULT 1 CHECK (part >= 1),
    field_name TEXT NOT NULL CHECK (
                   field_name IN (
                       'company', 'title', 'job_role', 'deadline', 'body', 'requirements', 'start_date',
                       'employment_type', 'career_level', 'work_location', 'duties', 'preferred',
                       'hiring_process', 'etc_info', 'job_major', 'job_minor', 'company_and_team_introduction',
                       'compensation', 'benefits', 'education_level', 'recruitment_headcount'
                   )
               ),
    value      TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (raw_job_id, part, field_name)
);

INSERT INTO job_field_suggestions_next (id, raw_job_id, part, field_name, value, reason, created_at)
SELECT id, raw_job_id, part,
       CASE field_name
                WHEN 'job_role' THEN 'job_minor'
                WHEN 'job_field' THEN 'job_major'
                WHEN 'recruitment_notice' THEN 'etc_info'
                WHEN 'preferred_qualifications' THEN 'preferred'
                WHEN 'responsibilities' THEN 'duties'
                WHEN 'region' THEN 'work_location'
                WHEN 'experience_type' THEN 'career_level'
                WHEN 'qualifications' THEN 'requirements'
                WHEN 'recruitment_start_at' THEN 'start_date'
                WHEN 'recruitment_end_at' THEN 'deadline'
                WHEN 'company_name' THEN 'company'
                ELSE field_name END,
       value, reason, created_at
  FROM job_field_suggestions;

DROP TABLE job_field_suggestions;
ALTER TABLE job_field_suggestions_next RENAME TO job_field_suggestions;
CREATE TABLE job_field_overrides_next (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_job_id INTEGER NOT NULL REFERENCES raw_jobs(id),
    part       INTEGER NOT NULL DEFAULT 1 CHECK (part >= 1),
    field_name TEXT NOT NULL CHECK (
                   field_name IN (
                       'company', 'title', 'department', 'deadline', 'body', 'requirements', 'start_date',
                       'job_category', 'employment_type', 'career_level', 'work_location', 'headcount',
                       'duties', 'preferred', 'hiring_process', 'etc_info', 'job_role', 'job_major',
                       'job_minor', 'company_and_team_introduction', 'compensation', 'benefits',
                       'education_level', 'recruitment_headcount'
                   )
               ),
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (raw_job_id, part, field_name)
);

INSERT INTO job_field_overrides_next (id, raw_job_id, part, field_name, value, created_at, updated_at)
SELECT id, raw_job_id, part,
       CASE field_name
                WHEN 'job_role' THEN 'job_minor'
                WHEN 'job_field' THEN 'job_major'
                WHEN 'recruitment_notice' THEN 'etc_info'
                WHEN 'preferred_qualifications' THEN 'preferred'
                WHEN 'responsibilities' THEN 'duties'
                WHEN 'region' THEN 'work_location'
                WHEN 'experience_type' THEN 'career_level'
                WHEN 'qualifications' THEN 'requirements'
                WHEN 'recruitment_start_at' THEN 'start_date'
                WHEN 'recruitment_end_at' THEN 'deadline'
                WHEN 'company_name' THEN 'company'
                ELSE field_name END,
       value, created_at, updated_at
  FROM job_field_overrides;

DROP TABLE job_field_overrides;
ALTER TABLE job_field_overrides_next RENAME TO job_field_overrides;
UPDATE job_classifications
   SET dropped_fields = trim(replace(replace(replace(replace(replace(replace(replace(replace(replace((', ' || dropped_fields || ', '), ', job_role, ', ', job_minor, '), ', job_field, ', ', job_major, '), ', recruitment_notice, ', ', etc_info, '), ', qualifications, ', ', requirements, '), ', preferred_qualifications, ', ', preferred, '), ', responsibilities, ', ', duties, '), ', region, ', ', work_location, '), ', experience_type, ', ', career_level, '), ', position_name, ', ', job_role, '), ', ')
 WHERE dropped_fields <> '';
UPDATE job_classifications
   SET evidence_json = json_remove(json_set(evidence_json, '$.job_minor_evidence', json(evidence_json -> '$.job_role_evidence')), '$.job_role_evidence')
 WHERE CASE WHEN json_valid(evidence_json) THEN json_type(evidence_json, '$.job_role_evidence') IS NOT NULL ELSE 0 END;
UPDATE job_classifications
   SET evidence_json = json_remove(json_set(evidence_json, '$.job_minor', json(evidence_json -> '$.job_role')), '$.job_role')
 WHERE CASE WHEN json_valid(evidence_json) THEN json_type(evidence_json, '$.job_role') IS NOT NULL ELSE 0 END;
UPDATE job_classifications
   SET evidence_json = json_remove(json_set(evidence_json, '$.job_major_evidence', json(evidence_json -> '$.job_field_evidence')), '$.job_field_evidence')
 WHERE CASE WHEN json_valid(evidence_json) THEN json_type(evidence_json, '$.job_field_evidence') IS NOT NULL ELSE 0 END;
UPDATE job_classifications
   SET evidence_json = json_remove(json_set(evidence_json, '$.job_major', json(evidence_json -> '$.job_field')), '$.job_field')
 WHERE CASE WHEN json_valid(evidence_json) THEN json_type(evidence_json, '$.job_field') IS NOT NULL ELSE 0 END;
UPDATE job_classifications
   SET evidence_json = json_remove(json_set(evidence_json, '$.career_level_evidence', json(evidence_json -> '$.experience_type_evidence')), '$.experience_type_evidence')
 WHERE CASE WHEN json_valid(evidence_json) THEN json_type(evidence_json, '$.experience_type_evidence') IS NOT NULL ELSE 0 END;
UPDATE job_classifications
   SET evidence_json = json_remove(json_set(evidence_json, '$.career_level', json(evidence_json -> '$.experience_type')), '$.experience_type')
 WHERE CASE WHEN json_valid(evidence_json) THEN json_type(evidence_json, '$.experience_type') IS NOT NULL ELSE 0 END;
UPDATE job_classifications
   SET evidence_json = json_remove(json_set(evidence_json, '$.job_role', json(evidence_json -> '$.position_name')), '$.position_name')
 WHERE CASE WHEN json_valid(evidence_json) THEN json_type(evidence_json, '$.position_name') IS NOT NULL ELSE 0 END;
ALTER TABLE job_classifications RENAME COLUMN job_role TO job_minor;
ALTER TABLE job_classifications RENAME COLUMN job_field TO job_major;
ALTER TABLE job_classifications RENAME COLUMN recruitment_notice TO etc_info;
ALTER TABLE job_classifications RENAME COLUMN qualifications TO requirements;
ALTER TABLE job_classifications RENAME COLUMN preferred_qualifications TO preferred;
ALTER TABLE job_classifications RENAME COLUMN responsibilities TO duties;
ALTER TABLE job_classifications RENAME COLUMN region TO work_location;
ALTER TABLE job_classifications RENAME COLUMN experience_type TO career_level;
ALTER TABLE job_classifications RENAME COLUMN position_name TO job_role;
ALTER TABLE normalized_jobs RENAME COLUMN job_role TO job_minor;
ALTER TABLE normalized_jobs RENAME COLUMN job_field TO job_major;
ALTER TABLE normalized_jobs RENAME COLUMN recruitment_notice TO etc_info;
ALTER TABLE normalized_jobs RENAME COLUMN preferred_qualifications TO preferred;
ALTER TABLE normalized_jobs RENAME COLUMN responsibilities TO duties;
ALTER TABLE normalized_jobs RENAME COLUMN region TO work_location;
ALTER TABLE normalized_jobs RENAME COLUMN experience_type TO career_level;
ALTER TABLE normalized_jobs RENAME COLUMN qualifications TO requirements;
ALTER TABLE normalized_jobs RENAME COLUMN recruitment_start_at TO start_date;
ALTER TABLE normalized_jobs RENAME COLUMN recruitment_end_at TO deadline;
ALTER TABLE normalized_jobs RENAME COLUMN parent_company_name TO parent_company;
ALTER TABLE normalized_jobs RENAME COLUMN company_name TO company;
ALTER TABLE normalized_jobs ADD COLUMN job_role TEXT;
