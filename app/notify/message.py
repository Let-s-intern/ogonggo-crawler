"""알림 하나의 제목과 본문을 만든다.

**한 실행에 알림 하나다.** 공고마다 보내지 않는다 — SK 는 한 번에 104건이 들어오고, 그러면
알림이 104개 온다 (`.claude/tasks/done/ntfy-notify/tasks-ntfy-notify.md`).

같은 이유로 본문에 104건을 다 넣지 않는다. 앞의 몇 건만 보이고 나머지는 `외 N건` 으로 줄인다.
다 넣은 알림은 읽히지 않고, 읽히지 않는 알림은 안 보낸 것과 같다.

본문은 마크다운이다. 문장에는 그림문자를 넣지 않는다 (`.claude/rules/writing.md`).
상태를 한눈에 보이게 하는 것은 ntfy 태그 쪽이다 (`app/notify/ntfy.py`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.notify.ntfy import NtfyMessage

# 본문에 이름을 적는 공고 수. 나머지는 `외 N건` 이 된다.
# 휴대폰 알림 하나가 펼치지 않고 보여주는 줄 수를 넘지 않는 선이다
PREVIEW_LIMIT = 5

# 제목 한 줄의 상한. 긴 공고 제목 하나가 알림 전체를 밀어내지 않게 한다
TITLE_LIMIT = 60

# 새 공고 알림에 붙는 태그. ntfy 의 이모지 단축이름이다
NEW_JOBS_TAGS: tuple[str, ...] = ("briefcase",)

# 테스트 전송의 태그. 새 공고 알림과 달라야 휴대폰에서 둘이 구분된다
TEST_TAGS: tuple[str, ...] = ("white_check_mark",)

# 값이 비어 있을 때 그 자리에 적는 말. 빈 줄로 두면 공고가 없는 것으로 읽힌다
_UNTITLED = "제목 없음"


@dataclass(frozen=True)
class NewJob:
    """알림에 한 줄로 들어가는 공고."""

    company_name: str
    title: str
    # 공고 원본 주소. 알림에서 이 자리를 눌러 바로 열게 한다. 비어 있으면 링크 없이 글자만
    # 적는다 — 목록만 긁고 상세로 가지 못한 공고가 그렇다
    url: str = ""


def build_new_jobs_message(
    *,
    site_name: str,
    jobs: Sequence[NewJob],
    click: str = "",
) -> NtfyMessage:
    """새 공고 알림 하나. `jobs` 는 이번 실행이 적재한 공고 전부다.

    건수는 `jobs` 의 길이에서 나온다. 건수를 따로 받으면 목록과 숫자가 어긋난 알림이
    나갈 수 있고, 그때 어느 쪽이 맞는지 알 방법이 없다.

    `click` 은 알림을 그냥 눌렀을 때 열 곳이고 **그 사이트의 목록 페이지**다. 공고 하나로
    보내지 않는 것은 한 알림에 여러 건이 들어오기 때문이다 — 그중 하나만 열면 나머지를
    놓친다. 개별 공고는 본문의 제목 링크로 연다.

    비어 있으면 누를 곳 없는 알림이 된다. 알림 자체는 그대로 나간다.
    """
    total = len(jobs)
    return NtfyMessage(
        title=f"{site_name} 새 공고 {total}건",
        body=_body(jobs),
        tags=NEW_JOBS_TAGS,
        click=click,
    )


def build_test_message(*, click: str = "") -> NtfyMessage:
    """설정 확인용 알림 하나. 새 공고 알림과 헷갈리지 않아야 한다.

    새 공고 알림의 모양을 그대로 빌려 쓰지 않는다. `... 새 공고 2건` 이라는 제목이 뜨면
    운영자는 공고가 실제로 두 건 들어온 것으로 읽는다. 제목과 태그를 따로 두어 이것이
    확인용이라는 사실이 알림만 보고도 남게 한다.
    """
    return NtfyMessage(
        title="알림 설정 확인",
        body=(
            "이 알림이 보이면 설정이 맞다.\n\n"
            "실제 알림은 실행이 끝나고 새 공고가 들어왔을 때 오고, "
            "이 자리에 회사와 공고 제목이 들어간다."
        ),
        tags=TEST_TAGS,
        click=click,
    )


def _body(jobs: Sequence[NewJob]) -> str:
    """앞의 몇 건은 이름으로, 나머지는 숫자로.

    `외 N건` 은 목록과 빈 줄로 떨어뜨린다. 붙여 두면 마크다운이 마지막 항목의 이어지는
    글로 읽어 목록 안에 들어가 버린다.
    """
    if not jobs:
        # 부르는 쪽이 0건에는 보내지 않는다 (`app/notify/new_jobs.py`). 그래도 여기서
        # 빈 문자열을 내면 본문 없는 알림이 되므로 사실을 적는다
        return "새 공고가 없다"

    lines = [f"- {_line(job)}" for job in jobs[:PREVIEW_LIMIT]]
    remaining = len(jobs) - PREVIEW_LIMIT
    if remaining > 0:
        lines.append("")
        lines.append(f"외 {remaining}건")
    return "\n".join(lines)


def _line(job: NewJob) -> str:
    """공고 한 줄. 회사가 있으면 굵게 앞세우고, 제목은 원본 주소로 건다."""
    title = _clip(job.title.strip()) or _UNTITLED
    url = job.url.strip()
    if url:
        # 마크다운 링크. 제목에 든 대괄호가 링크를 깨뜨리므로 먼저 지운다
        title = f"[{title.replace('[', '').replace(']', '')}]({url})"
    company_name = job.company_name.strip()
    if not company_name:
        return title
    return f"**{_clip(company_name)}** {title}"


def _clip(text: str) -> str:
    """상한을 넘는 글자는 자른다. 잘렸다는 사실이 보이게 뒤에 표시를 남긴다."""
    if len(text) <= TITLE_LIMIT:
        return text
    return text[:TITLE_LIMIT] + "..."
