-- 0046 근무 지역을 오공고 enum 시·도·시·군·구 두 칸으로 (LC-3385, 2026-09-28 결정)
--
-- 오공고가 근무 지역을 글자 한 칸(`region`)에서 enum 두 칸(`region`, `subRegion`)으로 바꿨다
-- (ogonggo-server LC-3385). 크롤러도 enum 이름(`SEOUL`, `SEOUL_GANGNAM_GU`)을 저장하고 보낸다
-- (`app/regions.py`). 시·군·구 칸을 더하고, 사람 보정·제안 표가 그 칸을 받게 CHECK 를 넓힌다 —
-- 0043 과 같은 방법이다.
--
-- 쌓인 근무 지역은 enum 이름으로 옮긴다. 2026-09-21 부터 큰 지역 목록에서 쉼표로 여러 개를 골랐다
-- (`경기, 울산`). 오공고 칸은 하나라 첫 번째 것만 옮기고, 광주·전남은 `JEONNAM_GWANGJU` 하나가
-- 된다. 그 전의 원문 글자(`울산광역시 동구`)는 옮길 곳이 없어 비우고, 그런 보정·제안 행은 지운다.
-- 다시 분류하면 채워진다. 시·군·구는 쌓인 값에 없어 다시 분류해야 채워진다.
--
-- AI 규칙을 저장한 판이 있으면 근무지 두 칸의 규칙을 새 기본 규칙으로 바꾼 판을 하나 더 쌓는다.
-- 옛 판은 `여러 곳이면 모두 고른다`·`울산, 경기` 라서 enum 목록과 어긋난다. 판이 없으면 이미 기본
-- 규칙이다 (`app/classify/prompt_rules.py`).
--
-- 되돌리기: 시·도 enum 이름을 한글 이름으로 되돌리고(`JEONNAM_GWANGJU` 는 `광주`), 시·군·구의
-- 보정·제안 행과 두 칸을 지운다. 쌓은 규칙 판은 이력이라 지우지 않는다 — 화면에서 옛 판을 되돌린다.

-- migrate:up

ALTER TABLE job_classifications ADD COLUMN sub_region TEXT;
ALTER TABLE normalized_jobs ADD COLUMN sub_region TEXT;

UPDATE job_classifications
   SET region = CASE trim(substr(region, 1, instr(region || ',', ',') - 1))
                  WHEN '전국' THEN 'NATIONWIDE'
                  WHEN '서울' THEN 'SEOUL'
                  WHEN '경기' THEN 'GYEONGGI'
                  WHEN '인천' THEN 'INCHEON'
                  WHEN '부산' THEN 'BUSAN'
                  WHEN '대구' THEN 'DAEGU'
                  WHEN '광주' THEN 'JEONNAM_GWANGJU'
                  WHEN '대전' THEN 'DAEJEON'
                  WHEN '울산' THEN 'ULSAN'
                  WHEN '세종' THEN 'SEJONG'
                  WHEN '강원' THEN 'GANGWON'
                  WHEN '경남' THEN 'GYEONGNAM'
                  WHEN '경북' THEN 'GYEONGBUK'
                  WHEN '전남' THEN 'JEONNAM_GWANGJU'
                  WHEN '전북' THEN 'JEONBUK'
                  WHEN '충남' THEN 'CHUNGNAM'
                  WHEN '충북' THEN 'CHUNGBUK'
                  WHEN '제주' THEN 'JEJU'
                  WHEN '해외' THEN 'OVERSEAS'
              END
 WHERE region IS NOT NULL;

UPDATE normalized_jobs
   SET region = CASE trim(substr(region, 1, instr(region || ',', ',') - 1))
                  WHEN '전국' THEN 'NATIONWIDE'
                  WHEN '서울' THEN 'SEOUL'
                  WHEN '경기' THEN 'GYEONGGI'
                  WHEN '인천' THEN 'INCHEON'
                  WHEN '부산' THEN 'BUSAN'
                  WHEN '대구' THEN 'DAEGU'
                  WHEN '광주' THEN 'JEONNAM_GWANGJU'
                  WHEN '대전' THEN 'DAEJEON'
                  WHEN '울산' THEN 'ULSAN'
                  WHEN '세종' THEN 'SEJONG'
                  WHEN '강원' THEN 'GANGWON'
                  WHEN '경남' THEN 'GYEONGNAM'
                  WHEN '경북' THEN 'GYEONGBUK'
                  WHEN '전남' THEN 'JEONNAM_GWANGJU'
                  WHEN '전북' THEN 'JEONBUK'
                  WHEN '충남' THEN 'CHUNGNAM'
                  WHEN '충북' THEN 'CHUNGBUK'
                  WHEN '제주' THEN 'JEJU'
                  WHEN '해외' THEN 'OVERSEAS'
              END
 WHERE region IS NOT NULL;

