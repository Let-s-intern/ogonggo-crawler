"""목록 링크와 예시 공고 주소의 모양 견주기 (`app/selector/link_shape.py`). 모양은 실측 사이트다."""

from __future__ import annotations

import pytest

from app.selector.link_shape import looks_like_posting, matches_sample


@pytest.mark.parametrize(
    ("links", "sample"),
    [
        (["https://a.career.greetinghr.com/ko/o/195450"], "https://a.career.greetinghr.com/ko/o/1"),
        (
            ["https://x.ninehire.site/job_posting/GBDlUZTI"],
            "https://x.ninehire.site/job_posting/oz6xlsi9",
        ),
        (
            ["https://m.seoulmilk.co.kr/recruit_view.sm?articleId=2&search="],
            "https://m.seoulmilk.co.kr/recruit_view.sm?articleId=1&search=&keyword=",
        ),
        (
            ["https://www.neowiz.com/kr/career/browse-job/c977653e-1360-4c18-a48b-452af618f650"],
            "https://neowiz.com/kr/career/browse-job/f98e7602-7c88-4c01-b492-879e565685c7",
        ),
        # 견줄 것이 없으면 같은 것으로 본다
        ([], "https://a.test/jobs/1"),
        (["https://a.test/jobs/1"], ""),
    ],
)
def test_공고마다_다른_번호와_값은_보지_않는다(links: list[str], sample: str) -> None:
    assert matches_sample(links, sample) is True


@pytest.mark.parametrize(
    ("links", "sample"),
    [
        # 휴온스: 새 채용 화면 링크와 옛 화면 예시
        (
            ["https://huonsgroup.recruiter.co.kr/career/jobs/134345"],
            "https://huonsgroup.recruiter.co.kr/app/jobnotice/view?systemKindCode=MRS2&jobnoticeSn=1",
        ),
        # 데상트: 상품 소개 카드
        (
            ["https://d.career.greetinghr.com/ko/products"],
            "https://d.career.greetinghr.com/ko/o/189424",
        ),
        # 네오위즈: 투자자 공시
        (
            ["https://www.neowiz.com/kr/investor/announcement/259"],
            "https://www.neowiz.com/kr/career/browse-job/c977653e-1360-4c18-a48b-452af618f650",
        ),
    ],
)
def test_모양이_다른_링크를_가른다(links: list[str], sample: str) -> None:
    assert matches_sample(links, sample) is False


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://h.recruiter.co.kr/career/jobs/134345", True),
        ("https://x.ninehire.site/job_posting/GBDlUZTI", True),
        ("https://s.test/recruit_list_sub.php?division=ALWAYS&sub_idx=2340", True),
        ("https://d.career.greetinghr.com/ko/products", False),
        # 낱말 뒤에 순번만 붙은 페이지 이름이다 (데상트)
        ("https://d.career.greetinghr.com/ko/products1", False),
        ("https://a.career.greetinghr.com/ko/interview10", False),
        ("https://team.test/culture", False),
    ],
)
def test_공고_한_건_주소인지_본다(url: str, expected: bool) -> None:
    assert looks_like_posting(url) is expected
