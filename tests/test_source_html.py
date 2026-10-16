"""오공고 공고 상세에 그릴 원문 HTML 정리 (LC-3456).

| 확인 | 깨지면 |
|---|---|
| 스크립트·스타일·폼은 빼고 꾸밈 속성을 지운다 | 사이트 스크립트나 깨진 꾸밈이 섞인다 |
| 남기지 않는 태그는 벗기고 블록은 div 로 바꾼다 | 글이 사라지거나 문단이 한 줄로 붙는다 |
| 상대 주소를 원문 주소 기준으로 풀고 지연 로딩 이미지를 살린다 | 이미지 공고가 빈 화면이 된다 |
| 공백을 줄이고 빈 블록을 지운다 | 원문에 빈 줄이 수십 줄 낀다 |
"""

from __future__ import annotations

from app.deliver.source_html import clean_html, text_html

PAGE = "https://careers.example.com/jobs/7#2"


def test_스크립트와_꾸밈은_빼고_제목_목록_표는_남긴다() -> None:
    raw = """
    <section class="job" style="padding:10px" onclick="x()">
      <h3 style="color:#333">이런 업무를 해요</h3>
      <ul><li><span style="color:red">광고 운영</span></li><li>성과 분석</li></ul>
      <table border="1"><tr><th colspan="2" style="x">근무지</th></tr><tr><td>서울</td></tr></table>
      <script>track()</script><style>.x{}</style><button>지원하기</button>
      <!-- 주석 -->
    </section>
    """

    assert clean_html(raw, PAGE) == (
        "<div><h3>이런 업무를 해요</h3><ul><li>광고 운영</li><li>성과 분석</li></ul>"
        '<table><tr><th colspan="2">근무지</th></tr><tr><td>서울</td></tr></table></div>'
    )


def test_상대_주소를_풀고_지연_로딩_이미지를_살린다() -> None:
    raw = (
        '<div><img src="data:image/gif;base64,AAAA" data-src="/img/job.png" width="900">'
        '<img src="javascript:alert(1)"><a href="apply?id=7" target="x">지원</a>'
        '<a href="javascript:x()">나쁜 링크</a><a href="mailto:hr@example.com">메일</a></div>'
    )

    assert clean_html(raw, PAGE) == (
        '<div><img src="https://careers.example.com/img/job.png">'
        '<a href="https://careers.example.com/jobs/apply?id=7">지원</a>나쁜 링크'
        '<a href="mailto:hr@example.com">메일</a></div>'
    )


def test_빈_블록은_지우고_글도_이미지도_없으면_null() -> None:
    assert clean_html("<div><p> </p><div><span>\n</span></div><p>글</p></div>", PAGE) == (
        "<div><p>글</p></div>"
    )
    assert clean_html("<div><p>&nbsp;</p><script>x()</script></div>", PAGE) is None
    assert clean_html("", PAGE) is None
    assert clean_html(None, PAGE) is None


def test_페이지_부속은_뺀다() -> None:
    raw = '<div><p>공고</p><div class="aside_board"><a href="/9">다른 공고</a></div></div>'

    assert clean_html(raw, PAGE) == "<div><p>공고</p></div>"


def test_줄바꿈을_그리는_스타일_안의_줄바꿈은_br_로_남긴다() -> None:
    """스마일게이트: `white-space: pre-line` 섹션이 `- 항목 - 항목` 한 줄로 붙었다."""
    raw = (
        '<div style="white-space: pre-line;">\n- 첫 항목\n- 둘째 항목\n\n\n- 셋째 항목\n</div>'
        "<p>보통\n문단</p>"
    )

    assert clean_html(raw, PAGE) == (
        "<div>- 첫 항목<br>- 둘째 항목<br><br>- 셋째 항목</div><p>보통 문단</p>"
    )


def test_HTML이_없으면_본문_글자를_문단으로_감싼다() -> None:
    assert text_html("  첫 줄 \n\t둘째  줄\n\n\n\n<셋째> & 끝\n") == (
        "<p>첫 줄<br>둘째 줄</p><p>&lt;셋째&gt; &amp; 끝</p>"
    )
    assert text_html(" \n ") is None
    assert text_html(None) is None


def test_같은_본문이_두_벌_담겨_있으면_한_벌만_보낸다() -> None:
    """2026-10-14~16 에 수집한 그리팅 공고는 본문 HTML 에 PC·모바일 두 벌이 담겨 있다."""
    body = "<p>" + "리터니티는 글로벌 퍼포먼스 마케터를 찾고 있어요. " * 5 + "</p>"
    raw = (
        f'<div><div class="m"><div class="ql-editor">{body}</div></div>'
        f'<div class="pc"><div><div class="ql-editor">{body}</div></div></div></div>'
    )

    cleaned = clean_html(raw, PAGE)

    assert cleaned is not None
    assert cleaned.count("글로벌 퍼포먼스 마케터를") == 5


def test_이미지만_있는_본문도_두_벌이면_한_벌만_보낸다() -> None:
    raw = (
        '<div><div><p><img src="https://x/job.jpg"></p></div>'
        '<div><div><p><img src="https://x/job.jpg"></p></div></div></div>'
    )

    assert clean_html(raw, PAGE) == '<div><div><p><img src="https://x/job.jpg"></p></div></div>'
