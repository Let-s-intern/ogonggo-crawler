-- 0062 폐기한 까닭 (2026-10-22 결정)
--
-- 지운 공고만 폐기하다가(0061), AI 분류가 직군을 대상 직군 밖으로 정한 공고도 폐기한다
-- (`app/discard.py`). 둘을 가르려고 까닭을 남긴다. `operator` 는 운영자가 공고 목록에서 지운 것,
-- `off_target` 은 분류 뒤 대상 직군 밖이라 버린 것이다. 이미 있는 행은 운영자가 지운 것이다.
--
-- 되돌리기: 칸을 지운다.

-- migrate:up

ALTER TABLE discarded_jobs ADD COLUMN reason TEXT NOT NULL DEFAULT 'operator';

-- migrate:down

ALTER TABLE discarded_jobs DROP COLUMN reason;