DELETE FROM job_field_overrides
 WHERE field_name = 'region'
   AND (CASE trim(substr(value, 1, instr(value || ',', ',') - 1))
                  WHEN '전국' THEN 'NATIONWIDE'
                  WHEN '서울' THEN 'SEOUL'
                  WHEN '경기' THEN 'GYEONGGI'
                  WHEN '인천' THEN 'INCHEON'
                  WHEN '부산' THEN 'BUSAN'
                  WHEN '대구' THEN 'DAEGU'
                  WHEN '광주' THEN 'JEONNAM_GWANGJU'
                  WHEN '대전' THEN 'DAEJEON'
                  WHEN '울산' THEN 'ULSAN'
                  WHEN '세종' THEN 'SEJONG'
                  WHEN '강원' THEN 'GANGWON'
                  WHEN '경남' THEN 'GYEONGNAM'
                  WHEN '경북' THEN 'GYEONGBUK'
                  WHEN '전남' THEN 'JEONNAM_GWANGJU'
                  WHEN '전북' THEN 'JEONBUK'
                  WHEN '충남' THEN 'CHUNGNAM'
                  WHEN '충북' THEN 'CHUNGBUK'
                  WHEN '제주' THEN 'JEJU'
                  WHEN '해외' THEN 'OVERSEAS'
              END) IS NULL;

UPDATE job_field_overrides
   SET value = CASE trim(substr(value, 1, instr(value || ',', ',') - 1))
                  WHEN '전국' THEN 'NATIONWIDE'
                  WHEN '서울' THEN 'SEOUL'
                  WHEN '경기' THEN 'GYEONGGI'
                  WHEN '인천' THEN 'INCHEON'
                  WHEN '부산' THEN 'BUSAN'
                  WHEN '대구' THEN 'DAEGU'
                  WHEN '광주' THEN 'JEONNAM_GWANGJU'
                  WHEN '대전' THEN 'DAEJEON'
                  WHEN '울산' THEN 'ULSAN'
                  WHEN '세종' THEN 'SEJONG'
                  WHEN '강원' THEN 'GANGWON'
                  WHEN '경남' THEN 'GYEONGNAM'
                  WHEN '경북' THEN 'GYEONGBUK'
                  WHEN '전남' THEN 'JEONNAM_GWANGJU'
                  WHEN '전북' THEN 'JEONBUK'
                  WHEN '충남' THEN 'CHUNGNAM'
                  WHEN '충북' THEN 'CHUNGBUK'
                  WHEN '제주' THEN 'JEJU'
                  WHEN '해외' THEN 'OVERSEAS'
              END
 WHERE field_name = 'region';

DELETE FROM job_field_suggestions
 WHERE field_name = 'region'
   AND (CASE trim(substr(value, 1, instr(value || ',', ',') - 1))
                  WHEN '전국' THEN 'NATIONWIDE'
                  WHEN '서울' THEN 'SEOUL'
                  WHEN '경기' THEN 'GYEONGGI'
                  WHEN '인천' THEN 'INCHEON'
                  WHEN '부산' THEN 'BUSAN'
                  WHEN '대구' THEN 'DAEGU'
                  WHEN '광주' THEN 'JEONNAM_GWANGJU'
                  WHEN '대전' THEN 'DAEJEON'
                  WHEN '울산' THEN 'ULSAN'
                  WHEN '세종' THEN 'SEJONG'
                  WHEN '강원' THEN 'GANGWON'
                  WHEN '경남' THEN 'GYEONGNAM'
                  WHEN '경북' THEN 'GYEONGBUK'
                  WHEN '전남' THEN 'JEONNAM_GWANGJU'
                  WHEN '전북' THEN 'JEONBUK'
                  WHEN '충남' THEN 'CHUNGNAM'
                  WHEN '충북' THEN 'CHUNGBUK'
                  WHEN '제주' THEN 'JEJU'
                  WHEN '해외' THEN 'OVERSEAS'
              END) IS NULL;

