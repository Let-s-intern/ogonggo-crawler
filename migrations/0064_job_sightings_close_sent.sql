-- 0064 사라진 상시 채용 공고를 오공고에서 마감한 결과 (2026-10-28 결정)
--
-- 0063 이 목록에서 연달아 사라진 상시 채용 공고를 마감으로 본다. 설정에서 켜면 그 공고를 오공고
-- `POST /api/v1/internal/jobs/{jobId}/close` 로 마감한다 (`app/deliver/spring.py` 의
-- `deliver_closes`). 보낸 때와 실패 사유, 시도 수를 남긴다. 시도 수가 상한에 닿으면 더 보내지 않는다.
--
-- 되돌리기: 세 칸을 지운다.

-- migrate:up

ALTER TABLE job_sightings ADD COLUMN close_sent_at TEXT;

ALTER TABLE job_sightings ADD COLUMN close_error TEXT;

ALTER TABLE job_sightings ADD COLUMN close_attempts INTEGER NOT NULL DEFAULT 0;

-- migrate:down

ALTER TABLE job_sightings DROP COLUMN close_attempts;

ALTER TABLE job_sightings DROP COLUMN close_error;

ALTER TABLE job_sightings DROP COLUMN close_sent_at;
