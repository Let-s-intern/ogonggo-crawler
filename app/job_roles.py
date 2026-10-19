"""오공고 직군·직무 enum (LC-3385, 2026-09-29).

오공고가 직군·직무를 글자로 받다가 enum(`IT_DEVELOPMENT`, `IT_BACKEND`)으로 바꿨다. 한글
이름이 그대로 오면 400 이다. 크롤러 직무 분류표(`job_taxonomy`)는 오공고 enum 과 한글 이름이
하나하나 같아, 표와 공고에는 한글 이름을 그대로 두고 보낼 때만 enum 이름으로 바꾼다. 목록은 오공고
enum 을 옮긴 씨앗 파일에 있다 (`seeds/job-roles-ogonggo-20261019.json`). 오공고 enum 이 바뀌면 그
파일과 분류표 씨앗을 다시 뽑고, 쌓인 분류표와 공고 값은 마이그레이션으로 옮긴다.

직무는 직군마다 `기타` 같은 이름이 겹칠 수 있어 직군과 함께 찾는다. 오공고는 직군에 속하지 않는
직무를 400 으로 거절하므로, 직군을 모르면 직무도 보내지 않는다.

## 옛 이름

2026-10-12 오공고가 직군 여섯 개를 다섯 직군으로 합쳤고(`MERGED_FIELDS`), 2026-10-15 IT·개발 직무를
16개로 묶고(`MERGED_ROLES`) 기획이 뺀 직무 41개를 지웠다(`EXCLUDED_ROLES`, LC-3472). 크롤러는 처음에
보낼 때만 이름을 바꾸다가 2026-10-19 분류표와 공고 값을 오공고와 같게 옮겼다
(`migrations/0054_job_taxonomy_ogonggo.sql`). 그래도 옛 이름이 남은 곳(일경험 AI 답 등)이 있어,
새 씨앗에 없는 이름은 옛 씨앗(`seeds/job-roles-ogonggo-20260929.json`)으로 찾아 새 직무로 바꾼다.

지운 직무는 분류표에 꺼진 채 남고 공고 값도 그대로다. 보낼 때는 같은 직군의 "기타" 직무로 바꾸고,
자동 전송에서는 그 공고를 보내지 않는다 (`app/deliver/spring.py` 의 `target_sql`).
"""

from __future__ import annotations

import json
import pathlib
from functools import cache
from typing import Any

_SEEDS = pathlib.Path(__file__).resolve().parent.parent / "seeds"
SEED_PATH = _SEEDS / "job-roles-ogonggo-20261019.json"
# 2026-10-12 이전 오공고 enum. 옛 한글 이름을 찾을 때만 쓴다
LEGACY_SEED_PATH = _SEEDS / "job-roles-ogonggo-20260929.json"


def _load(path: pathlib.Path) -> list[dict[str, Any]]:
    return list(json.loads(path.read_text(encoding="utf-8"))["jobFields"])


@cache
def _seed() -> list[dict[str, Any]]:
    return _load(SEED_PATH)


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


@cache
def _role_fields() -> dict[str, str]:
    """직무 enum 이름 → 그 직군 enum 이름."""
    return {str(role["name"]): str(field["name"]) for field in _seed() for role in field["roles"]}


@cache
def _legacy_fields() -> dict[str, str]:
    return {str(field["label"]): str(field["name"]) for field in _load(LEGACY_SEED_PATH)}


@cache
def _legacy_roles() -> dict[str, tuple[str, str]]:
    """옛 직무 한글 이름 → (옛 직군 한글 이름, 옛 직무 enum 이름). 옛 직무 이름은 직군이 달라도
    겹치지 않는다 (`tests/test_job_roles.py`)."""
    return {
        str(role["label"]): (str(field["label"]), str(role["name"]))
        for field in _load(LEGACY_SEED_PATH)
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
# 그 직무의 공고는 자동으로 보내지 않는다. 옛 씨앗의 enum 이름이다
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


def _legacy_role(role: str) -> str | None:
    """옛 직무 한글 이름을 오공고 지금 직무 enum 이름으로."""
    found = _legacy_roles().get(role)
    if found is None:
        return None
    name = found[1]
    return MERGED_ROLES.get(name) or EXCLUDED_ROLES.get(name, name)


def enum_names(field: str | None, role: str | None) -> tuple[str | None, str | None]:
    """한글 직군·직무를 오공고 enum 이름으로. 목록 밖이면 None 이다 — 둘 다 선택 칸이다.

    새 씨앗에 없는 이름은 옛 이름으로 보고 오공고의 새 직군·직무로 바꾼다. 지운 직무는 같은 직군의
    "기타" 직무다. 직무가 그 직군 것이 아니면 직무는 보내지 않는다.
    """
    if not field:
        return None, None
    field_name = _fields().get(field)
    if field_name is None:
        legacy = _legacy_fields().get(field)
        field_name = None if legacy is None else MERGED_FIELDS.get(legacy, legacy)
    if field_name is None:
        return None, None
    if not role:
        return field_name, None
    role_name = _roles().get((field, role)) or _legacy_role(role)
    if role_name is None or _role_fields().get(role_name) != field_name:
        return field_name, None
    return field_name, role_name


@cache
def excluded_labels() -> tuple[tuple[str, str], ...]:
    """오공고에서 지운 직무의 한글 (직군, 직무). 자동 전송에서 뺀다.

    분류표를 옮긴 뒤(0054)의 합친 직군 이름과, 옮기기 전 옛 직군 이름을 둘 다 담는다.
    """
    labels = {name: label for label, name in _fields().items()}
    pairs: list[tuple[str, str]] = []
    for role, (old_field, name) in _legacy_roles().items():
        if name not in EXCLUDED_ROLES:
            continue
        legacy = _legacy_fields()[old_field]
        pairs.append((old_field, role))
        new_field = labels[MERGED_FIELDS.get(legacy, legacy)]
        if new_field != old_field:
            pairs.append((new_field, role))
    return tuple(pairs)


def field_labels() -> tuple[str, ...]:
    """오공고 직군 한글 이름. 씨앗 파일 순서 그대로다."""
    return tuple(_fields())


def role_labels(field: str) -> tuple[str, ...]:
    """직군 하나에 딸린 직무 한글 이름. 모르는 직군이면 비어 있다."""
    return tuple(role for (owner, role) in _roles() if owner == field)
