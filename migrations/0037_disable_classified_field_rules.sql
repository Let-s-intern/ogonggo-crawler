-- 0037 AI 분류가 덮는 칸에 걸린 정규화 규칙을 끈다 (2026-09-15 결정)
--
-- 분류가 판정 칸·본문 칸·직군·직무·산업을 규칙이 만든 값 위에 덮어쓴다 (`app/normalize/engine.py` 의
-- `apply_classification`). 그 칸에 걸린 규칙은 분류가 끝난 공고에서 아무 일도 하지 않는데, 화면에서는
-- 켜져 있어 규칙이 먹는 줄로 읽혔다. 규칙 화면은 이제 수집 칸 다섯(`app/normalize/rules.py` 의
-- `RULE_FIELDS`)만 고르게 한다.
--
-- 지우지 않고 끈다. 메모 끝에 표시를 붙여, 되돌릴 때 이 마이그레이션이 끈 것만 다시 켠다.
--
-- 되돌리기: 표시가 붙은 규칙을 켜고 표시를 뗀다.

-- migrate:up

UPDATE normalization_rules
   SET enabled = 0,
       note = trim(coalesce(note, '') || ' [0037: AI 분류가 덮는 칸이라 껐다]')
 WHERE enabled = 1
   AND field_name NOT IN (
       'company_name', 'title', 'body', 'recruitment_end_at', 'recruitment_start_at'
   );

-- migrate:down

UPDATE normalization_rules
   SET enabled = 1,
       note = trim(replace(note, '[0037: AI 분류가 덮는 칸이라 껐다]', ''))
 WHERE note LIKE '%[0037: AI 분류가 덮는 칸이라 껐다]%';
