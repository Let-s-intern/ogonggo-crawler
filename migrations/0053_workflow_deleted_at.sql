-- 0053 사이트를 지운다 (2026-10-15 결정)
--
-- 사이트 화면에 지우기가 없어 쓰지 않는 사이트도 멈춘 채로 목록에 남았다. 지우면 사이트 목록과 매일
-- 수집, 전체 다시 수집에서 빠지고 스케줄러 잡도 없어진다. 행은 지우지 않고 지운 때만 적는다 —
-- `raw_jobs.workflow_id` 가 이 행을 가리키고, 그 사이트가 모은 공고와 분류·전송 기록은 공고 화면에
-- 그대로 남아야 한다. 이미 오공고로 보낸 공고도 서버에 그대로다.
--
-- 되돌리기: 칸을 지운다. 지운 사이트는 멈춘 사이트로 목록에 다시 보인다.

-- migrate:up

ALTER TABLE workflows ADD COLUMN deleted_at TEXT;

-- migrate:down

ALTER TABLE workflows DROP COLUMN deleted_at;
