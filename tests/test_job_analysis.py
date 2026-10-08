"""공고 분석 — 입력 공고, 답 다듬기, 다시 묻기, 분석 방법의 판 (2026-10-07, LC-3446).

AI 를 부르지 않는다. 제공자를 가짜로 바꿔 끼우고, 무엇을 묻고 받은 답을 어떻게 다듬는지 본다.

| 확인 | 깨지면 |
|---|---|
| 크롤러 행과 오공고 공고가 같은 입력이 된다 | 고용24 공고와 크롤러 공고의 분석 기준이 갈린다 |
| 빈 값은 null, 개수는 상한까지 | 프런트가 '공고에 명시 없음' 을 그리지 못하거나 카드가 넘친다 |
| 원문에 없는 문구의 역량은 다시 묻고, 그래도 없으면 뺀다 | 공고가 하지 않은 말이 '공고 속 문구' 로
나간다 |
| 화면의 지침과 파일이 프롬프트에 실린다 | 화면에서 고쳐도 분석이 바뀌지 않는다 |
| 판은 쌓이고 되돌리기도 새 판이다 | 어느 방법으로 만든 분석인지 알 수 없다 |
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from app import db
from app.job_analysis import analyzer, guide
from app.job_analysis.analyzer import AnalysisError, analyze, build_prompt
from app.job_analysis.posting import Posting
from app.llm.base import LlmCallError, Usage

POSTING = Posting.of(
    {
        "title": "[전환형 인턴] B2B AI Product Owner Intern",
        "companyName": "PFCT",
        "employmentType": "INTERN",
        "recruitmentType": "ALWAYS_OPEN",
        "responsibilities": (
            "• Zero to One 기획 — 가설을 설정하고, MVP를 기획하며, "
            "시장의 피드백을 통해 제품을 완성해 나가요."
        ),
        "qualifications": (
            "• Problem Solver: 모호한 요구 사항 속에서 핵심 문제를 정의하고 "
            "해결책을 도출하는 데 두려움이 없는 분"
        ),
        "hiringProcess": "서류 전형 → 1차 인터뷰 → 2차 인터뷰 → 처우 협의 → 최종 합류",
        "benefits": "",
    }
)


def answer(**changes: Any) -> dict[str, Any]:
    fact = {"value": "", "note": ""}
    body: dict[str, Any] = {
        "tasks": [{"tag": "기획", "text": "가설을 세우고 MVP를 기획해요."}],
        "required": ["Problem Solver — 모호한 요구사항에서 핵심 문제를 정의할 수 있는 분"],
        "preferred": [],
        "employment": {
            "type": {"value": "전환형 인턴십", "note": ""},
            "conversion": fact,
            "salary": fact,
            "affiliation": fact,
        },
        "submission": {
            "documents": fact,
            "essay": {"value": "별도 문항 없음", "note": ""},
            "process": {"value": "서류 → 1차 인터뷰 → 2차 인터뷰", "note": ""},
            "deadline": {"value": "", "note": "상시 채용으로 보여요"},
        },
        "competencies": [
            {
                "name": "문제 정의",
                "quote": "모호한 요구 사항 속에서 핵심 문제를 정의하고",
                "description": "진짜 풀어야 할 문제를 골라내는 역량이에요.",
                "experiences": ["a 한 경험", "b 한 경험", "c 한 경험", "d 한 경험"],
            }
        ],
    }
    body.update(changes)
    return body


class FakeProvider:
    name = "fake"

    def __init__(self, *answers: dict[str, Any] | str | Exception) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    def build_client(self, settings: Any) -> object:
        return object()

    async def call_model(
        self, client: Any, model: str, prompt: str, *args: Any, **kwargs: Any
    ) -> tuple[str, Usage]:
        self.prompts.append(prompt)
        self.systems.append(kwargs["system_instruction"])
        current = self.answers.pop(0)
        if isinstance(current, Exception):
            raise current
        text = current if isinstance(current, str) else json.dumps(current, ensure_ascii=False)
        return text, Usage(
            provider="fake",
            model=model,
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            latency_ms=1,
        )


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


def use(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> None:
    monkeypatch.setattr(analyzer, "for_feature", lambda feature, settings: (provider, "fake-model"))


def calls(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT feature, ok, rules_version FROM llm_calls ORDER BY id").fetchall()


def test_크롤러_전송_본문과_오공고_공고가_같은_입력이_된다() -> None:
    from app.deliver.spring import payload

    row = {
        "company_name": "PFCT",
        "parent_company_name": "",
        "title": "PO 인턴",
        "job_field": "",
        "job_role": "",
        "industry": "",
        "cover_image_url": "",
        "logo_url": "",
        "employment_type": "INTERN",
        "experience_type": "",
        "experience_min_years": None,
        "education_level": "",
        "region": "",
        "sub_region": "",
        "recruitment_type": "PERIOD",
        "recruitment_headcount": None,
        "recruitment_start_at": "",
        "recruitment_end_at": "2026-10-20 23:59:59",
        "closes_when_filled": None,
        "auto_close_enabled": None,
        "company_and_team_introduction": "",
        "responsibilities": "기획",
        "qualifications": "",
        "preferred_qualifications": "",
        "compensation": "",
        "benefits": "",
        "hiring_process": "",
        "recruitment_notice": "",
        "application_method": "",
        "application_email": "",
        "inquiry_email": "",
        "source_url": "https://x/1",
    }
    posting = Posting.of(payload(row))

    assert posting.contents == {"responsibilities": "기획"}
    assert "고용 형태: 인턴" in posting.text()
    assert "모집 마감: 2026-10-20" in posting.text()
    # 본문이 같으면 해시도 같다. 오공고 서버가 같은 규칙으로 세어 낡은 분석을 거른다
    assert (
        posting.content_hash()
        == Posting.of({"title": "PO 인턴", "responsibilities": "기획"}).content_hash()
    )


async def test_빈_값은_null_이고_개수는_상한까지다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider(answer(required=[f"조건 {n}" for n in range(12)]))
    use(monkeypatch, provider)

    result = await analyze(conn, POSTING, guide.DEFAULT_GUIDE, guide_version=0)

    data = result.analysis
    assert data["employment"]["salary"] == {"value": None, "note": None}
    assert data["submission"]["deadline"] == {"value": None, "note": "상시 채용으로 보여요"}
    assert len(data["required"]) == 8
    assert data["competencies"][0]["experiences"] == ["a 한 경험", "b 한 경험", "c 한 경험"]
    assert [tuple(row) for row in calls(conn)] == [("job_analysis", 1, 0)]


async def test_원문에_없는_문구는_다시_묻고_그래도_없으면_그_역량을_뺀다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    made_up = answer()
    made_up["competencies"] = [
        {**made_up["competencies"][0]},
        {"name": "리더십", "quote": "팀을 이끌어 본 분", "description": "", "experiences": []},
    ]
    provider = FakeProvider(made_up, made_up)
    use(monkeypatch, provider)

    result = await analyze(conn, POSTING, guide.DEFAULT_GUIDE)

    assert len(provider.prompts) == 2
    assert "- 리더십" in provider.prompts[1]
    assert [item["name"] for item in result.analysis["competencies"]] == ["문제 정의"]
    assert result.dropped == ("리더십",)


async def test_모양이_틀린_답은_한_번_더_묻는다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider("{}", answer())
    use(monkeypatch, provider)

    result = await analyze(conn, POSTING, guide.DEFAULT_GUIDE)

    assert "정해진 모양이 아니었다" in provider.prompts[1]
    assert result.analysis["tasks"][0]["tag"] == "기획"


async def test_호출이_실패하면_사유를_올리고_기록을_남긴다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    use(monkeypatch, FakeProvider(LlmCallError("api_error", "503")))

    with pytest.raises(AnalysisError) as raised:
        await analyze(conn, POSTING, guide.DEFAULT_GUIDE)

    assert raised.value.reason == "api_error"
    assert [tuple(row)[:2] for row in calls(conn)] == [("job_analysis", 0)]


async def test_본문이_없으면_부르지_않는다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider()
    use(monkeypatch, provider)

    with pytest.raises(AnalysisError):
        await analyze(conn, Posting.of({"title": "제목만"}), guide.DEFAULT_GUIDE)

    assert provider.prompts == []


def test_화면의_지침과_파일이_프롬프트에_실린다() -> None:
    edited = guide.Guide(
        system="너는 분석가다",
        sections={**guide.DEFAULT_GUIDE.sections, "tasks": "일은 두 개만 고른다"},
        files=(guide.GuideFile("역량가이드.md", "# 기획 직무 역량\n문제 정의"),),
    )

    prompt = build_prompt(edited, POSTING)

    assert "## 실제 하는 일 (tasks)\n\n일은 두 개만 고른다" in prompt
    assert "# 참고 자료" in prompt and "## 역량가이드.md" in prompt
    assert prompt.rstrip().endswith("최종 합류")


def test_판은_쌓이고_되돌리기도_새_판이다(conn: sqlite3.Connection) -> None:
    assert guide.current(conn).number == 0

    first = guide.save(conn, guide.Guide("지시 1", dict(guide.DEFAULT_GUIDE.sections)), "처음")
    second = guide.save(
        conn,
        guide.with_files(first.guide, (guide.GuideFile("a.md", "내용"),)),
        "파일 추가",
    )
    restored = guide.restore(conn, first.number)

    assert (first.number, second.number, restored.number) == (1, 2, 3)
    assert guide.current(conn).guide.files == ()
    assert guide.read(conn, 2).guide.files[0].content == "내용"
    assert [entry.note for entry in guide.history(conn)] == [
        "판 1 으로 되돌림",
        "파일 추가",
        "처음",
    ]
    with pytest.raises(guide.GuideError) as raised:
        guide.save(conn, restored.guide)
    assert raised.value.reason == "unchanged"


@pytest.mark.parametrize(
    ("name", "data", "reason"),
    [
        ("guide.pdf", b"x", "file_type"),
        ("guide.md", "가".encode("cp949"), "file_encoding"),
        ("guide.md", b"   ", "empty_file"),
        ("guide.txt", ("가" * (guide.MAX_FILE_CHARS + 1)).encode(), "too_long"),
    ],
)
def test_올린_파일을_검사한다(name: str, data: bytes, reason: str) -> None:
    with pytest.raises(guide.GuideError) as raised:
        guide.decode_file(name, data)
    assert raised.value.reason == reason


async def test_명시_없음이라고_적은_값은_빈_값으로_본다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    employment = answer()["employment"]
    employment["salary"] = {"value": "공고에 명시 없음", "note": "공고에 명시 없음"}
    employment["type"] = {"value": "인턴", "note": "인턴"}
    use(monkeypatch, FakeProvider(answer(employment=employment)))

    result = await analyze(conn, POSTING, guide.DEFAULT_GUIDE)

    assert result.analysis["employment"]["salary"] == {"value": None, "note": None}
    assert result.analysis["employment"]["type"] == {"value": "인턴", "note": None}


async def test_칸이_글자_하나로_와도_값으로_받는다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = answer()
    body["employment"] = {
        "type": "정규직",
        "conversion": "",
        "salary": "월 230만원",
        "affiliation": "",
    }
    provider = FakeProvider(body)
    use(monkeypatch, provider)

    result = await analyze(conn, POSTING, guide.DEFAULT_GUIDE)

    assert len(provider.prompts) == 1
    assert result.analysis["employment"]["type"] == {"value": "정규직", "note": None}
    assert result.analysis["employment"]["conversion"] == {"value": None, "note": None}
