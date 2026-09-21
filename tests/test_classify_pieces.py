"""뽑는 칸을 원문 그대로 옮기는지 (`app/classify/pieces.py`).

모델은 몇 번 줄의 어느 부분인지를 답하고, 저장하는 글자는 원문에서 잘라 온다. 모델이 글자를
바꿔 답해도 저장되는 글자는 원문이어야 한다.
"""

from __future__ import annotations

import pytest

from app.classify.classifier import classify_body
from app.classify.pieces import number_lines, render, resolve, strip_line_marks
from app.config import Settings
from tests.classify_fakes import response
from tests.test_selector_generator import FakeClient

TITLE = "백엔드 개발자 (경력)"
# 1 번 줄의 `결재` 는 원문 오타다. 모델이 고쳐 답해도 저장은 원문 글자여야 한다
BODY = (
    "주요업무 : 결재 서버 개발\n\n지원자격\n• Java 3년 이상 경험\n근무지: 성남 | 고용형태: 정규직\n"
)
LINES = number_lines(TITLE, BODY)


def test_제목이_0번이고_빈_줄은_번호를_받지_않는다() -> None:
    assert LINES == [
        "백엔드 개발자 (경력)",
        "주요업무 : 결재 서버 개발",
        "지원자격",
        "• Java 3년 이상 경험",
        "근무지: 성남 | 고용형태: 정규직",
    ]
    assert render(LINES[1:3], start=1) == "[1] 주요업무 : 결재 서버 개발\n[2] 지원자격"


def test_한_줄에_섞인_칸에서_그_부분만_잘라_온다() -> None:
    assert resolve([(4, "성남")], LINES).value == "성남"


def test_소제목이_붙은_줄에서_내용만_가져온다() -> None:
    assert resolve([(1, "결재 서버 개발")], LINES).value == "결재 서버 개발"


def test_글머리표가_달라도_원문_글자로_저장한다() -> None:
    resolved = resolve([(3, "- Java 3년 이상 경험")], LINES)

    assert resolved.value == "Java 3년 이상 경험"
    assert resolved.whole_lines == 0


def test_번호만_틀리면_다른_줄에서_찾는다() -> None:
    resolved = resolve([(2, "Java 3년 이상 경험")], LINES)

    assert resolved.value == "Java 3년 이상 경험"
    assert resolved.whole_lines == 0


def test_글자를_바꿨으면_짚은_줄_전체를_남긴다() -> None:
    """2026-09-10 결정. 내용이 빠지는 것보다 소제목이 섞이는 편이 낫다."""
    resolved = resolve([(1, "결제 서버 개발")], LINES)

    assert resolved.value == "주요업무 : 결재 서버 개발"
    assert resolved.whole_lines == 1
    assert resolved.lost == 0


def test_짚은_줄도_없고_어디에도_없으면_버린다() -> None:
    resolved = resolve([(99, "Kotlin 5년 이상")], LINES)

    assert resolved.value == ""
    assert resolved.lost == 1


def test_조각을_줄바꿈으로_잇고_같은_글자는_한_번만_남긴다() -> None:
    resolved = resolve(
        [(1, "결재 서버 개발"), (3, "Java 3년 이상 경험"), (3, "Java 3년 이상 경험")], LINES
    )

    assert resolved.value == "결재 서버 개발\nJava 3년 이상 경험"


def test_번호까지_옮겨_온_조각도_읽는다() -> None:
    assert resolve([(4, "[4] 성남")], LINES).value == "성남"
    assert strip_line_marks("[3] 가\n[4] 나") == "가\n나"


def test_글머리표뿐인_조각은_건너뛴다() -> None:
    resolved = resolve([(3, "-")], LINES)

    assert resolved.value == ""
    assert resolved.whole_lines == 0
    assert resolved.lost == 0


@pytest.mark.parametrize(
    ("line", "text", "expected"),
    [
        # 느슨한 비교가 문장부호를 걷어내 끝 괄호가 떨어지던 자리 (2026-09-13 실제 호출)
        ("ruWorkpl: 본사(서울 63빌딩)", "본사(서울 63빌딩)", "본사(서울 63빌딩)"),
        (
            "[Big Data센터] SW개발 (데이터 엔지니어링)",
            "SW개발 (데이터 엔지니어링)",
            "SW개발 (데이터 엔지니어링)",
        ),
        ("(필수) 자격증 소지자", "(필수) 자격증 소지자", "(필수) 자격증 소지자"),
        (
            "- 2년 이상 유관경력 보유하신 분.",
            "2년 이상 유관경력 보유하신 분.",
            "2년 이상 유관경력 보유하신 분.",
        ),
        # 모델이 닫는 괄호를 빠뜨려도 원문대로 닫는다
        ("ruWorkpl: 본사(서울 63빌딩)", "본사(서울 63빌딩", "본사(서울 63빌딩)"),
        # 글머리표는 되붙이지 않고, 모델이 괄호 없이 적은 이름에 괄호를 붙이지 않는다
        ("- 2년 이상 유관경력 보유하신 분.", "- 2년 이상", "2년 이상"),
        ("[Big Data센터] SW개발", "Big Data센터", "Big Data센터"),
    ],
)
def test_앞뒤_문장부호를_원문대로_살린다(line: str, text: str, expected: str) -> None:
    assert resolve([(1, text)], ["제목", line]).value == expected


async def test_분류가_조각을_원문_글자로_저장한다() -> None:
    client = FakeClient(
        response(
            responsibilities=[{"line": 1, "text": "결제 서버 개발"}],
            qualifications=[{"line": 3, "text": "- Java 3년 이상 경험"}],
        )
    )

    result = await classify_body(
        BODY,
        title=TITLE,
        settings=Settings(gemini_api_key="테스트키", gemini_model="gemini-3.5-flash"),
        client=client,
    )

    assert result.postings[0].fields["responsibilities"] == "주요업무 : 결재 서버 개발"
    assert result.postings[0].fields["qualifications"] == "Java 3년 이상 경험"
    assert result.postings[0].dropped == []
    assert any("짚은 줄 전체" in note for note in result.notes)

    prompt = client.calls[0]["contents"]
    assert "[0] 백엔드 개발자 (경력)" in prompt
    assert "[4] 근무지: 성남 | 고용형태: 정규직" in prompt
