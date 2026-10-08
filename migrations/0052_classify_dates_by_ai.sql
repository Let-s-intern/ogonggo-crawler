-- 0052 모집 기간을 AI 가 정했는지 (2026-10-08 결정, LC-3446)
--
-- 사이트에서 마감일을 읽은 공고는 그 값을 그대로 오공고로 보냈다. AI 는 사이트에서 못 읽은 공고만 짚고,
-- 본문 분류 응답의 "다르면 제안" 칸은 긴 응답 안에서 거의 채워지지 않아 사실상 검수가 없었다.
-- 이제 모집 시작·마감은 사이트에서 읽었더라도 AI 에게 사이트 값과 원문을 함께 보여 주고, AI 가 고른
-- 값을 무조건 쓴다 — 못 찾았다고 답하면 빈 값이다 (`app/classify/basics.py`, `app/normalize/engine.py`).
--
-- 빈 값이 "AI 가 없다고 했다" 인지 "AI 에게 묻지 않았다(이 마이그레이션 전 분류, 호출 실패)" 인지를
-- 가르려고 이 칸을 둔다. 1 이면 모집 시작·마감 두 칸은 AI 값이 사이트 값보다 먼저다.
--
-- 되돌리기: 칸을 지운다.

-- migrate:up

ALTER TABLE job_classifications ADD COLUMN dates_by_ai INTEGER NOT NULL DEFAULT 0;

-- migrate:down

ALTER TABLE job_classifications DROP COLUMN dates_by_ai;
