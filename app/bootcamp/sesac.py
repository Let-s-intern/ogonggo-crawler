"""새싹(SeSAC) 오프라인 과정 목록과 상세를 읽는다 (2026-09-22 결정, LC-3364).

새싹은 서울시 청년취업사관학교다. 오프라인 과정이 오공고의 부트캠프에 해당한다. 온라인 과정은 짧은
동영상 강의라 부트캠프가 아니어서 읽지 않는다.

**모집 상태와 상관없이 전부 읽는다** (2026-09-22 결정). 모집중은 몇 건뿐이고 대부분
운영중·과정종료다.
신청이 끝난 과정은 오공고에 모집 마감으로 들어간다 (`spring_status`).

공고 크롤러처럼 LLM 이 셀렉터를 만들지 않는다. 부트캠프 사이트는 새싹 하나이고 틀이 정해져 있어
고정 파서가 싸고 확실하다. 틀이 바뀌면 여기서 `SesacParseError` 가 나고 실행 기록에 남는다.

| 자리 | 읽는 것 |
|---|---|
| 목록 `courseList.do` | 과정마다 `crsSn`·모집 상태·썸네일 |
| 상세 머리 태그 | 모집 상태·캠퍼스·분야 |
| 상세 강의 정보 표 | 모집기간·교육기간·교육시간·교육장소 |
| 상세 교육개요 | 글과 이미지. 대개 이미지 몇 장이 전부다 |
| 상세 학습목차 | 차시 묶음마다 이름과 수업 날짜 |
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

BASE_URL = "https://sesac.seoul.kr"
LIST_PATH = "/sesac/course/offline/courseList.do"
DETAIL_PATH = "/sesac/course/offline/courseDetail.do"
# 한 쪽에 12개가 나온다. 2026-09-22 에 192건, 16쪽이다. 끝없이 넘기지 않게 상한을 둔다
MAX_PAGES = 30
# 새싹 모집 상태 중 아직 신청할 수 있는 것. 나머지(운영중·과정종료·운영대기)는 모집 마감이다
OPEN_STATUSES = frozenset({"모집중", "모집예정"})

_DATE = re.compile(r"(\d{4})[.-](\d{1,2})[.-](\d{1,2})")
_HOURS = re.compile(r"(\d+)\s*시간")
_SPACES = re.compile(r"\s+")


class SesacParseError(ValueError):
    """새싹 페이지의 틀이 바뀌어 읽을 수 없다. 고정 파서를 고쳐야 한다."""


@dataclass(frozen=True)
class ListItem:
    crs_sn: str
    title: str
    thumbnail_url: str
    status: str = ""


@dataclass(frozen=True)
class CurriculumGroup:
    """학습목차의 차시 묶음 하나. 날짜는 그 묶음 수업들의 첫날과 마지막 날이다."""

    name: str
    first_day: date | None
    last_day: date | None
    lessons: tuple[str, ...]


@dataclass(frozen=True)
class Course:
    crs_sn: str
    source_url: str
    title: str
    status: str
    campus: str
    category: str
    recruitment_start: date | None
    recruitment_end: date | None
    program_start: date | None
    program_end: date | None
    hours: int | None
    place: str
    og_image_url: str
    overview_text: str
    overview_images: tuple[str, ...]
    curriculum: tuple[CurriculumGroup, ...] = field(default_factory=tuple)


def list_url(page: int = 1) -> str:
    return f"{BASE_URL}{LIST_PATH}?cPage={page}"


def spring_status(label: str) -> str:
    """새싹 모집 상태를 오공고 부트캠프 모집 상태로. 신청할 수 없으면 모집 마감이다."""
    return "RECRUITING" if label.strip() in OPEN_STATUSES else "CLOSED"


def detail_url(crs_sn: str) -> str:
    """원문 주소. 목록 링크에 붙은 검색·쪽 파라미터를 걷어 과정 하나에 주소 하나가 되게 한다."""
    return f"{BASE_URL}{DETAIL_PATH}?crsSn={crs_sn}"


def parse_list(html: str) -> list[ListItem]:
    """목록 한 쪽의 과정들. 틀이 바뀌어 카드가 하나도 없으면 빈 목록이다 — 부르는 쪽이 판단한다."""
    soup = BeautifulSoup(html, "html.parser")
    items: list[ListItem] = []
    for link in soup.select(".list-wrap a[href*='courseDetail.do']"):
        crs_sn = _crs_sn(str(link.get("href", "")))
        if not crs_sn:
            continue
        title = _text(link.select_one(".tit"))
        image = link.select_one(".thumb-area img")
        thumbnail = urljoin(BASE_URL, str(image.get("src", ""))) if image else ""
        status = _text(link.select_one(".tag-list li"))
        items.append(ListItem(crs_sn=crs_sn, title=title, thumbnail_url=thumbnail, status=status))
    return list({item.crs_sn: item for item in items}.values())


def total_count(html: str) -> int | None:
    """목록 머리의 `총 N건`. 목록이 비었을 때 정말 0건인지 틀이 바뀐 것인지 가른다."""
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one(".list-top .cnt strong")
    if node is None:
        return None
    digits = re.sub(r"\D", "", node.get_text())
    return int(digits) if digits else None


def parse_detail(html: str, crs_sn: str) -> Course:
    soup = BeautifulSoup(html, "html.parser")
    title = _title(soup)
    if not title:
        raise SesacParseError(f"과정명을 찾지 못했다 crsSn={crs_sn}")

    tags = [_text(node) for node in soup.select(".assist-area .tag-list li")]
    # 첫 태그가 모집 상태다. 상태마다 클래스가 다르다(모집중 clr01, 운영중 clr04, 과정종료 clr05)
    status = tags[0] if tags else ""
    campus = next((_text(node) for node in soup.select(".assist-area .tag-list li.clr02")), "")
    category = next((_text(node) for node in soup.select(".assist-area .tag-list li.clr03")), "")
    if not tags:
        raise SesacParseError(f"모집 상태·캠퍼스 태그를 찾지 못했다 crsSn={crs_sn}")

    info = _info_table(soup)
    recruitment = _dates(info.get("모집기간", ""))
    program = _dates(info.get("교육기간", ""))
    if len(program) < 2:
        raise SesacParseError(f"교육기간을 읽지 못했다 crsSn={crs_sn}: {info.get('교육기간')!r}")
    hours_match = _HOURS.search(info.get("교육시간", ""))

    overview = soup.select_one(".box-area.summary .edu-cn")
    overview_text = _block_text(overview) if overview else ""
    overview_images = tuple(
        dict.fromkeys(
            urljoin(BASE_URL, str(image.get("src", "")))
            for image in (overview.select("img[src]") if overview else [])
            if str(image.get("src", "")).strip()
        )
    )

    og_image = soup.select_one("meta[property='og:image']")
    og_image_url = urljoin(BASE_URL, str(og_image.get("content", ""))) if og_image else ""

    return Course(
        crs_sn=crs_sn,
        source_url=detail_url(crs_sn),
        title=title,
        status=status,
        campus=campus,
        category=category,
        recruitment_start=recruitment[0] if recruitment else None,
        recruitment_end=recruitment[1] if len(recruitment) > 1 else None,
        program_start=program[0],
        program_end=program[1],
        hours=int(hours_match.group(1)) if hours_match else None,
        place=info.get("교육장소", ""),
        og_image_url=og_image_url,
        overview_text=overview_text,
        overview_images=overview_images,
        curriculum=_curriculum(soup),
    )


def _title(soup: BeautifulSoup) -> str:
    """과정명. 머리 태그 바로 아래 제목이 먼저고, 없으면 og:description 이다.

    새싹은 og:description 에도 과정명을 둔다.
    """
    found = _text(soup.select_one(".assist-area ~ p.title"))
    if found:
        return found
    meta = soup.select_one("meta[property='og:description']")
    return _SPACES.sub(" ", str(meta.get("content", ""))).strip() if meta else ""


def _info_table(soup: BeautifulSoup) -> dict[str, str]:
    """강의 정보·수료기준 표의 `이름: 값`. 같은 이름이 두 번 나오면 앞의 것이다."""
    found: dict[str, str] = {}
    for row in soup.select("table tr"):
        head = row.find("th")
        cell = row.find("td")
        if head is None or cell is None:
            continue
        found.setdefault(_text(head), _text(cell))
    return found


def _dates(text: str) -> list[date]:
    out: list[date] = []
    for year, month, day in _DATE.findall(text):
        try:
            out.append(date(int(year), int(month), int(day)))
        except ValueError:
            continue
    return out


def _curriculum(soup: BeautifulSoup) -> tuple[CurriculumGroup, ...]:
    groups: list[CurriculumGroup] = []
    for node in soup.select(".box-area.index li.acc-list"):
        name = _text(node.select_one(".acc-tit .tit"))
        lessons: list[str] = []
        days: list[date] = []
        for lesson in node.select(".acc-cont li"):
            lesson_title = _text(lesson.select_one(".tit-area .tit"))
            if lesson_title:
                lessons.append(lesson_title)
            for paragraph in lesson.select(".data-area p"):
                days.extend(_dates(_text(paragraph)))
        if not name:
            continue
        groups.append(
            CurriculumGroup(
                name=name,
                first_day=min(days) if days else None,
                last_day=max(days) if days else None,
                lessons=tuple(lessons),
            )
        )
    return tuple(groups)


def _crs_sn(href: str) -> str:
    values = parse_qs(urlsplit(href.replace("&amp;", "&")).query).get("crsSn", [])
    return values[0].strip() if values and values[0].strip().isdigit() else ""


def _text(node: Tag | None) -> str:
    if node is None:
        return ""
    return _SPACES.sub(" ", node.get_text(" ")).strip()


def _block_text(node: Tag) -> str:
    """교육개요 글. 문단마다 한 줄이고 빈 줄은 버린다."""
    lines = (_SPACES.sub(" ", line).strip() for line in node.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)