UPDATE job_field_suggestions
   SET value = CASE trim(substr(value, 1, instr(value || ',', ',') - 1))
                  WHEN '전국' THEN 'NATIONWIDE'
                  WHEN '서울' THEN 'SEOUL'
                  WHEN '경기' THEN 'GYEONGGI'
                  WHEN '인천' THEN 'INCHEON'
                  WHEN '부산' THEN 'BUSAN'
                  WHEN '대구' THEN 'DAEGU'
                  WHEN '광주' THEN 'JEONNAM_GWANGJU'
                  WHEN '대전' THEN 'DAEJEON'
                  WHEN '울산' THEN 'ULSAN'
                  WHEN '세종' THEN 'SEJONG'
                  WHEN '강원' THEN 'GANGWON'
                  WHEN '경남' THEN 'GYEONGNAM'
                  WHEN '경북' THEN 'GYEONGBUK'
                  WHEN '전남' THEN 'JEONNAM_GWANGJU'
                  WHEN '전북' THEN 'JEONBUK'
                  WHEN '충남' THEN 'CHUNGNAM'
                  WHEN '충북' THEN 'CHUNGBUK'
                  WHEN '제주' THEN 'JEJU'
                  WHEN '해외' THEN 'OVERSEAS'
              END
 WHERE field_name = 'region';

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
                       'application_email', 'inquiry_email',
                       'sub_region'
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
                       'application_email', 'inquiry_email',
                       'sub_region'
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

INSERT INTO classify_rule_versions (rules_json, note)
SELECT json_set(
           rules_json,
           '$.fields.region', json('{"rule": "근무지의 시·도. 근무지 주소나 근무 지역 문구에서 찾는다. 회사 본사 주소보다 이 직무를 실제로 일하는 곳이 먼저다. **근무지가 여러 곳이면 첫 번째 근무지 하나만 고른다.** 전체 이름과 줄임말은 같은 값이다(서울특별시·서울 → `SEOUL`, 경상남도·경남 → `GYEONGNAM`). 광주광역시·전라남도·전남광주통합특별시는 모두 `JEONNAM_GWANGJU` 다. 강원도·강원특별자치도 → `GANGWON`, 전라북도·전북특별자치도 → `JEONBUK`. 구·시·군이나 사업장 이름만 적혀 있으면 그곳이 속한 시·도를 고른다. 전국 어디서나 일하면(전국 지점 순환, 지역 무관) `NATIONWIDE`, 나라 밖이면 `OVERSEAS` 다. 근무지를 알 수 없거나 재택·원격만 적혀 있으면(`100% 원격 근무`) 빈 글자로 둔다 — 비슷한 값을 짐작해 넣지 않는다", "examples": [{"source": "서울 강남구 테헤란로 123", "value": "SEOUL"}, {"source": "근무지: 성남시 분당구(판교)", "value": "GYEONGGI"}, {"source": "울산 본사 및 분당 GRC", "value": "ULSAN"}, {"source": "광주광역시 북구", "value": "JEONNAM_GWANGJU"}, {"source": "근무지 : 본사(양재동) / 전국 현장", "value": "NATIONWIDE"}, {"source": "근무지: 미국 샌프란시스코", "value": "OVERSEAS"}]}'),
           '$.fields.sub_region', json('{"rule": "근무지의 시·군·구. **`region` 으로 고른 시·도 줄에 있는 값만 고른다** — 다른 시·도의 값이면 버려진다. 중구·동구·서구·남구·북구·강서구·고성군처럼 같은 이름이 여러 시·도에 있으니 시·도를 먼저 정한다. 수원시 장안구·성남시 분당구·전주시 완산구처럼 시 아래의 구는 그 시다(성남시 분당구 → `GYEONGGI_SEONGNAM_SI`). 경기 광주시는 `GYEONGGI_GWANGJU_SI` 이고 광주광역시와 다르다. 광주의 구와 전남의 시·군은 `JEONNAM_GWANGJU_` 로 시작하는 값이다. 인천은 2026년 개편 뒤의 구다 — 예전 중구·동구·서구로만 적혀 있어 제물포구·영종구·서해구·검단구 중 어디인지 정할 수 없으면 빈 글자로 둔다. 시·도만 알 수 있거나(`부산 (구 미정)`), 시·군·구가 없는 시·도(`SEJONG`)이거나, `region` 이 비었거나 `NATIONWIDE`·`OVERSEAS` 이면 빈 글자로 둔다", "examples": [{"source": "서울 강남구 테헤란로 123", "value": "SEOUL_GANGNAM_GU"}, {"source": "경기도 성남시 분당구 판교역로", "value": "GYEONGGI_SEONGNAM_SI"}, {"source": "광주광역시 북구", "value": "JEONNAM_GWANGJU_BUK_GU"}]}')
       ),
       '근무 지역을 오공고 enum 으로 (0046)'
  FROM classify_rule_versions
 ORDER BY id DESC
 LIMIT 1;

-- migrate:down

DELETE FROM job_field_suggestions WHERE field_name = 'sub_region';
DELETE FROM job_field_overrides WHERE field_name = 'sub_region';

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
                       'cover_image_url',
                       'application_email', 'inquiry_email'
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
                       'cover_image_url',
                       'application_email', 'inquiry_email'
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

