-- 0045 공고 로고 (2026-09-28 결정)
--
-- 오공고(Spring) `Job` 은 대표 이미지(`coverImageUrl`)와 기업 로고(`logoUrl`)를 따로 받는다. 0035 는
-- 둘을 한 칸에 섞어 회사 로고가 먼저, 없으면 og:image 로 `cover_image_url` 을 채웠다. 이제 나눈다.
--
-- - `cover_image_url`: 수집이 공고 페이지에서 읽은 og:image (`raw_data_json.og_image_url`)
-- - `logo_url`: 회사 화면에서 등록한 로고(자회사 → 모회사). 없으면 수집이 읽은 사이트 아이콘
--   (`raw_data_json.site_icon_url`). 등록한 로고가 있으면 그 회사 공고 전부가 그 로고를 쓴다
--
-- 정규화가 정해 저장한다 (`app/normalize/engine.py` 의 `cover_image`, `logo_image`). 사람 보정은
-- 받지 않는다 — 로고는 공고마다가 아니라 회사마다 고친다. 그래서 보정·제안 표의 CHECK 는 그대로다.
--
-- 이미 쌓인 공고는 재정규화하면 등록한 로고가 채워지고, 사이트 아이콘은 원문을 다시 수집해야 붙는다.
--
-- 되돌리기: 칸을 지운다.

-- migrate:up

ALTER TABLE normalized_jobs ADD COLUMN logo_url TEXT;

-- migrate:down

ALTER TABLE normalized_jobs DROP COLUMN logo_url;
