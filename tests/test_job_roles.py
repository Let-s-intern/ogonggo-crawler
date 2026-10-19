"""직군·직무 한글 이름을 오공고 enum 이름으로 바꾸기 (`app/job_roles.py`).

| 확인 | 깨지면 |
|---|---|
| 씨앗의 enum 이름은 대문자 모양이고 겹치지 않는다 | 오공고가 읽지 못해 400 으로 거절한다 |
| 숫자가 든 이름도 통째로 옮긴다 | `B2B영업` 이 `B` 로 가 거절된다 (2026-09-30) |
| 직군을 모르면 직무도 보내지 않는다 | 직군에 속하지 않는 직무라며 거절된다 |
| 새 씨앗과 분류표 씨앗이 오공고 직군·직무와 같다 | AI 가 오공고에 없는 직무를 고른다 |
| 옛 이름도 오공고의 새 직군·직무로 보낸다 | 옮기지 못한 일경험 답이 400 을 받는다 |
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from app import job_roles

# 직군은 `SALES` 처럼 한 낱말일 수 있다. 직무는 늘 직군 머리말이 붙어 밑줄이 있다
FIELD_NAME = re.compile(r"[A-Z][A-Z0-9_]*[A-Z0-9]")
ROLE_NAME = re.compile(r"[A-Z][A-Z0-9]*(_[A-Z0-9]+)+")
TAXONOMY_SEED = (
    pathlib.Path(__file__).parent.parent / "seeds" / "job-taxonomy-ogonggo-20261019.json"
)


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
        ("게임", "게임3D모델링", ("GAME", "GAME_MODELING_3D")),
        ("IT·개발", "임베디드·IoT·로봇", ("IT_DEVELOPMENT", "IT_EMBEDDED_IOT_ROBOTICS")),
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
    """2026-10-12 오공고가 직군 여섯 개를 다섯 직군으로 합쳤다. 옛 직군 이름이 남은 값이다."""
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
        ("IT·개발", "서버·백엔드", ("IT_DEVELOPMENT", "IT_BACKEND")),
        ("기획·전략", "웹디자인", ("PLANNING_STRATEGY", "PLANNING_ETC")),
        ("마케팅·광고", "방송작가", ("MARKETING_ADVERTISING", "MARKETING_ETC")),
        ("기획·전략", "3D·VFX", ("PLANNING_STRATEGY", "PLANNING_ETC")),
    ],
)
def test_오공고에서_묶거나_지운_직무는_새_직무로_보낸다(
    field: str, role: str, expected: tuple[str, str]
) -> None:
    """2026-10-15 오공고가 IT·개발 직무를 16개로 묶고 기획이 뺀 직무를 "기타" 로 옮겼다(LC-3472).

    옛 직군·직무 이름이 남은 값과, 0054 가 합친 직군 아래로 옮긴 지운 직무다.
    """
    assert job_roles.enum_names(field, role) == expected


def test_묶거나_지운_직무는_옛_씨앗에_있고_바꾼_직무는_새_씨앗에_있다() -> None:
    """오공고 표와 이름이 어긋나면 그 직무는 옛 이름으로 가 400 을 받는다."""
    legacy = {name for _, name in job_roles._legacy_roles().values()}
    seed = set(job_roles._roles().values())

    assert set(job_roles.MERGED_ROLES) - legacy == set()
    assert set(job_roles.EXCLUDED_ROLES) - legacy == set()
    assert set(job_roles.MERGED_ROLES.values()) - seed == set()
    assert set(job_roles.EXCLUDED_ROLES.values()) - seed == set()


def test_분류표_씨앗은_오공고_직군_직무와_같다() -> None:
    """분류표 씨앗과 enum 씨앗은 같은 오공고 enum 에서 뽑았다 (LC-3472, 2026-10-19)."""
    tree = json.loads(TAXONOMY_SEED.read_text(encoding="utf-8"))["majors"]

    assert [major["name"] for major in tree] == list(job_roles._fields())
    assert {(major["name"], minor) for major in tree for minor in major["minors"]} == set(
        job_roles._roles()
    )
    assert (len(job_roles._fields()), len(job_roles._roles())) == (19, 229)


def test_옛_직무_이름은_직군이_달라도_겹치지_않는다() -> None:
    """옛 직무는 이름만으로 찾는다. 겹치면 다른 직군의 직무로 바뀐다."""
    legacy = job_roles._load(job_roles.LEGACY_SEED_PATH)
    labels = [role["label"] for field in legacy for role in field["roles"]]

    assert len(labels) == len(set(labels)) == len(job_roles._legacy_roles())


def test_지운_직무는_옮긴_뒤와_옮기기_전_이름_모두_자동_전송에서_뺀다() -> None:
    excluded = set(job_roles.excluded_labels())

    assert {("기획·전략", "웹디자인"), ("디자인", "웹디자인")} <= excluded
    assert ("HR·총무", "리크루터·헤드헌터") in excluded
    assert ("기획·전략", "UXUI·프로덕트디자인") not in excluded
