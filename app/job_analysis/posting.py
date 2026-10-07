"""분석에 넣는 공고 한 건.

크롤러가 모은 공고만이 아니라 고용24 공고(오공고 서버가 직접 모은다)도 분석한다. 둘에 공통으로 있는
것은 오공고 `jobs` 의 본문 여덟 칸이라 분석 입력을 그 여덟 칸과 제목·회사·고용 형태·모집 기간으로
맞춘다. 사용자가 '원문 공고' 탭에서 보는 것도 이 여덟 칸이라, 분석의 '공고 속 문구' 를 같은 글에서
찾을 수 있다.

칸 이름은 오공고 요청 칸 이름(camelCase)을 쓴다. 크롤러 정규화 행은 전송 본문을 만드는
`app/deliver/spring.py` 의 `payload` 로 같은 모양이 되고, 오공고 서버가 내주는 분석 대상도 같은
이름으로 온다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# 본문 칸과 프롬프트에 적는 이름. 순서가 프롬프트의 순서다
CONTENT_FIELDS: tuple[tuple[str, str], ...] = (
    ("companyAndTeamIntroduction", "회사·팀 소개"),
    ("responsibilities", "주요 업무"),
    ("qualifications", "자격 요건"),
    ("preferredQualifications", "우대 사항"),
    ("compensation", "급여·처우"),
    ("benefits", "복지·혜택"),
    ("hiringProcess", "채용 절차"),
    ("recruitmentNotice", "채용 안내사항"),
)

EMPLOYMENT_LABELS: dict[str, str] = {
    "FULL_TIME": "정규직",
    "CONTRACT": "계약직",
    "INTERN": "인턴",
    "PART_TIME": "아르바이트·시간제",
    "WORK_EXPERIENCE": "미래내일 일경험",
    "ETC": "기타",
}

# 한 칸이 이보다 길면 자른다. 회사 소개를 통째로 붙인 공고가 있어도 호출 하나가 한없이 커지지 않게
MAX_FIELD_CHARS = 6000
# 붙여 넣은 원문의 상한. 칸 여덟 개를 합친 만큼이다
MAX_PASTED_CHARS = 20000


@dataclass(frozen=True)
class Posting:
    """공고 한 건. `contents` 는 `CONTENT_FIELDS` 의 칸 이름에서 본문으로, 빈 칸은 없다.

    `body` 는 칸으로 나뉘지 않은 원문이다. 분석 방법 화면에서 원문을 붙여 넣어 시험할 때만 쓴다.
    """

    title: str
    company: str = ""
    employment_type: str = ""
    recruitment_type: str = ""
    recruitment_end_at: str = ""
    contents: dict[str, str] = field(default_factory=dict)
    body: str = ""

    @classmethod
    def of(cls, job: Mapping[str, Any]) -> Posting:
        """오공고 요청 칸 이름으로 된 사전에서 읽는다.

        크롤러 전송 본문과 오공고 분석 대상이 이 모양이다.
        """
        return cls(
            title=_text(job.get("title")),
            company=_text(job.get("companyName")),
            employment_type=_text(job.get("employmentType")),
            recruitment_type=_text(job.get("recruitmentType")),
            recruitment_end_at=_text(job.get("recruitmentEndAt")),
            contents={
                name: _text(job.get(name)) for name, _ in CONTENT_FIELDS if _text(job.get(name))
            },
        )

    def text(self) -> str:
        """AI 에게 주는 공고 글. 칸마다 `## 이름` 을 두고 그 아래에 본문을 그대로 적는다."""
        head = [f"제목: {self.title or '(없음)'}"]
        if self.company:
            head.append(f"회사: {self.company}")
        if self.employment_type:
            head.append(
                f"고용 형태: {EMPLOYMENT_LABELS.get(self.employment_type, self.employment_type)}"
            )
        if self.recruitment_type == "ALWAYS_OPEN":
            head.append("모집 기간: 상시 채용")
        elif self.recruitment_end_at:
            head.append(f"모집 마감: {self.recruitment_end_at}")
        blocks = ["\n".join(head)]
        for name, label in CONTENT_FIELDS:
            body = self.contents.get(name, "")
            if body:
                blocks.append(f"## {label}\n{body[:MAX_FIELD_CHARS]}")
        if self.body:
            blocks.append(f"## 본문\n{self.body[:MAX_PASTED_CHARS]}")
        return "\n\n".join(blocks)

    def source(self) -> str:
        """'공고 속 문구' 를 찾는 글. 사용자가 원문 탭에서 보는 제목과 본문 여덟 칸이다."""
        return "\n".join([self.title, *self.contents.values(), self.body])

    def content_hash(self) -> str:
        """분석이 어느 본문에 대한 것인지 가르는 해시. 오공고 서버도 같은 규칙으로 센다."""
        data = {
            "title": self.title,
            **{name: self.contents.get(name, "") for name, _ in CONTENT_FIELDS},
        }
        text = json.dumps(data, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @property
    def empty(self) -> bool:
        return not self.contents and not self.body


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
