"""근무 지역을 오공고 enum 시·도·시·군·구에서 고르는 것 테스트 (`app/regions.py`, LC-3385).

모델은 부르지 않는다. 목록·근거 검사·스키마·프롬프트·검수 편집·전송·마이그레이션이 같은 목록을
쓰는지 본다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from typing import get_args

from fastapi.testclient import TestClient

from app import db, regions
from app.classify.classifier import build_prompt
from app.classify.grounding import NOT_IN_LIST, OUTSIDE_REGION, ground
from app.classify.prompt_rules import DEFAULT_RULES, current
from app.classify.schema import (
    Classification,
    posting_model_of,
    validate_classification,
)
from app.deliver import spring
from tests.classify_fakes import response_body
from tests.test_deliver_spring import job_row
from tests.test_ui_review_actions import client, conn, overrides  # noqa: F401


def test_오공고_enum_의_시도_열여덟과_시군구_이백이십구에서_고른다() -> None:
    """광주와 전남은 `JEONNAM_GWANGJU` 하나다. 오공고 `Region`·`SubRegion` 과 같은 이름이다."""
    assert len(regions.names()) == 18
    assert regions.names()[:3] == ("NATIONWIDE", "SEOUL", "GYEONGGI")
    assert "JEONNAM_GWANGJU" in regions.names()
    assert len(regions.sub_names()) == 229
    assert regions.labels()["JEONNAM_GWANGJU"] == "전남광주"
    # 같은 이름의 구가 여러 시·도에 있어 화면 이름에 시·도를 붙인다
    assert regions.sub_labels()["BUSAN_JUNG_GU"] == "부산 중구"
    assert regions.subs_of()["SEJONG"] == ()


def test_시군구는_그_시도_안에_있어야_한다() -> None:
    assert regions.fits("SEOUL", "SEOUL_GANGNAM_GU")
    assert regions.fits("SEJONG", "")
    assert not regions.fits("BUSAN", "SEOUL_JUNG_GU")
    assert not regions.fits("", "SEOUL_JUNG_GU")


def test_근거_검사는_맞는_시도와_시군구를_남긴다() -> None:
    grounded = ground({"region": "SEOUL", "sub_region": "SEOUL_GANGNAM_GU"}, "본문", "제목")

    assert grounded.fields["region"] == "SEOUL"
    assert grounded.fields["sub_region"] == "SEOUL_GANGNAM_GU"
    assert grounded.dropped == []


def test_다른_시도의_시군구는_시군구만_버린다() -> None:
    """오공고가 맞지 않는 둘을 400 으로 거절한다. 시·도는 맞을 수 있어 남긴다."""
    grounded = ground({"region": "BUSAN", "sub_region": "SEOUL_JUNG_GU"}, "본문", "제목")

    assert grounded.fields["region"] == "BUSAN"
    assert grounded.fields["sub_region"] == ""
    assert grounded.dropped == ["sub_region"]
    assert grounded.reasons["sub_region"] == OUTSIDE_REGION


def test_목록_밖_시도는_시군구와_함께_버린다() -> None:
    """옛 한글 값(`서울`)이나 지어낸 이름이다. 시·도가 없으면 시·군·구도 둘 곳이 없다."""
    grounded = ground({"region": "서울", "sub_region": "SEOUL_JUNG_GU"}, "본문", "제목")

    assert (grounded.fields["region"], grounded.fields["sub_region"]) == ("", "")
    assert grounded.dropped == ["region", "sub_region"]
    assert grounded.reasons["region"] == NOT_IN_LIST


def test_근무지가_없는_공고는_두_칸_모두_빈_칸이다() -> None:
    grounded = ground({"region": "", "sub_region": ""}, "본문", "제목")

    assert (grounded.fields["region"], grounded.fields["sub_region"]) == ("", "")
    assert grounded.dropped == []


def test_응답의_근무지가_목록이면_첫_값을_쓴다() -> None:
    """예전 규칙판은 여러 곳을 고르게 했다. 오공고 칸은 하나다."""
    listed = validate_classification(response_body(region=["ULSAN", "GYEONGGI"]))
    single = validate_classification(response_body(region="SEOUL", sub_region="SEOUL_GANGNAM_GU"))

    assert listed.postings[0].fields["region"] == "ULSAN"
    assert single.postings[0].fields["sub_region"] == "SEOUL_GANGNAM_GU"


def test_응답_모델은_두_칸을_enum_으로_건다(conn: sqlite3.Connection) -> None:  # noqa: F811
    from app.classify.schema import build_classification_model

    posting = posting_model_of(build_classification_model(conn))

    assert get_args(posting.model_fields["region"].annotation) == ("", *regions.names())
    assert get_args(posting.model_fields["sub_region"].annotation) == ("", *regions.sub_names())
    assert issubclass(build_classification_model(conn), Classification)


def test_프롬프트는_시군구를_시도마다_묶어_적는다() -> None:
    prompt, _ = build_prompt("근무지: 성남시 분당구", "백엔드 개발자")

    judge = prompt.split("# 판정하는 칸")[1].split("# 공고 제목")[0]
    assert "- region:" in judge and "- sub_region:" in judge
    assert "NATIONWIDE(전국) / SEOUL(서울) / GYEONGGI(경기)" in judge
    assert "  - SEOUL: SEOUL_JONGNO_GU(종로구) / SEOUL_JUNG_GU(중구)" in judge
    assert "  - SEJONG: 없음" in judge
    assert "`경기도 성남시 분당구 판교역로` → `GYEONGGI_SEONGNAM_SI`" in judge
    # 뽑는 칸 구역에는 없다
    extract = prompt.split("# 뽑는 칸")[1].split("# 판정하는 칸")[0]
    assert "- region:" not in extract


def test_검수에서_다른_시도의_시군구로_고치면_막는다(
    client: TestClient,  # noqa: F811
    conn: sqlite3.Connection,  # noqa: F811
) -> None:
    html = client.post(
        "/ui/review/jobs/1/edit", data={"region": "BUSAN", "sub_region": "SEOUL_JUNG_GU"}
    ).text

    assert "고른 시·도 안에 없다" in html
    assert overrides(conn) == {}


def test_검수에서_시도와_시군구를_함께_고친다(
    client: TestClient,  # noqa: F811
    conn: sqlite3.Connection,  # noqa: F811
) -> None:
    client.post(
        "/ui/review/jobs/1/edit", data={"region": "SEOUL", "sub_region": "SEOUL_GANGNAM_GU"}
    )

    assert overrides(conn) == {"region": "SEOUL", "sub_region": "SEOUL_GANGNAM_GU"}
    row = conn.execute("SELECT region, sub_region FROM normalized_jobs WHERE id = 1").fetchone()
    assert tuple(row) == ("SEOUL", "SEOUL_GANGNAM_GU")


def test_검수에서_목록_밖_시도는_받지_않는다(
    client: TestClient,  # noqa: F811
    conn: sqlite3.Connection,  # noqa: F811
) -> None:
    html = client.post("/ui/review/jobs/1/edit", data={"region": "판교"}).text

    assert "목록 밖 값" in html
    assert overrides(conn) == {}


def test_오공고에_시도와_시군구를_enum_이름으로_보낸다() -> None:
    body = spring.payload(job_row(region="GYEONGGI", sub_region="GYEONGGI_SEONGNAM_SI"))

    assert (body["region"], body["subRegion"]) == ("GYEONGGI", "GYEONGGI_SEONGNAM_SI")


def test_목록_밖이거나_시도와_맞지_않는_값은_null_로_보낸다() -> None:
    """근무 지역은 선택 칸이다. 틀린 값을 보내 400 을 받느니 비워 등록한다."""
    old = spring.payload(job_row(region="울산광역시 동구", sub_region=None))
    crossed = spring.payload(job_row(region="BUSAN", sub_region="SEOUL_JUNG_GU"))

    assert (old["region"], old["subRegion"]) == (None, None)
    assert (crossed["region"], crossed["subRegion"]) == ("BUSAN", None)


def _at_0045(tmp_path: pathlib.Path) -> sqlite3.Connection:
    connection = db.connect(tmp_path / "region.db")
    db.migrate_up(connection)
    _down_to_0045(connection)
    connection.execute("INSERT INTO crawlers (id, name, list_url) VALUES (1, 'x', 'https://x')")
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, 'x')")
    for seq, region in enumerate(("경기, 울산", "광주", "울산광역시 동구", None), start=1):
        connection.execute(
            "INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
            " VALUES (?, 1, ?, '{}', ?)",
            (seq, f"https://x/{seq}", f"h{seq}"),
        )
        connection.execute(
            "INSERT INTO normalized_jobs (raw_job_id, source_url, title, body, region)"
            " VALUES (?, ?, '공고', '본문', ?)",
            (seq, f"https://x/{seq}", region),
        )
        connection.execute(
            "INSERT INTO job_classifications (raw_job_id, model, region) VALUES (?, 'm', ?)",
            (seq, region),
        )
    connection.execute(
        "INSERT INTO job_field_overrides (raw_job_id, field_name, value)"
        " VALUES (1, 'region', '전남'), (2, 'region', '분당')"
    )
    return connection


def _down_to_0045(connection: sqlite3.Connection) -> None:
    """0046 과 그 뒤에 더한 마이그레이션을 되돌린다."""
    later = [version for version in db.applied_versions(connection) if version >= "0046"]
    db.migrate_down(connection, steps=len(later))


def test_쌓인_근무_지역은_첫_지역의_enum_이름이_된다(tmp_path: pathlib.Path) -> None:
    """`경기, 울산` 은 `GYEONGGI`, 광주는 `JEONNAM_GWANGJU`. 옛 원문 글자는 옮길 곳이 없다."""
    connection = _at_0045(tmp_path)

    db.migrate_up(connection)

    expected = ["GYEONGGI", "JEONNAM_GWANGJU", None, None]
    for table in ("normalized_jobs", "job_classifications"):
        rows = connection.execute(f"SELECT region, sub_region FROM {table} ORDER BY raw_job_id")
        assert [tuple(row) for row in rows] == [(value, None) for value in expected], table
    # 옮길 곳이 없는 사람 보정은 지운다
    assert overrides(connection) == {"region": "JEONNAM_GWANGJU"}


def test_되돌리면_한글_이름으로_돌아가고_시군구는_지워진다(tmp_path: pathlib.Path) -> None:
    connection = _at_0045(tmp_path)
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO job_field_overrides (raw_job_id, field_name, value)"
        " VALUES (3, 'sub_region', 'SEOUL_JUNG_GU')"
    )

    _down_to_0045(connection)

    rows = connection.execute("SELECT region FROM normalized_jobs ORDER BY raw_job_id")
    assert [row["region"] for row in rows] == ["경기", "광주", None, None]
    assert overrides(connection) == {"region": "광주"}


def test_저장한_규칙판이_있으면_근무지_규칙을_새_기본으로_바꾼_판을_쌓는다(
    tmp_path: pathlib.Path,
) -> None:
    """옛 판의 `여러 곳이면 모두 고른다` 가 enum 목록과 어긋난다. 다른 칸의 고친 규칙은 둔다."""
    connection = _at_0045(tmp_path)
    saved = json.loads(_old_rules_json(connection))
    assert saved["fields"]["region"]["examples"][1]["value"] == "울산, 경기"

    db.migrate_up(connection)

    rules = current(connection).rules
    assert rules.fields["region"] == DEFAULT_RULES.fields["region"]
    assert rules.fields["sub_region"] == DEFAULT_RULES.fields["sub_region"]
    assert rules.common == "공통 규칙을 고쳤다"


def test_저장한_규칙판이_없으면_판을_쌓지_않는다(tmp_path: pathlib.Path) -> None:
    connection = _at_0045(tmp_path)

    db.migrate_up(connection)

    assert connection.execute("SELECT count(*) FROM classify_rule_versions").fetchone()[0] == 0
    assert current(connection).number == 0


def _old_rules_json(connection: sqlite3.Connection) -> str:
    """0045 때의 규칙판 하나를 저장한다. 근무지는 여러 곳을 고르던 옛 규칙이다."""
    old = {
        "rule": "근무지. 여러 곳이면 모두 고른다",
        "examples": [
            {"source": "근무지: 성남시 분당구(판교)", "value": "경기"},
            {"source": "울산 본사 및 분당 GRC", "value": "울산, 경기"},
        ],
    }
    text = json.dumps(
        {"common": "공통 규칙을 고쳤다", "fields": {"region": old}}, ensure_ascii=False
    )
    connection.execute("INSERT INTO classify_rule_versions (rules_json) VALUES (?)", (text,))
    return text
