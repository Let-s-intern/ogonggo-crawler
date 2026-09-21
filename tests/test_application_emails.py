"""지원 접수·채용 문의 이메일 수집 테스트 (0043, 2026-09-21 결정).

모델은 부르지 않는다. 근거 검사가 주소를 어떻게 거르는지, 프롬프트와 화면에 두 칸이 있는지 본다.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.classify.classifier import build_prompt, posting_only_names
from app.classify.grounding import NOT_AN_EMAIL, NOT_IN_SOURCE, ground
from tests.test_ui_review_actions import client, conn  # noqa: F401

SOURCE = "이력서는 recruit@ogonggo.com 으로 보내 주세요.\n채용 문의: HR@Ogonggo.com"


def test_원문에_있는_주소만_남긴다() -> None:
    grounded = ground(
        {"application_email": "recruit@ogonggo.com", "inquiry_email": "hr@ogonggo.com"},
        SOURCE,
        "제목",
    )

    assert grounded.fields["application_email"] == "recruit@ogonggo.com"
    # 대소문자만 다른 것은 같은 주소다. 모델이 적은 대로 남긴다
    assert grounded.fields["inquiry_email"] == "hr@ogonggo.com"
    assert grounded.dropped == []


def test_원문에_없는_주소는_지어낸_것이라_버린다() -> None:
    """틀린 주소 하나가 지원자를 엉뚱한 곳으로 보낸다."""
    grounded = ground({"application_email": "jobs@ogonggo.com"}, SOURCE, "제목")

    assert grounded.fields["application_email"] == ""
    assert grounded.reasons["application_email"] == NOT_IN_SOURCE


def test_주소가_아니면_버리고_앞말은_떼어_낸다() -> None:
    wrong = ground({"inquiry_email": "인사팀에 문의"}, SOURCE, "제목")
    prefixed = ground({"application_email": "이메일: recruit@ogonggo.com"}, SOURCE, "제목")

    assert wrong.fields["inquiry_email"] == ""
    assert wrong.reasons["inquiry_email"] == NOT_AN_EMAIL
    assert prefixed.fields["application_email"] == "recruit@ogonggo.com"


def test_주소가_없으면_빈_칸이고_버린_것도_아니다() -> None:
    grounded = ground({"application_email": ""}, SOURCE, "제목")

    assert grounded.fields["application_email"] == ""
    assert grounded.fields["inquiry_email"] == ""
    assert grounded.dropped == []


def test_프롬프트는_두_칸을_공고마다_묻는다() -> None:
    prompt, _ = build_prompt(SOURCE, "백엔드 개발자")

    judge = prompt.split("# 판정하는 칸")[1].split("# 공고 제목")[0]
    assert "- application_email:" in judge
    assert "- inquiry_email:" in judge
    assert {"application_email", "inquiry_email"} <= set(posting_only_names())


def test_공고_화면과_수집_항목_화면에_두_칸이_보인다(
    client: TestClient,  # noqa: F811
) -> None:
    panel = client.get("/ui/review/jobs/1/edit").text
    fields = client.get("/ui/fields").text

    assert 'name="application_email"' in panel
    assert 'name="inquiry_email"' in panel
    assert "applicationEmail" in fields
    assert "inquiryEmail" in fields
