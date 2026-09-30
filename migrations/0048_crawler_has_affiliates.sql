-- 0048 사이트마다 계열사가 있는지 (2026-09-30 결정)
--
-- 공고에서 읽은 회사 이름은 수집할 때마다 조금씩 달랐다. 채널톡 사이트의 공고가 `채널톡` 과
-- `채널코퍼레이션` 으로 갈려 오공고에 같은 회사가 두 이름으로 쌓였다. 서비스 이름과 법인 이름은
-- 글자가 달라 규칙으로 같은 회사인지 알 수 없다.
--
-- 회사가 하나인 사이트가 대부분이라 기본은 0 이다. 그때 공고의 회사명은 사이트 추가에 넣은 이름
-- (`default_company`)으로 고정한다. 그룹 채용 사이트(삼성·동원)만 1 로 켠다. 그때는 공고에서 읽은
-- 계열사 이름이 회사명이고, 사이트 이름은 모회사다 (`app/normalize/engine.py` 의 `settle_company`).
--
-- 이미 있는 사이트는 전부 0 으로 시작한다. 그룹 사이트는 사이트 패널에서 켠다.
--
-- 되돌리기: 칸을 지운다.

-- migrate:up

ALTER TABLE crawlers ADD COLUMN has_affiliates INTEGER NOT NULL DEFAULT 0
    CHECK (has_affiliates IN (0, 1));

-- migrate:down

ALTER TABLE crawlers DROP COLUMN has_affiliates;
