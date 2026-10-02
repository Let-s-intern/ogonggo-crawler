"""청년일경험 포털의 미래내일 일경험 프로그램 목록과 상세를 읽는다 (2026-10-02 결정, LC-3432).

미래내일 일경험은 고용노동부 청년 일경험 지원사업이다. 운영기관이 참여기업과 묶어 프로그램을 열고,
청년이 신청해 몇 주 동안 그 기업에서 일해 본다. 오공고에는 고용 형태가 `미래내일 일경험`
(`WORK_EXPERIENCE`)인 채용공고로 들어간다.

공고 크롤러처럼 LLM 이 셀렉터를 만들지 않는다. 사이트가 하나이고 틀이 정해져 있어 고정 파서가 싸고
확실하다 — 새싹 부트캠프와 같은 이유다 (`app/bootcamp/sesac.py`). 틀이 바뀌면 여기서
`PortalParseError` 가 나고 실행 기록에 남는다.

**목록도 상세도 POST 로만 열린다.** 상세 주소를 GET 으로 열면 `GET Parameter 조회를 지원하지
않습니다` 화면이 뜬다. 그래도 원문 주소는 상세 주소에 프로그램 번호를 붙인 것으로 둔다
(2026-10-02 결정) — 프로그램마다 하나뿐인 주소가 필요하고, 오공고가 그 주소로 같은 공고를 가른다.

| 자리 | 읽는 것 |
|---|---|
| 목록 `selectWkexPrgmList.do` | 카드마다 번호·유형·제목·직무·지역·인원·모집기간·운영기관·참여기업 |
| 상세 `selectItrnPrjtEsgPrgmDtal.do` | 표의 칸 이름과 값. 주요내용·모집내용·선발기준·담당자 |

목록은 모집이 끝나지 않은 프로그램만 보여 준다. 기업탐방형(`C`)은 상세 주소가 다른데 2026년 2월에
끝나 목록에 나오지 않는다. 나오면 읽지 않고 건너뛴다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from bs4 import BeautifulSoup, Tag

BASE_URL = "https://yw.work24.go.kr"
LIST_URL = f"{BASE_URL}/d/a/selectWkexPrgmList.do"
DETAIL_URL = f"{BASE_URL}/d/a/selectItrnPrjtEsgPrgmDtal.do"
# 상세를 여는 프로그램 번호 이름. 목록의 `fn_searchDetail('I','PG…')` 두 번째 값이다
ID_PARAM = "untyPrgmCtn"
# 한 쪽에 48개까지 받는다. 2026-10-02 에 모집 중 384건, 8쪽이다. 끝없이 넘기지 않게 상한을 둔다
PAGE_SIZE = 48
MAX_PAGES = 30
# 최근 모집일순. 새 프로그램이 앞에 온다
SORT_RECENT = "C"
# 인턴형(I)·프로젝트형(P)·ESG지원형(E) 만 이 상세 주소로 열린다
READABLE_TYPES = frozenset({"I", "P", "E"})
# 지역이 `지역무관` 인 프로그램은 오공고 시·도 `전국` 이다. 나머지 지역 이름은 오공고 시·도 화면
# 이름과 글자가 같다 (`app/regions.py`)
ANY_REGION = "지역무관"

_DETAIL_CALL = re.compile(r"fn_searchDetail\('([A-Z])'\s*,\s*'([A-Za-z0-9]+)'\)")
_DATE = re.compile(r"(\d{2,4})[.-](\d{1,2})[.-](\d{1,2})")
_NUMBER = re.compile(r"\d[\d,]*")
_SPACES = re.compile(r"[ \t ]+")
# 값 칸 끝에 붙은 버튼 글자. 화면에서만 뜻이 있다
_BUTTON_WORDS = ("위치보기", "미리보기")


class PortalParseError(ValueError):
    """포털 페이지의 틀이 바뀌어 읽을 수 없다. 고정 파서를 고쳐야 한다."""


@dataclass(frozen=True)
class ListItem:
    """목록 카드 하나. 상세 없이도 오공고 칸 몇 개가 여기서 정해진다."""

    program_id: str
    type_code: str
    title: str
    type_label: str = ""
    job: str = ""
    region: str = ""
    headcount: int | None = None
    recruitment_start: date | None = None
    recruitment_end: date | None = None
    operator: str = ""
    company: str = ""

    @property
    def source_url(self) -> str:
        return detail_url(self.program_id)

    def card_values(self) -> dict[str, str]:
        """다시 읽을지 가르는 카드 값. 모집기간이나 인원이 바뀌면 상세를 다시 받는다."""
        return {
            "title": self.title,
            "job": self.job,
            "region": self.region,
            "headcount": str(self.headcount or ""),
            "recruitment_start": _iso(self.recruitment_start),
            "recruitment_end": _iso(self.recruitment_end),
            "company": self.company,
        }


@dataclass(frozen=True)
class Program:
    """상세 페이지 하나. 칸 이름이 표에 적힌 그대로인 값들과, 날짜로 읽은 기간들이다."""

    program_id: str
    title: str
    type_label: str
    region: str
    job_and_headcount: str
    recruitment_start: date | None
    recruitment_end: date | None
    work_start: date | None
    work_end: date | None
    # 표의 칸 이름 → 값. 같은 이름이 두 표에 있으면(`전화번호`) 표 이름을 앞에 붙인다
    sections: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def source_url(self) -> str:
        return detail_url(self.program_id)

    def value(self, section: str, label: str) -> str:
        return self.sections.get(section, {}).get(label, "")


def list_form(page: int) -> dict[str, str]:
    """목록 한 쪽을 묻는 폼 본문. 검색 조건은 비워 모집 중인 프로그램 전부를 받는다."""
    return {
        "currentPageNo": str(page),
        "recordCountPerPage": str(PAGE_SIZE),
        "sortOption": SORT_RECENT,
    }


def detail_url(program_id: str) -> str:
    """원문 주소. GET 으로는 열리지 않지만 프로그램마다 하나다 (2026-10-02 결정)."""
    return f"{DETAIL_URL}?{ID_PARAM}={program_id}"


def main_company(company: str) -> str:
    """참여기업 칸의 첫 기업. 프로젝트형은 `A, B, C` 처럼 여럿을 적는데 오공고 회사명은 하나다."""
    return company.split(",", 1)[0].strip()


def detail_form(program_id: str) -> dict[str, str]:
    return {ID_PARAM: program_id}


def total_count(html: str) -> int | None:
    """목록 머리의 `총 N건`. 못 읽으면 None 이다."""
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("div.total strong")
    match = _NUMBER.search(_text(node)) if node else None
    return int(match.group(0).replace(",", "")) if match else None


def parse_list(html: str) -> list[ListItem]:
    """목록 한 쪽의 카드들. 틀이 바뀌어 카드가 하나도 없으면 빈 목록이다 — 부르는 쪽이 판단한다."""
    soup = BeautifulSoup(html, "html.parser")
    items: list[ListItem] = []
    for card in soup.select("ul.card-list > li"):
        link = card.select_one("div.link a")
        found = _DETAIL_CALL.search(str(link.get("href", ""))) if link else None
        if link is None or found is None:
            continue
        values = _card_values(card)
        start, end = _period(values.get("모집기간", ""))
        items.append(
            ListItem(
                program_id=found.group(2),
                type_code=found.group(1),
                title=_text(link.select_one("strong") or link),
                type_label=_text(card.select_one("div.label i.label-txt")),
                job=values.get("직무", ""),
                region=values.get("지역", ""),
                headcount=_count(values.get("모집인원", "")),
                recruitment_start=start,
                recruitment_end=end,
                operator=values.get("운영기관", ""),
                company=values.get("참여기업", ""),
            )
        )
    return items


def _card_values(card: Tag) -> dict[str, str]:
    values: dict[str, str] = {}
    for row in card.select("ul.list > li"):
        label = row.select_one("strong")
        value = row.select_one("span")
        if label is None or value is None:
            continue
        values[_text(label)] = _text(value)
    return values


def parse_detail(html: str, program_id: str) -> Program:
    """상세 페이지 하나. 프로그램명과 모집기간이 없으면 틀이 바뀐 것이다."""
    soup = BeautifulSoup(html, "html.parser")
    sections: dict[str, dict[str, str]] = {}
    for table in soup.select("table"):
        caption = table.select_one("caption")
        name = _section_name(_text(caption)) if caption else ""
        cells = sections.setdefault(name, {})
        for row in table.select("tr"):
            for label, value in _pairs(row):
                cells.setdefault(label, value)

    program = sections.get("프로그램정보", {})
    recruiting = sections.get("모집정보", {})
    title = program.get("프로그램명", "")
    start, end = _period(recruiting.get("모집기간", ""))
    if not title or end is None:
        raise PortalParseError(
            f"상세에서 프로그램명이나 모집기간을 읽지 못했다({program_id}). 틀이 바뀌었는지 본다"
        )
    work_start, work_end = _period(program.get("일경험 기간", ""))
    return Program(
        program_id=program_id,
        title=title,
        type_label=program.get("프로그램 유형", ""),
        region=program.get("지역", ""),
        job_and_headcount=recruiting.get("직무(모집인원)", ""),
        recruitment_start=start,
        recruitment_end=end,
        work_start=work_start,
        work_end=work_end,
        sections={name: cells for name, cells in sections.items() if cells},
    )


def _section_name(caption: str) -> str:
    """`모집정보 표 - 직무, …` 의 `모집정보`. 띄어 쓴 이름(`참여기업 정보`)은 붙인다."""
    head = caption.split(" 표", 1)[0]
    return head.replace(" ", "")


def _pairs(row: Tag) -> list[tuple[str, str]]:
    """한 줄의 `th` 와 바로 뒤 `td` 짝들. 한 줄에 두 짝이 있는 표가 있다(`프로그램 유형`·`지역`)."""
    pairs: list[tuple[str, str]] = []
    cells = row.find_all(["th", "td"])
    for index, cell in enumerate(cells[:-1]):
        following = cells[index + 1]
        if cell.name == "th" and following.name == "td":
            pairs.append((_text(cell), _block(following)))
    return pairs


def _block(cell: Tag) -> str:
    """값 칸의 글. 줄바꿈을 살리고 빈 줄과 버튼 글자를 덜어 낸다."""
    lines: list[str] = []
    for raw in cell.get_text("\n").split("\n"):
        line = _SPACES.sub(" ", raw.replace("​", "")).strip()
        for word in _BUTTON_WORDS:
            line = line.removesuffix(word).strip()
        if line and line not in _BUTTON_WORDS:
            lines.append(line)
    return "\n".join(lines)


def _period(text: str) -> tuple[date | None, date | None]:
    """`26-09-28 ~ 26-10-02` 나 `2026-09-28 ~ 2026-10-02`. 한쪽만 있으면 그쪽만 날짜다."""
    head, _, tail = text.partition("~")
    return _date(head), _date(tail)


def _date(text: str) -> date | None:
    match = _DATE.search(text)
    if match is None:
        return None
    year, month, day = (int(part) for part in match.groups())
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _count(text: str) -> int | None:
    match = _NUMBER.search(text)
    if match is None:
        return None
    number = int(match.group(0).replace(",", ""))
    return number if number > 0 else None


def _text(node: Tag | None) -> str:
    if node is None:
        return ""
    return _SPACES.sub(" ", node.get_text(" ").replace("​", "")).strip()


def _iso(value: date | None) -> str:
    return value.isoformat() if value else ""
