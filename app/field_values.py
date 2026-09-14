"""0033 이 바꾼 판정 값과 모집 일시 (`migrations/0033_spring_job_values.sql`).

크롤러가 모은 공고를 오공고(Spring)로 보내므로 판정 칸을 오공고 enum 이름으로, 모집 일시를
`YYYY-MM-DD HH:MM:SS` 로 저장한다 (2026-09-14 결정). 마이그레이션은 이 서버의 DB 를 옮기고, 이
모듈은 그 전에 내보낸 DB 파일을 가져올 때 같은 표로 옮긴다 (`app/api/import_data.py`). 두 표가
갈리면 옛 파일에서 들어온 보정이 한글 값으로 남아 오공고로 보낼 수 없고, 옛 날짜 규칙이 시각을
떼어 마감 시각이 사라진다.
"""

from __future__ import annotations

import json
import re

# 이 버전보다 앞선 파일은 옛 값을 쓴다
CHANGED_IN = "0033"

START = "recruitment_start_at"
END = "recruitment_end_at"

# 한글 판정 값에서 오공고 enum 이름으로. 한글 목록에 없던 값은 옮기지 않는다
JUDGE_VALUES: dict[str, dict[str, str]] = {
    "employment_type": {
        "정규직": "FULL_TIME",
        "계약직": "CONTRACT",
        "인턴": "INTERN",
        "기타": "ETC",
    },
    "experience_type": {"신입": "NEWCOMER", "경력": "EXPERIENCED", "무관": "IRRELEVANT"},
    "education_level": {
        "무관": "ANY",
        "고졸": "HIGH_SCHOOL",
        "전문학사": "ASSOCIATE",
        "학사": "BACHELOR",
        "석사": "MASTER",
        "박사": "DOCTORATE",
    },
}

# 옛 정규화가 빈 마감에 채우던 글자
ALWAYS_OPEN_TEXT = "상시모집"

# 날짜만 있던 일시에 붙이는 시각
DAY_BOUNDARY: dict[str, str] = {END: " 23:59:59", START: " 00:00:00"}
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# 옛 마감일 규칙 중 시각을 떼던 regex 의 패턴. 이 규칙은 끈다
TIME_STRIP_PATTERN = r"\s*\d{1,2}\s*:\s*\d{2}(\s*:\s*\d{2})?\s*$"

# date_parse 가 시각이 있는 표기도 읽도록 뒤에 더하는 형식과, 새 출력 형식
DATETIME_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y.%m.%d %H:%M",
    "%Y.%m.%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y.%m.%d. %H:%M",
    "%Y년 %m월 %d일 %H:%M",
)
DATETIME_OUTPUT = "%Y-%m-%d %H:%M:%S"


def changed_before(version: str) -> bool:
    """그 마이그레이션 버전의 파일이 옛 값을 쓰는가."""
    return version < CHANGED_IN


def override_value(field_name: str, value: str) -> str:
    """사람 보정 값 하나를 옮긴다. 옮길 것이 없으면 그대로다."""
    if field_name in JUDGE_VALUES:
        return JUDGE_VALUES[field_name].get(value, value)
    if field_name == END and value == ALWAYS_OPEN_TEXT:
        return ""
    if field_name in DAY_BOUNDARY and _DATE.fullmatch(value):
        return value + DAY_BOUNDARY[field_name]
    return value


def rule(field_name: str, rule_type: str, config_json: str, enabled: int) -> tuple[str, int]:
    """정규화 규칙 하나를 옮긴다. 설정 글자와 켜짐을 돌려준다. 읽히지 않는 설정은 그대로 둔다."""
    if field_name not in DAY_BOUNDARY:
        return config_json, enabled
    try:
        config = json.loads(config_json)
    except json.JSONDecodeError:
        return config_json, enabled
    if not isinstance(config, dict):
        return config_json, enabled
    if rule_type == "regex" and field_name == END and config.get("pattern") == TIME_STRIP_PATTERN:
        return config_json, 0
    if rule_type == "date_parse":
        formats = config.get("formats")
        if isinstance(formats, list):
            config["formats"] = [
                *formats,
                *(item for item in DATETIME_FORMATS if item not in formats),
            ]
        config["output_format"] = DATETIME_OUTPUT
        return json.dumps(config, ensure_ascii=False), enabled
    return config_json, enabled
