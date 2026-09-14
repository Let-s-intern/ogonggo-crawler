"""0031 이 바꾼 칸 이름 (`migrations/0031_spring_field_names.sql`).

크롤러가 모은 공고를 오공고(Spring)로 보내므로 칸 이름을 오공고 `Job` 엔티티의 칼럼 이름에 맞췄다
(2026-09-14 결정). 마이그레이션은 이 서버의 DB 를 옮기고, 이 모듈은 그 전에 내보낸 DB 파일을 가져올
때 같은 표로 옮긴다 (`app/api/import_data.py`). 두 표가 갈리면 옛 파일에서 들어온
셀렉터·원본·규칙·보정이 이 서버가 모르는 이름으로 남아 조용히 빈다.

옛 `job_role`(제목에서 옮긴 자유 글자)은 오공고에 받을 칸이 없어 지웠다. 그 이름은 직무 분류의
소분류가 받았으므로, 옛 파일의 `job_role` 규칙·보정은 옮기지 않고 버린다.
"""

from __future__ import annotations

import json

# 이 버전보다 앞선 파일은 옛 이름을 쓴다
RENAMED_IN = "0031"

# 수집 원본·셀렉터에 있는 이름
COLLECTED: dict[str, str] = {
    "company": "company_name",
    "deadline": "recruitment_end_at",
    "start_date": "recruitment_start_at",
    "requirements": "qualifications",
    "career_level": "experience_type",
    "work_location": "region",
    "duties": "responsibilities",
    "preferred": "preferred_qualifications",
    "etc_info": "recruitment_notice",
}

# 규칙·보정·제안의 `field_name`. 직무 분류 둘은 수집 원본에는 없고 분류가 채운다
FIELD_NAMES: dict[str, str] = {
    **COLLECTED,
    "job_major": "job_field",
    "job_minor": "job_role",
}

# 받을 칸이 없어 지운 옛 이름
DROPPED = "job_role"


def renamed_before(version: str) -> bool:
    """그 마이그레이션 버전의 파일이 옛 이름을 쓰는가."""
    return version < RENAMED_IN


def record(data: dict[str, object]) -> dict[str, object]:
    """수집 원본 한 건의 키를 옮긴다. 값은 그대로다."""
    return {COLLECTED.get(key, key): value for key, value in data.items()}


def field_name(name: str) -> str | None:
    """규칙·보정의 옛 칸 이름을 옮긴다. 갈 곳이 없는 옛 자유 글자 직무는 None 이다."""
    if name == DROPPED:
        return None
    return FIELD_NAMES.get(name, name)


def selectors_json(text: str | None) -> str | None:
    """`crawlers.selectors_json` 의 목록·상세 칸 이름을 옮긴다. 읽히지 않는 값은 그대로 둔다."""
    data = _load(text)
    if data is None:
        return text
    for section in ("list", "detail"):
        part = data.get(section)
        if isinstance(part, dict):
            data[section] = record(part)
    return json.dumps(data, ensure_ascii=False)


def api_config_json(text: str | None) -> str | None:
    """`crawlers.api_config_json` 의 목록·상세 `fields` 칸 이름을 옮긴다.

    요청 본문(`body`)은 사이트에 보내는 값이라 칸 이름이 아니므로 그대로 둔다.
    """
    data = _load(text)
    if data is None:
        return text
    for section in ("list", "detail"):
        part = data.get(section)
        if isinstance(part, dict) and isinstance(part.get("fields"), dict):
            part["fields"] = record(part["fields"])
    return json.dumps(data, ensure_ascii=False)


def _load(text: str | None) -> dict[str, object] | None:
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
