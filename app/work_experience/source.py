"""미래내일 일경험 프로그램을 공공데이터 CSV 에서 읽는다 (2026-11-16 결정, LC-3605).

포털(yw.work24.go.kr)을 직접 수집하던 길은 걷었다. 운영 서버 IP 가 포털 방화벽에 막혀 연결이
시간 초과로 끝났다 (2026-11-16 확인, 같은 기관의 www.work24.go.kr 은 열린다). 대신 한국고용정보원이
주는 `청년일경험포털_일경험 프로그램 정보` CSV 를 쓴다. 업데이트될 때마다 받는다.

**CSV 에는 프로그램 번호도 주소도 없다.** 그래서 사람이 로컬에서 포털 모집 중 목록과 맞춰 두 칸을
붙인 파일을 올린다.

| 붙인 칸 | 쓰임 |
|---|---|
| `프로그램번호` (`PG…`) | 프로그램을 가르는 값. 예전 수집기가 모은 행과 같은 번호면 같은 행이다 |
| `공유주소` (`…/d/a/prgmDtl/…`) | 오공고 원문 주소. GET 으로 열린다 (LC-3615) |

프로그램 번호가 없는 줄은 건너뛴다. 포털 모집 중 목록에 없던 줄이다 — 마감됐거나 내려갔다.

CSV 칸은 포털 상세 표와 거의 같다. 표 이름·칸 이름을 예전 상세 파서와 맞춰 둬서 AI 정리
(`fill.py`)와 오공고 전송(`deliver.py`)은 그대로 돈다.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date

BASE_URL = "https://yw.work24.go.kr"
LIST_URL = f"{BASE_URL}/d/a/selectWkexPrgmList.do"
DETAIL_URL = f"{BASE_URL}/d/a/selectItrnPrjtEsgPrgmDtal.do"
# 프로그램을 가르는 주소의 번호 이름. 예전 수집기와 같은 주소를 만들어야 그때 모은 행에 이어진다
ID_PARAM = "untyPrgmCtn"

ID_COLUMN = "프로그램번호"
SHARE_COLUMN = "공유주소"
# 이 칸이 없으면 매핑하지 않은 원본 CSV 다
REQUIRED_COLUMNS: tuple[str, ...] = ("프로그램명", "모집종료일", ID_COLUMN)
# 포털 유형 이름 → 유형 기호. 기호는 예전 목록 카드의 `fn_searchDetail('I', …)` 첫 값이다
TYPE_CODES = {"인턴형": "I", "프로젝트형": "P", "ESG지원형": "E", "기업탐방형": "C"}

_PROGRAM_ID = re.compile(r"PG[0-9A-Za-z]+")
_DATE = re.compile(r"(\d{4})\D?(\d{1,2})\D?(\d{1,2})")
_NUMBER = re.compile(r"\d[\d,]*")
_SHARE_PREFIX = f"{BASE_URL}/"


class SourceError(ValueError):
    """올린 파일을 읽을 수 없다. 사유를 화면이 그대로 옮긴다."""


@dataclass(frozen=True)
class ListItem:
    """프로그램 한 건의 고정 칸. 오공고 회사·지역·인원·모집기간이 여기서 정해진다."""

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
    # 오공고 원문 주소. 다시 정리할지 가르는 값(`card_values`)에는 넣지 않는다
    share_url: str = ""

    @property
    def source_url(self) -> str:
        return detail_url(self.program_id)

    def card_values(self) -> dict[str, str]:
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
    """AI 에게 줄 글. `sections` 는 표 이름 → 칸 이름 → 값이다."""

    program_id: str
    title: str
    type_label: str
    region: str
    job_and_headcount: str
    recruitment_start: date | None
    recruitment_end: date | None
    work_start: date | None
    work_end: date | None
    sections: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def source_url(self) -> str:
        return detail_url(self.program_id)

    def value(self, section: str, label: str) -> str:
        return self.sections.get(section, {}).get(label, "")


@dataclass
class Parsed:
    programs: list[tuple[ListItem, Program]] = field(default_factory=list)
    # 건너뛴 줄. `N번째 줄: 사유`
    skipped: list[str] = field(default_factory=list)
    rows: int = 0


def detail_url(program_id: str) -> str:
    """프로그램을 가르는 주소. GET 으로는 열리지 않는다 — 오공고에는 공유 주소를 보낸다."""
    return f"{DETAIL_URL}?{ID_PARAM}={program_id}"


def main_company(company: str) -> str:
    """참여기업 칸의 첫 기업. 프로젝트형은 `A, B, C` 처럼 여럿을 적는데 오공고 회사명은 하나다."""
    return company.split(",", 1)[0].strip()


def parse_csv(data: bytes) -> Parsed:
    """매핑한 CSV 를 읽는다. 인코딩은 UTF-8(BOM 있어도 됨)과 공공데이터 원본의 CP949 를 받는다."""
    reader = csv.DictReader(io.StringIO(_decode(data)))
    header = [name.strip() for name in reader.fieldnames or []]
    missing = [name for name in REQUIRED_COLUMNS if name not in header]
    if missing:
        raise SourceError(
            f"{', '.join(missing)} 칸이 없다."
            " 포털 목록과 맞춰 프로그램번호·공유주소를 붙인 CSV 를 올린다"
        )
    parsed = Parsed()
    seen: set[str] = set()
    for number, raw in enumerate(reader, start=2):
        parsed.rows += 1
        row = {str(key).strip(): (value or "").strip() for key, value in raw.items() if key}
        program_id = row.get(ID_COLUMN, "")
        end = _date(row.get("모집종료일", ""))
        if not _PROGRAM_ID.fullmatch(program_id):
            parsed.skipped.append(
                f"{number}번째 줄: 프로그램번호가 없다 ({row.get('프로그램명', '')})"
            )
            continue
        if end is None:
            parsed.skipped.append(f"{number}번째 줄: 모집종료일을 읽지 못했다 ({program_id})")
            continue
        if program_id in seen:
            parsed.skipped.append(f"{number}번째 줄: 앞 줄과 프로그램번호가 같다 ({program_id})")
            continue
        seen.add(program_id)
        parsed.programs.append(_program(row, program_id))
    return parsed


def _program(row: dict[str, str], program_id: str) -> tuple[ListItem, Program]:
    start, end = _date(row.get("모집시작일", "")), _date(row.get("모집종료일", ""))
    work_start, work_end = _date(row.get("운영시작일", "")), _date(row.get("운영종료일", ""))
    type_label = row.get("프로그램유형", "")
    title = row.get("프로그램명", "")
    region = row.get("지역", "")
    job = row.get("세부직무내용", "")
    headcount = _count(row.get("모집인원", ""))
    item = ListItem(
        program_id=program_id,
        type_code=TYPE_CODES.get(type_label.replace(" ", ""), ""),
        title=title,
        type_label=type_label,
        job=job,
        region=region,
        headcount=headcount,
        recruitment_start=start,
        recruitment_end=end,
        operator=row.get("운영기관명", ""),
        company=row.get("참여기업", ""),
        share_url=_share_url(row.get(SHARE_COLUMN, "")),
    )
    sections = {
        "프로그램정보": {
            "프로그램명": title,
            "프로그램 유형": type_label,
            "지역": region,
            "주요내용": row.get("주요내용", ""),
            "일경험 기간": _span(work_start, work_end),
            "평가계획": row.get("평가계획", ""),
            "코칭계획": row.get("코칭계획", ""),
            "해시태그": row.get("해시태그내용", ""),
        },
        "모집정보": {
            "직무(모집인원)": _job_and_headcount(job, headcount),
            "모집기간": _span(start, end),
            "모집내용": row.get("모집공고내용", ""),
            "선발기준내용": row.get("선발기준내용", ""),
        },
        "사전직무교육정보": {
            "교육기관": row.get("교육기관명", ""),
            "사전직무교육기간": _span(
                _date(row.get("교육시작일자", "")), _date(row.get("교육종료일자", ""))
            ),
            "교육과정내용": row.get("교육과정내용", ""),
            "전화번호": row.get("교육기관전화번호", ""),
        },
        "운영기관담당자정보": {
            "운영기관": row.get("운영기관명", ""),
            "담당자": row.get("운영담당자명", ""),
            "전화번호": row.get("운영기관전화번호", ""),
        },
        "참여기업정보": {"참여기업": row.get("참여기업", "")},
    }
    kept = {
        name: {label: value for label, value in cells.items() if value}
        for name, cells in sections.items()
    }
    program = Program(
        program_id=program_id,
        title=title,
        type_label=type_label,
        region=region,
        job_and_headcount=kept["모집정보"].get("직무(모집인원)", ""),
        recruitment_start=start,
        recruitment_end=end,
        work_start=work_start,
        work_end=work_end,
        sections={name: cells for name, cells in kept.items() if cells},
    )
    return item, program


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise SourceError("CSV 인코딩을 읽지 못했다. UTF-8 이나 CP949 로 저장한다")


def _share_url(value: str) -> str:
    """포털 공유 주소만 받는다. 포털이 붙이는 `:443` 은 덜어 낸다."""
    url = value.strip().replace("https://yw.work24.go.kr:443/", _SHARE_PREFIX, 1)
    return url if url.startswith(_SHARE_PREFIX) else ""


def _job_and_headcount(job: str, headcount: int | None) -> str:
    if not job:
        return ""
    return f"{job}({headcount})" if headcount else job


def _span(start: date | None, end: date | None) -> str:
    if start is None and end is None:
        return ""
    return f"{_iso(start)} ~ {_iso(end)}"


def _date(text: str) -> date | None:
    match = _DATE.search(text)
    if match is None:
        return None
    year, month, day = (int(part) for part in match.groups())
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


def _iso(value: date | None) -> str:
    return value.isoformat() if value else ""
