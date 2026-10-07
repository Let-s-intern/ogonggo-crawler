-- 0050 공고 분석 방법의 판 (2026-10-07 결정, LC-3446)
--
-- 오공고 공고 상세의 '공고 분석' 탭에 올라가는 정리를 AI 가 만든다. 그 방법(시스템 지시, 항목별 지침,
-- 참고 파일)을 분석 방법 화면에서 고치고, 저장할 때마다 한 행이 쌓인다. 가장 최근 행이 지금 방법이다.
-- 되돌리기도 새 행으로 저장하므로 행을 고치거나 지우는 경로가 없다 (`app/job_analysis/guide.py`).
--
-- 참고 파일은 `guide_json` 안에 내용째 들어간다. 판이 파일 이름만 가리키면 파일을 바꾼 뒤 옛 판으로
-- 되돌렸을 때 같은 프롬프트를 다시 만들 수 없다.
--
-- 되돌리기: 표를 지운다.

-- migrate:up

CREATE TABLE job_analysis_guide_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guide_json TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- migrate:down

DROP TABLE job_analysis_guide_versions;
