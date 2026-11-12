-- 0067 미래내일 일경험 공유 주소 (2026-11-12 결정, LC-3615)
--
-- 포털 상세 주소는 POST 로만 열려, 오공고 지원하기 버튼이 `GET Parameter 조회를 지원하지 않습니다`
-- 화면으로 갔다. 목록 카드의 공유 버튼이 들고 있는 단축 주소(`/d/a/prgmDtl/…`)는 GET 으로 열린다.
-- 이 주소를 오공고 `sourceUrl` 로 보낸다.
--
-- 수집기 안에서 프로그램을 가르는 값은 그대로 `source_url`(상세 주소 + 프로그램 번호)이다. 공유 주소는
-- 카드마다 읽어 `share_url` 에 두고, 오공고에 보낸 주소를 `sent_share_url` 에 적는다. 둘이 다르면 다시
-- 보낸다 — 이미 보낸 프로그램도 다음 수집에서 공유 주소를 읽으면 오공고 id 로 교체된다. 상세와 AI 는
-- 다시 부르지 않는다. NULL 은 아직 공유 주소를 읽지 않았다는 뜻이다.
--
-- 되돌리기: 칸을 지운다.

-- migrate:up

ALTER TABLE work_experiences ADD COLUMN share_url TEXT;
ALTER TABLE work_experiences ADD COLUMN sent_share_url TEXT;

-- migrate:down

ALTER TABLE work_experiences DROP COLUMN sent_share_url;
ALTER TABLE work_experiences DROP COLUMN share_url;
