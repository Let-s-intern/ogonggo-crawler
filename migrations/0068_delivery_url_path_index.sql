-- 0068 보낸 공고 주소 경로 인덱스 (LC-3645, 2026-11-18)
--
-- 오공고 전송의 중복 판정(`app/deliver/spring.py` 의 `DUPLICATE_SQL`)이 도메인만 다르고 경로가
-- 같은 공고를 찾는다. 바이트랩은 `bitelab.ninehire.site` 와 `bitelab.co.kr`, 피알앤디는
-- `career.heydealer.com` 과 `prnd.co.kr` 두 사이트로 등록돼 같은 공고 번호가 두 번 갔다.
--
-- 경로는 `https://` 뒤 첫 `/` 부터 끝까지다(쿼리·`#번호` 포함). 내장 함수만 쓴 식 인덱스라
-- 앱 밖(sqlite3 셸)에서 써도 깨지지 않는다. 판정 SQL 은 이 식을 글자 그대로 써야 인덱스를 탄다
-- (`app/deliver/spring.py` 의 `URL_PATH_SQL`). 찾은 보낸 주소로 정규화 행을 찾는 인덱스도 함께 단다.
--
-- 되돌리기: 인덱스 둘을 지운다.

-- migrate:up

CREATE INDEX idx_spring_deliveries_url_path
    ON spring_deliveries (substr(source_url, instr(substr(source_url, 9), '/') + 8));

CREATE INDEX idx_normalized_jobs_source_url ON normalized_jobs (source_url);

-- migrate:down

DROP INDEX idx_normalized_jobs_source_url;

DROP INDEX idx_spring_deliveries_url_path;
