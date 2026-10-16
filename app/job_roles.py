"""오공고 직군·직무 enum (LC-3385, 2026-09-29).

오공고가 직군·직무를 글자로 받다가 enum(`IT_DEVELOPMENT`, `IT_BACKEND`)으로 바꿨다. 한글
이름이 그대로 오면 400 이다. 크롤러 직무 분류표(`job_taxonomy`)는 오공고 enum 과 한글 이름이
하나하나 같아, 표와 공고에는 한글 이름을 그대로 두고 보낼 때만 enum 이름으로 바꾼다. 목록은 오공고
enum 을 옮긴 씨앗 파일에 있다 (`seeds/job-roles-ogonggo-20260929.json`). 오공고 enum 이 바뀌면 그
파일을 다시 뽑는다.

직무는 직군마다 `기타` 같은 이름이 겹칠 수 있어 직군과 함께 찾는다. 오공고는 직군에 속하지 않는
직무를 400 으로 거절하므로, 직군을 모르면 직무도 보내지 않는다.

2026-10-12 오공고가 직군 여섯 개를 다섯 직군으로 합치고 그 직무를 합쳐진 직군 아래로 옮겼다
(`MERGED_FIELDS`). 크롤러 분류표와 씨앗 파일은 그대로 두고, 보낼 때 직군만 합쳐진 직군으로 바꾼다.
직무 enum 이름은 오공고도 바꾸지 않았다.

2026-10-15 오공고가 IT·개발 직무를 16개로 묶고, 기획이 뺀 직무 41개를 지웠다(LC-3472). 크롤러
분류표와 씨앗 파일은 그대로 두고, 보낼 때 직무를 묶은 직무로 바꾼다(`MERGED_ROLES`). 뺀 직무는
같은 직군의 "기타" 직무로 바꾸고(`EXCLUDED_ROLES`), 자동 전송에서는 그 공고를 보내지 않는다
(`app/deliver/spring.py` 의 `TARGET_SQL`).
"""

from __future__ import annotations

import json
import pathlib
from functools import cache
from typing import Any

SEED_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "seeds" / "job-roles-ogonggo-20260929.json"
)


@cache
def _seed() -> list[dict[str, Any]]:
    return list(json.loads(SEED_PATH.read_text(encoding="utf-8"))["jobFields"])


@cache
def _fields() -> dict[str, str]:
    return {str(field["label"]): str(field["name"]) for field in _seed()}


@cache
def _roles() -> dict[tuple[str, str], str]:
    return {
        (str(field["label"]), str(role["label"])): str(role["name"])
        for field in _seed()
        for role in field["roles"]
    }


# 오공고에서 없어진 직군 → 합쳐진 직군 (2026-10-12). 오공고 `JobField` 주석과 같다
MERGED_FIELDS: dict[str, str] = {
    "AI_DATA": "IT_DEVELOPMENT",
    "MERCHANDISING": "PLANNING_STRATEGY",
    "DESIGN": "PLANNING_STRATEGY",
    "ACCOUNTING_TAX_FINANCE": "HR_GENERAL_AFFAIRS",
    "CUSTOMER_SERVICE_TM": "HR_GENERAL_AFFAIRS",
    "MEDIA_ENTERTAINMENT": "MARKETING_ADVERTISING",
}


# 오공고에서 묶은 직무 → 묶은 직무 (2026-10-15, LC-3472). 오공고 `JobRole` 과 같다
MERGED_ROLES: dict[str, str] = {
    "IT_FRONTEND": "IT_FRONTEND_FULLSTACK",
    "IT_FULLSTACK": "IT_FRONTEND_FULLSTACK",
    "IT_WEB_PUBLISHING": "IT_WEB",
    "IT_DEVOPS_SRE": "IT_INFRA_DEVOPS",
    "IT_SYSTEM_NETWORK": "IT_INFRA_DEVOPS",
    "IT_EMBEDDED": "IT_EMBEDDED_IOT_ROBOTICS",
    "IT_ROBOTICS_SOFTWARE": "IT_EMBEDDED_IOT_ROBOTICS",
    "IT_IOT": "IT_EMBEDDED_IOT_ROBOTICS",
    "IT_CROSS_PLATFORM": "IT_ETC",
    "IT_DBA": "IT_ETC",
    "IT_SYSTEM_SOFTWARE": "IT_ETC",
    "IT_SOFTWARE_ENGINEER": "IT_ETC",
    "IT_QA": "IT_ETC",
    "IT_APPLICATION": "IT_ETC",
    "IT_BLOCKCHAIN": "IT_ETC",
    "IT_XR_3D": "IT_ETC",
    "IT_GRAPHICS": "IT_ETC",
    "IT_HARDWARE_ENGINEER": "IT_ETC",
    "AI_RAG": "IT_ETC",
    "AI_NLP": "IT_ETC",
    "AI_DATA_ANALYST": "AI_DATA_ANALYST_SCIENTIST",
    "AI_DATA_SCIENTIST": "AI_DATA_ANALYST_SCIENTIST",
    "AI_ML_ENGINEER": "AI_ML_MLOPS",
    "AI_MLOPS": "AI_ML_MLOPS",
    "AI_GENERATIVE_AI": "AI_LLM_GENERATIVE_AI",
    "AI_LLM": "AI_LLM_GENERATIVE_AI",
    "AI_MULTIMODAL_ENGINEER": "AI_ETC",
    "AI_VISION_SPEECH": "AI_ETC",
    "AI_AUTONOMOUS_DRIVING": "AI_ETC",
    "AI_COMPUTER_VISION": "AI_ETC",
    "AI_BUSINESS": "AI_ETC",
    "AI_SERVICE_PLANNING": "AI_ETC",
    "AI_RESEARCH": "AI_ETC",
}

