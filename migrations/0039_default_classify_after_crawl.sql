-- 0039 수집 직후 AI 분류를 기본으로 켠다 (2026-09-17 결정, LC-3344)
--
-- 분류는 수집과 따로 도는 부가 워크플로우이고 (`0021_side_workflows.sql`), 새로 만들면 멈춘 채로
-- 시작한다. 그래서 사이트를 넣고 수집해도 운영자가 설정 > 시스템에서 자동 분류를 만들고 켜기 전까지
-- 공고가 분류되지 않았고, 매번 분류 버튼을 다시 눌러야 했다. 운영자가 원한 것은 수집 → 분류 →
-- 오공고 전송이 한 번에 이어지는 것이다.
--
-- 분류 워크플로우가 하나도 없을 때만 하나 넣는다. 이미 만들어 둔 곳은 그 설정을 그대로 둔다.
-- 대상은 아직 분류되지 않은 공고, 1회 상한은 분류 배치의 최대치 200 이다 — 첫 수집에서 공고가
-- 수십 건 들어와도 한 번에 끝나게 한다. 분류가 끝나면 전송 설정이 켜진 경우 오공고로 이어서
-- 보낸다 (`app/deliver/spring.py` 의 `deliver_after_classify`).
--
-- 되돌리기: 이 마이그레이션이 넣은 이름의 워크플로우와 그 실행 기록을 지운다.

-- migrate:up

INSERT INTO side_workflows (kind, name, status, trigger_kind, target_scope, batch_limit)
SELECT 'classify', '수집 직후 AI 분류', 'active', 'after_crawl', 'unclassified', 200
 WHERE NOT EXISTS (SELECT 1 FROM side_workflows WHERE kind = 'classify');

-- migrate:down

DELETE FROM side_runs
 WHERE side_workflow_id IN (SELECT id FROM side_workflows WHERE name = '수집 직후 AI 분류');
DELETE FROM side_workflows WHERE name = '수집 직후 AI 분류';