UPDATE job_field_suggestions
   SET value = CASE value
                  WHEN 'NATIONWIDE' THEN '전국'
                  WHEN 'SEOUL' THEN '서울'
                  WHEN 'GYEONGGI' THEN '경기'
                  WHEN 'INCHEON' THEN '인천'
                  WHEN 'BUSAN' THEN '부산'
                  WHEN 'DAEGU' THEN '대구'
                  WHEN 'JEONNAM_GWANGJU' THEN '광주'
                  WHEN 'DAEJEON' THEN '대전'
                  WHEN 'ULSAN' THEN '울산'
                  WHEN 'SEJONG' THEN '세종'
                  WHEN 'GANGWON' THEN '강원'
                  WHEN 'GYEONGNAM' THEN '경남'
                  WHEN 'GYEONGBUK' THEN '경북'
                  WHEN 'JEONBUK' THEN '전북'
                  WHEN 'CHUNGNAM' THEN '충남'
                  WHEN 'CHUNGBUK' THEN '충북'
                  WHEN 'JEJU' THEN '제주'
                  WHEN 'OVERSEAS' THEN '해외'
              END
 WHERE field_name = 'region';

UPDATE job_field_overrides
   SET value = CASE value
                  WHEN 'NATIONWIDE' THEN '전국'
                  WHEN 'SEOUL' THEN '서울'
                  WHEN 'GYEONGGI' THEN '경기'
                  WHEN 'INCHEON' THEN '인천'
                  WHEN 'BUSAN' THEN '부산'
                  WHEN 'DAEGU' THEN '대구'
                  WHEN 'JEONNAM_GWANGJU' THEN '광주'
                  WHEN 'DAEJEON' THEN '대전'
                  WHEN 'ULSAN' THEN '울산'
                  WHEN 'SEJONG' THEN '세종'
                  WHEN 'GANGWON' THEN '강원'
                  WHEN 'GYEONGNAM' THEN '경남'
                  WHEN 'GYEONGBUK' THEN '경북'
                  WHEN 'JEONBUK' THEN '전북'
                  WHEN 'CHUNGNAM' THEN '충남'
                  WHEN 'CHUNGBUK' THEN '충북'
                  WHEN 'JEJU' THEN '제주'
                  WHEN 'OVERSEAS' THEN '해외'
              END
 WHERE field_name = 'region';

UPDATE normalized_jobs
   SET region = CASE region
                  WHEN 'NATIONWIDE' THEN '전국'
                  WHEN 'SEOUL' THEN '서울'
                  WHEN 'GYEONGGI' THEN '경기'
                  WHEN 'INCHEON' THEN '인천'
                  WHEN 'BUSAN' THEN '부산'
                  WHEN 'DAEGU' THEN '대구'
                  WHEN 'JEONNAM_GWANGJU' THEN '광주'
                  WHEN 'DAEJEON' THEN '대전'
                  WHEN 'ULSAN' THEN '울산'
                  WHEN 'SEJONG' THEN '세종'
                  WHEN 'GANGWON' THEN '강원'
                  WHEN 'GYEONGNAM' THEN '경남'
                  WHEN 'GYEONGBUK' THEN '경북'
                  WHEN 'JEONBUK' THEN '전북'
                  WHEN 'CHUNGNAM' THEN '충남'
                  WHEN 'CHUNGBUK' THEN '충북'
                  WHEN 'JEJU' THEN '제주'
                  WHEN 'OVERSEAS' THEN '해외'
              END
 WHERE region IS NOT NULL;

UPDATE job_classifications
   SET region = CASE region
                  WHEN 'NATIONWIDE' THEN '전국'
                  WHEN 'SEOUL' THEN '서울'
                  WHEN 'GYEONGGI' THEN '경기'
                  WHEN 'INCHEON' THEN '인천'
                  WHEN 'BUSAN' THEN '부산'
                  WHEN 'DAEGU' THEN '대구'
                  WHEN 'JEONNAM_GWANGJU' THEN '광주'
                  WHEN 'DAEJEON' THEN '대전'
                  WHEN 'ULSAN' THEN '울산'
                  WHEN 'SEJONG' THEN '세종'
                  WHEN 'GANGWON' THEN '강원'
                  WHEN 'GYEONGNAM' THEN '경남'
                  WHEN 'GYEONGBUK' THEN '경북'
                  WHEN 'JEONBUK' THEN '전북'
                  WHEN 'CHUNGNAM' THEN '충남'
                  WHEN 'CHUNGBUK' THEN '충북'
                  WHEN 'JEJU' THEN '제주'
                  WHEN 'OVERSEAS' THEN '해외'
              END
 WHERE region IS NOT NULL;

ALTER TABLE normalized_jobs DROP COLUMN sub_region;
ALTER TABLE job_classifications DROP COLUMN sub_region;