# 오공고에서 지운 직무 → 같은 직군의 "기타" 직무 (2026-10-15, LC-3472).
# 그 직무의 공고는 자동으로 보내지 않는다
EXCLUDED_ROLES: dict[str, str] = {
    "MEDIA_BROADCAST_WRITER": "MARKETING_ETC",
    "MEDIA_CINEMATOGRAPHER": "MARKETING_ETC",
    "MEDIA_CG_MOTION_GRAPHICS": "MARKETING_ETC",
    "MEDIA_SOUND_DESIGNER": "MARKETING_ETC",
    "MEDIA_REPORTER": "MARKETING_ETC",
    "MEDIA_ANNOUNCER_SHOW_HOST": "MARKETING_ETC",
    "MEDIA_VOICE_ACTOR_NARRATOR": "MARKETING_ETC",
    "MEDIA_MODEL_ACTOR": "MARKETING_ETC",
    "MEDIA_PHOTOGRAPHER": "MARKETING_ETC",
    "MEDIA_BROADCAST_ENGINEERING": "MARKETING_ETC",
    "MEDIA_TRANSMISSION_PROGRAMMING": "MARKETING_ETC",
    "MEDIA_ARTIST_MANAGEMENT": "MARKETING_ETC",
    "MEDIA_DISTRIBUTION_PRODUCTION": "MARKETING_ETC",
    "MEDIA_MUSIC_RECORDS": "MARKETING_ETC",
    "MEDIA_WEBTOON_WEB_NOVEL": "MARKETING_ETC",
    "MEDIA_PUBLISHING": "MARKETING_ETC",
    "MEDIA_TRANSLATION_INTERPRETATION": "MARKETING_ETC",
    "MEDIA_EXHIBITION_CURATOR": "MARKETING_ETC",
    "MEDIA_ETC": "MARKETING_ETC",
    "HR_RECRUITER": "HR_ETC",
    "ACCOUNTING_IR_DISCLOSURE": "ACCOUNTING_ETC",
    "ACCOUNTING_BOOKKEEPING": "ACCOUNTING_ETC",
    "CS_INBOUND": "HR_ETC",
    "CS_OUTBOUND": "HR_ETC",
    "CS_ETC": "HR_ETC",
    "DESIGN_WEB": "PLANNING_ETC",
    "DESIGN_GRAPHIC_VISUAL": "PLANNING_ETC",
    "DESIGN_ILLUSTRATOR": "PLANNING_ETC",
    "DESIGN_BRANDING": "PLANNING_ETC",
    "DESIGN_SPACE_INTERIOR_VMD": "PLANNING_ETC",
    "DESIGN_INDUSTRIAL_PRODUCT": "PLANNING_ETC",
    "DESIGN_PACKAGE": "PLANNING_ETC",
    "DESIGN_ADVERTISING_CONTENT": "PLANNING_ETC",
    "DESIGN_VIDEO_MOTION": "PLANNING_ETC",
    "DESIGN_VFX_3D": "PLANNING_ETC",
    "DESIGN_PUBLISHING_EDITORIAL": "PLANNING_ETC",
    "DESIGN_ARCHITECTURE_LANDSCAPE": "PLANNING_ETC",
    "DESIGN_FASHION_TEXTILE": "PLANNING_ETC",
    "DESIGN_ETC": "PLANNING_ETC",
    "PLANNING_TECH_PLANNING": "PLANNING_ETC",
    "SALES_FINANCE_INSURANCE": "SALES_ETC",
}


def enum_names(field: str | None, role: str | None) -> tuple[str | None, str | None]:
    """한글 직군·직무를 오공고 enum 이름으로. 목록 밖이면 None 이다 — 둘 다 선택 칸이다.

    합쳐진 직군의 직무는 직군을 합쳐진 직군으로 바꿔 보낸다. 오공고에서 그 직무의 직군이 바뀌었다.
    묶거나 지운 직무는 오공고의 새 직무로 바꿔 보낸다.
    """
    seed_field = _fields().get(field) if field else None
    if seed_field is None:
        return None, None
    field_name = MERGED_FIELDS.get(seed_field, seed_field)
    if not role:
        return field_name, None
    role_name = _roles().get((str(field), role))
    if role_name is None:
        return field_name, None
    return field_name, MERGED_ROLES.get(role_name) or EXCLUDED_ROLES.get(role_name, role_name)


@cache
def excluded_labels() -> tuple[tuple[str, str], ...]:
    """오공고에서 지운 직무의 한글 (직군, 직무). 자동 전송에서 뺀다."""
    return tuple(key for key, name in _roles().items() if name in EXCLUDED_ROLES)
