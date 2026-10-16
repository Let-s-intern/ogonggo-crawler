"""직군·직무 한글 이름을 오공고 enum 이름으로 바꾸기 (`app/job_roles.py`).

| 확인 | 깨지면 |
|---|---|
| 씨앗의 enum 이름은 대문자 모양이고 겹치지 않는다 | 오공고가 읽지 못해 400 으로 거절한다 |
| 숫자가 든 이름도 통째로 옮긴다 | `B2B영업` 이 `B` 로 가 거절된다 (2026-09-30) |
| 직군을 모르면 직무도 보내지 않는다 | 직군에 속하지 않는 직무라며 거절된다 |
"""

from __future__ import annotations

import re

import pytest

from app import job_roles

# 직군은 `SALES` 처럼 한 낱말일 수 있다. 직무는 늘 직군 머리말이 붙어 밑줄이 있다
FIELD_NAME = re.compile(r"[A-Z][A-Z0-9_]*[A-Z0-9]")
ROLE_NAME = re.compile(r"[A-Z][A-Z0-9]*(_[A-Z0-9]+)+")


def test_씨앗의_enum_이름은_대문자_모양이고_겹치지_않는다() -> None:
    fields = list(job_roles._fields().values())
    roles = list(job_roles._roles().values())

    assert [name for name in fields if not FIELD_NAME.fullmatch(name)] == []
    assert [name for name in roles if not ROLE_NAME.fullmatch(name)] == []
    assert len(set(fields)) == len(fields)
    assert len(set(roles)) == len(roles)


@pytest.mark.parametrize(
    ("field", "role", "expected"),
    [
        ("영업", "B2B영업", ("SALES", "SALES_B2B")),
        ("영업", "B2C영업", ("SALES", "SALES_B2C")),
        ("IT·개발", "VR·AR·3D", ("IT_DEVELOPMENT", "IT_ETC")),
        ("게임", "게임3D모델링", ("GAME", "GAME_MODELING_3D")),
        ("디자인", "3D·VFX", ("PLANNING_STRATEGY", "PLANNING_ETC")),
    ],
)
def test_숫자가_든_이름도_통째로_옮긴다(field: str, role: str, expected: tuple[str, str]) -> None:
    """2026-09-30 전에는 씨앗을 뽑을 때 숫자 뒤 글자만 남아 `B`·`C`·`D` 가 갔다."""
    assert job_roles.enum_names(field, role) == expected


def test_직군을_모르면_직무도_보내지_않는다() -> None:
    assert job_roles.enum_names("없는 직군", "B2B영업") == (None, None)


@pytest.mark.parametrize(
    ("field", "role", "expected"),
    [
        ("AI·데이터", "데이터엔지니어", ("IT_DEVELOPMENT", "AI_DATA_ENGINEER")),
        ("미디어·엔터", None, ("MARKETING_ADVERTISING", None)),
        ("회계·세무·재무", None, ("HR_GENERAL_AFFAIRS", None)),
        ("게임", None, ("GAME", None)),
    ],
)
def test_오공고에서_합친_직군은_합쳐진_직군으로_보낸다(
    field: str, role: str | None, expected: tuple[str, str | None]
) -> None:
    """2026-10-12 오공고가 직군 여섯 개를 다섯 직군으로 합쳤다. 직무 이름은 그대로다."""
    assert job_roles.enum_names(field, role) == expected


@pytest.mark.parametrize(
    ("field", "role", "expected"),
    [
        ("IT·개발", "프론트엔드", ("IT_DEVELOPMENT", "IT_FRONTEND_FULLSTACK")),
        ("IT·개발", "DBA", ("IT_DEVELOPMENT", "IT_ETC")),
        ("AI·데이터", "데이터분석가", ("IT_DEVELOPMENT", "AI_DATA_ANALYST_SCIENTIST")),
        ("AI·데이터", "RAG", ("IT_DEVELOPMENT", "IT_ETC")),
        ("AI·데이터", "컴퓨터비전", ("IT_DEVELOPMENT", "AI_ETC")),
        ("미디어·엔터", "방송작가", ("MARKETING_ADVERTISING", "MARKETING_ETC")),
        ("고객상담·TM", "인바운드", ("HR_GENERAL_AFFAIRS", "HR_ETC")),
        ("회계·세무·재무", "IR·공시", ("HR_GENERAL_AFFAIRS", "ACCOUNTING_ETC")),
        ("디자인", "UXUI·프로덕트", ("PLANNING_STRATEGY", "DESIGN_UX_UI_PRODUCT")),
    ],
)
def test_오공고에서_묶거나_지운_직무는_새_직무로_보낸다(
    field: str, role: str, expected: tuple[str, str]
) -> None:
    """2026-10-15 오공고가 IT·개발 직무를 16개로 묶고 기획이 뺀 직무를 "기타" 로 옮겼다(LC-3472)."""
    assert job_roles.enum_names(field, role) == expected


def test_묶거나_지운_직무는_모두_씨앗에_있다() -> None:
    """오공고 표와 이름이 어긋나면 그 직무는 옛 이름으로 가 400 을 받는다."""
    seed = set(job_roles._roles().values())

    assert set(job_roles.MERGED_ROLES) - seed == set()
    assert set(job_roles.EXCLUDED_ROLES) - seed == set()
    assert len(job_roles.excluded_labels()) == len(job_roles.EXCLUDED_ROLES)
