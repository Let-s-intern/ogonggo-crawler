-- `date_parse` 규칙의 `formats` 에서 연도가 없는 `%m/%d` 와 `%m.%d` 를 지운다.
--
-- 연도가 없는 형식은 `strptime` 이 연도를 1900 으로 채운다. 그래서 마감일을 `09/30` 으로
-- 적는 사이트가 나타나면 이렇게 된다.
--
--   1. `date_parse` 가 **성공**한다 → `1900-09-30`
--   2. `app/crawler/deadline.py` 의 `is_closed()` 가 오늘보다 이전이라 마감으로 읽는다
--   3. 그 공고의 상세를 열지 않는다. 실패가 아니라 `skipped_count` 다
--   4. 전부 건너뛴 실행은 `run_status()` 가 `success` 로 닫는다
--
-- 읽지 못한 값은 진행 중으로 두는 판정(`app/crawler/deadline.py`)이 이 경우를 막지 못한다.
-- 읽기에 실패한 것이 아니라 **성공했는데 값이 틀린 것**이라서다. 조용히 그 사이트의 공고가
-- 한 건도 들어오지 않는다.
--
-- 2026-09-10 기준 등록된 사이트 열한 곳의 마감일 표기에는 전부 연도가 있어서
-- (`.claude/site-recipes/`) 지금 이 두 형식에 도달하는 값은 없다. 터지기 전에 지운다.
--
-- 이 마이그레이션이 먼저 도는 이유가 있다. `app/normalize/rules.py` 의 `DateParseConfig` 가
-- 연도 없는 형식을 거부하게 바뀌는데, 그 검증은 저장할 때만이 아니라 **읽을 때도** 걸린다
-- (`app/normalize/engine.py` 의 `_rule_from_row`). 규칙을 정리하지 않고 코드만 배포하면
-- `load_rules()` 가 그 행에서 실패하고, 그 순간부터 모든 실행의 정규화가 통째로 멈춘다.
-- 컨테이너가 uvicorn 앞에서 마이그레이션을 돌리므로(`Dockerfile` 의 CMD) 순서는 보장된다.
--
-- 되돌리기: `down` 은 아무것도 하지 않는다. 지운 형식을 되살리면 1900 년을 만드는 설정이
-- 돌아오고, 새 검증이 그 행을 읽지 못해 정규화가 멈춘다. 되돌릴 이유가 있는 변경이 아니다.

-- migrate:up

-- 남는 형식이 하나도 없는 규칙은 지운다. `formats` 가 빈 배열이면 `DateParseConfig` 가
-- 거부하므로 그대로 두면 검증을 넣는 순간 그 행이 정규화를 멈춘다. 연도 없는 형식만 가진
-- 규칙은 1900 년만 만들어 내므로 남겨 둘 값이 없다 — 지우면 그 필드는 원문 그대로 남고,
-- 운영자가 화면에서 제대로 다시 넣을 수 있다.
DELETE FROM normalization_rules
 WHERE rule_type = 'date_parse'
   AND NOT EXISTS (
         SELECT 1
           FROM json_each(normalization_rules.rule_config_json, '$.formats') AS je
          WHERE je.value NOT IN ('%m/%d', '%m.%d')
       );

-- 나머지에서는 두 형식만 빼고 나열 순서를 그대로 둔다. `date_parse` 는 적힌 순서대로
-- 시도하므로 순서가 바뀌면 어느 형식이 먼저 잡을지가 달라진다.
UPDATE normalization_rules
   SET rule_config_json = json_set(
         rule_config_json,
         '$.formats',
         (SELECT json_group_array(je.value)
            FROM json_each(normalization_rules.rule_config_json, '$.formats') AS je
           WHERE je.value NOT IN ('%m/%d', '%m.%d'))
       )
 WHERE rule_type = 'date_parse'
   AND EXISTS (
         SELECT 1
           FROM json_each(normalization_rules.rule_config_json, '$.formats') AS je
          WHERE je.value IN ('%m/%d', '%m.%d')
       );

-- migrate:down
-- 의도적으로 비워 둔다. 위 되돌리기 설명을 본다.
SELECT 1;
