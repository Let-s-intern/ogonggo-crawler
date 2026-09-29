-- 0047 부트캠프 로고 (2026-09-29 결정, LC-3389)
--
-- 오공고 `Bootcamp` 는 대표 이미지(`representativeImageUrl`)와 운영 회사 로고(`logoUrl`)를 따로 받는다.
-- 대표 이미지는 지금처럼 과정마다의 썸네일이고, 로고는 회사 화면에서 `새싹 ○○캠퍼스` 나 모회사
-- `새싹(SeSAC)` 에 올린 것이다. 공고 로고(0045)와 같은 순서로 고른다.
--
-- 로고는 과정 행이 아니라 회사 표에 있어 보낼 때 정한다. 여기에는 보낸 로고만 적는다 — 지금 로고와
-- 다르면 다시 보낸다. 로고를 나중에 올려도 이미 보낸 과정에 붙는다. NULL 은 로고 없이 보냈다는
-- 뜻이라 로고를 올리기 전에는 이미 보낸 과정을 다시 보내지 않는다.
--
-- 되돌리기: 칸을 지운다.

-- migrate:up

ALTER TABLE bootcamps ADD COLUMN sent_logo_url TEXT;

-- migrate:down

ALTER TABLE bootcamps DROP COLUMN sent_logo_url;
