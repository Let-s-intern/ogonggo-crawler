-- 0036 오공고(Spring) 전송 기록 (2026-09-15 결정)
--
-- 분류가 끝난 공고를 오공고 관리자 API 로 등록하고, 받은 공고 id 를 원문 주소마다 한 행으로 적는다
-- (`app/deliver/spring.py`). 보낸 공고가 바뀌는 일은 드물어 수정·삭제를 따라가지 않으므로 행 하나에
-- 지금 상태만 둔다 — 이력을 쌓지 않는다.
--
-- 원문 주소로 잇는다. 정규화 행은 재정규화로 다시 쓰이고 검수 화면에서 지워지기도 하지만, 오공고가
-- 공고를 가르는 값은 `sourceUrl` 이다. 직무마다 나뉜 공고는 주소 끝에 `#번호` 가 붙어 새 행이 된다.
--
-- | 칸 | 뜻 |
-- |---|---|
-- | `spring_job_id` | 오공고가 돌려준 공고 id. 실패한 행은 NULL 이다 |
-- | `status` | `sent` 면 다시 보내지 않는다. `failed` 는 `attempts` 가 3 이 될 때까지 다시 보낸다 |
-- | `last_error` | 마지막 거절 사유. 전달 화면이 보여준다 |
--
-- 0036 전의 `deliver_method`·`deliver_auth_header` 설정 행은 더 읽지 않는다. 키는 크롤러 `.env` 의
-- `OGONGGO_INTERNAL_API_KEY` 에서 읽는다. 지우지 않고 둔다.
--
-- 되돌리기: 표를 지운다. 오공고에 이미 등록된 공고는 그대로다.

-- migrate:up

CREATE TABLE spring_deliveries (
    source_url    TEXT PRIMARY KEY,
    spring_job_id INTEGER,
    status        TEXT NOT NULL CHECK (status IN ('sent', 'failed')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT NOT NULL DEFAULT '',
    sent_at       TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- migrate:down

DROP TABLE spring_deliveries;
