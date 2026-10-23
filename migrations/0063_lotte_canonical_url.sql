-- 0063 롯데 공고 주소에서 목록 위치를 뗀다 (LC-3504, 2026-10-23)
--
-- 롯데 목록 링크는 `/apply/announcement/detail/21995366?compcd=?id=bookmark1001` 이다. `bookmark1001`
-- 은 즐겨찾기 버튼 id 라 `1000 + 목록 순번` 이고, 새 공고가 위에 붙을 때마다 같은 공고의 주소가
-- 바뀌었다. 수집·전송·오공고가 모두 주소로 같은 공고를 가려서, 같은 공고가 오공고에 4~5번씩 들어갔다.
-- 앞으로는 목록 파서가 이 조각을 뗀다 (`app/crawler/canonical.py`).
--
-- 쌓인 주소도 같은 모양으로 옮긴다. 옮기지 않으면 다음 수집이 뗀 주소를 처음 보는 공고로 읽어 한 번
-- 더 보낸다. `?` 앞까지와 나눈 공고 번호(`#2`)를 남긴다.
--
-- 같은 공고가 여러 주소로 쌓였으니 옮기면 주소가 겹친다. 주소가 유일해야 하는 표는 먼저 옮긴 행 하나만
-- 남기고 나머지를 지운다. 오공고 전송 기록은 보낸 행을 먼저 옮긴다 — 그래야 그 공고가 보낸 공고로
-- 남는다. 수집 원본(`raw_jobs`)과 정규화(`normalized_jobs`)는 주소가 유일하지 않아 행을 지우지 않는다.
-- 오공고에 이미 여러 번 들어간 공고는 여기서 풀리지 않는다. 오공고에서 숨긴다.
--
-- 되돌리기: 하지 않는다. 뗀 목록 위치는 어디에도 남지 않고, 지운 행은 같은 공고의 겹친 기록이다.

-- migrate:up

UPDATE raw_jobs
   SET source_url = substr(source_url, 1, instr(source_url, '?') - 1)
       || CASE WHEN instr(source_url, '#') > 0 THEN substr(source_url, instr(source_url, '#')) ELSE '' END
 WHERE source_url LIKE 'https://recruit.lotte.co.kr/%?compcd=?id=bookmark%';

UPDATE normalized_jobs
   SET source_url = substr(source_url, 1, instr(source_url, '?') - 1)
       || CASE WHEN instr(source_url, '#') > 0 THEN substr(source_url, instr(source_url, '#')) ELSE '' END
 WHERE source_url LIKE 'https://recruit.lotte.co.kr/%?compcd=?id=bookmark%';

UPDATE OR IGNORE spring_deliveries
   SET source_url = substr(source_url, 1, instr(source_url, '?') - 1)
       || CASE WHEN instr(source_url, '#') > 0 THEN substr(source_url, instr(source_url, '#')) ELSE '' END
 WHERE source_url LIKE 'https://recruit.lotte.co.kr/%?compcd=?id=bookmark%'
   AND status = 'sent';

UPDATE OR IGNORE spring_deliveries
   SET source_url = substr(source_url, 1, instr(source_url, '?') - 1)
       || CASE WHEN instr(source_url, '#') > 0 THEN substr(source_url, instr(source_url, '#')) ELSE '' END
 WHERE source_url LIKE 'https://recruit.lotte.co.kr/%?compcd=?id=bookmark%';

DELETE FROM spring_deliveries
 WHERE source_url LIKE 'https://recruit.lotte.co.kr/%?compcd=?id=bookmark%';

UPDATE OR IGNORE screened_jobs
   SET source_url = substr(source_url, 1, instr(source_url, '?') - 1)
       || CASE WHEN instr(source_url, '#') > 0 THEN substr(source_url, instr(source_url, '#')) ELSE '' END
 WHERE source_url LIKE 'https://recruit.lotte.co.kr/%?compcd=?id=bookmark%';

DELETE FROM screened_jobs
 WHERE source_url LIKE 'https://recruit.lotte.co.kr/%?compcd=?id=bookmark%';

UPDATE OR IGNORE discarded_jobs
   SET source_url = substr(source_url, 1, instr(source_url, '?') - 1)
       || CASE WHEN instr(source_url, '#') > 0 THEN substr(source_url, instr(source_url, '#')) ELSE '' END
 WHERE source_url LIKE 'https://recruit.lotte.co.kr/%?compcd=?id=bookmark%';

DELETE FROM discarded_jobs
 WHERE source_url LIKE 'https://recruit.lotte.co.kr/%?compcd=?id=bookmark%';

-- migrate:down

-- 의도적으로 비워 둔다. 위 되돌리기 설명을 본다.
SELECT 1;
