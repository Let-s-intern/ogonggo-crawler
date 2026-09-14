"""알림 내용 테스트 (1.2.V).

1건·5건·104건일 때 본문이 각각 어떻게 줄어드는지를 본다. 104건은 SK 한 번의 실제 건수다
(`.claude/tasks/done/ntfy-notify/tasks-ntfy-notify.md`).
"""

from __future__ import annotations

import unicodedata

from app.notify.message import NEW_JOBS_TAGS, PREVIEW_LIMIT, NewJob, build_new_jobs_message


def jobs(count: int) -> list[NewJob]:
    return [
        NewJob(company_name=f"회사{index}", title=f"공고 {index}") for index in range(1, count + 1)
    ]


def test_1건이면_그_한_건만_적고_외_N건은_없다() -> None:
    message = build_new_jobs_message(site_name="SK", jobs=jobs(1))

    assert message.title == "SK 새 공고 1건"
    assert message.body == "- **회사1** 공고 1"
    assert "외" not in message.body


def test_5건이면_다섯_건을_다_적는다() -> None:
    message = build_new_jobs_message(site_name="SK", jobs=jobs(5))

    assert message.title == "SK 새 공고 5건"
    assert message.body.count("\n- ") + 1 == 5
    assert "외" not in message.body


def test_104건이면_다섯_건만_적고_나머지는_외_99건이_된다() -> None:
    message = build_new_jobs_message(site_name="SK", jobs=jobs(104))

    assert message.title == "SK 새 공고 104건"
    lines = message.body.split("\n")
    named = [line for line in lines if line.startswith("- ")]
    assert len(named) == PREVIEW_LIMIT
    assert named[0] == "- **회사1** 공고 1"
    assert lines[-1] == "외 99건"
    # 마크다운이 목록 안으로 빨아들이지 않게 빈 줄로 떨어뜨린다
    assert lines[-2] == ""
    # 104건을 다 넣지 않는다
    assert "공고 104" not in message.body


def test_6건은_다섯_건과_외_1건이다() -> None:
    """상한 바로 위. `외 0건` 이나 여섯 번째 이름이 나오면 경계가 틀린 것이다."""
    message = build_new_jobs_message(site_name="SK", jobs=jobs(6))

    assert message.body.endswith("외 1건")
    assert "공고 6" not in message.body


def test_회사가_비면_제목만_적는다() -> None:
    message = build_new_jobs_message(
        site_name="SK", jobs=[NewJob(company_name="  ", title="클라우드 엔지니어")]
    )

    assert message.body == "- 클라우드 엔지니어"


def test_제목이_비면_빈_줄_대신_사실을_적는다() -> None:
    """빈 칸은 반대 뜻으로 읽힌다 (`.claude/rules/writing.md`)."""
    message = build_new_jobs_message(site_name="SK", jobs=[NewJob(company_name="SK", title="")])

    assert message.body == "- **SK** 제목 없음"


def test_긴_제목은_잘리고_잘렸다는_표시가_남는다() -> None:
    message = build_new_jobs_message(
        site_name="SK", jobs=[NewJob(company_name="SK", title="가" * 200)]
    )

    assert message.body.endswith("...")
    assert len(message.body) < 100


def test_누르면_열_주소가_그대로_실린다() -> None:
    message = build_new_jobs_message(
        site_name="SK", jobs=jobs(3), click="https://ops.example.com/review"
    )

    assert message.click == "https://ops.example.com/review"
    assert message.headers()["X-Click"] == "https://ops.example.com/review"


def test_주소가_없으면_누를_곳_없는_알림이_된다() -> None:
    message = build_new_jobs_message(site_name="SK", jobs=jobs(3))

    assert "X-Click" not in message.headers()


def test_태그는_붙고_본문_문장에는_그림문자가_없다() -> None:
    """태그는 예외지만 제목과 본문은 글자로만 쓴다."""
    message = build_new_jobs_message(site_name="SK", jobs=jobs(104))

    assert message.tags == NEW_JOBS_TAGS
    assert message.headers()["X-Tags"] == "briefcase"
    assert _pictograms(message.title) == []
    assert _pictograms(message.body) == []


def _pictograms(text: str) -> list[str]:
    """그림문자로 읽히는 글자. 유니코드 분류가 `So`(기타 기호)인 것이 이모지·픽토그램이다."""
    return [character for character in text if unicodedata.category(character) == "So"]


def test_제목이_공고_원본_주소로_걸린다() -> None:
    """본문의 제목을 누르면 그 공고가 열린다."""
    message = build_new_jobs_message(
        site_name="LG",
        jobs=[NewJob(company_name="LG전자", title="백엔드 개발자", url="https://x.test/jobs/1")],
        click="https://x.test/list",
    )

    assert "[백엔드 개발자](https://x.test/jobs/1)" in message.body


def test_알림을_누르면_목록_페이지가_열린다() -> None:
    """한 알림에 여러 건이 들어오므로 공고 하나로 보내지 않는다. 목록에 다 있다."""
    message = build_new_jobs_message(
        site_name="LG",
        jobs=[
            NewJob(company_name="LG전자", title="가", url="https://x.test/jobs/1"),
            NewJob(company_name="LG화학", title="나", url="https://x.test/jobs/2"),
        ],
        click="https://x.test/list",
    )

    assert message.click == "https://x.test/list"


def test_주소가_없으면_링크_없이_글자만_적는다() -> None:
    """목록만 긁고 상세로 가지 못한 공고가 그렇다. 빈 링크를 만들지 않는다."""
    message = build_new_jobs_message(
        site_name="LG",
        jobs=[NewJob(company_name="LG전자", title="백엔드 개발자")],
        click="https://x.test/list",
    )

    assert "](" not in message.body
    assert "백엔드 개발자" in message.body


def test_제목의_대괄호가_링크를_깨뜨리지_않는다() -> None:
    """`[정보보안센터] IT보안 담당자` 처럼 대괄호로 시작하는 제목이 흔하다."""
    message = build_new_jobs_message(
        site_name="LG",
        jobs=[
            NewJob(company_name="LG", title="[정보보안센터] IT보안", url="https://x.test/jobs/3")
        ],
    )

    assert "[정보보안센터 IT보안](https://x.test/jobs/3)" in message.body
