"""값 여러 개로 상세 주소를 만드는 사이트. 현대가 그렇다. 픽스처로만 돈다.

현대 상세는 `recuYy`·`recuType`·`recuCls` 세 값이 다 있어야 열린다. 한 값으로는 공고를
지목할 수 없어서, id 자체가 세 값을 이어 붙인 것이 된다
(`.claude/tasks/done/fill-body/tasks-fill-body-push4.md`).
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app.crawler.api_source import build_items
from app.crawler.parser import FieldParseError
from app.selector.api_schema import validate_api_config

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LIST_PAYLOAD = json.loads((FIXTURES / "hyundai-list-20260825.json").read_text(encoding="utf-8"))

CONFIG = validate_api_config(
    {
        "list": {
            "url": "https://talent.hyundai.com/api/rec/AP-HM-FO-02730?hgrCd=1&lang=ko",
            "method": "GET",
            "items_path": "data.applyList",
            "fields": {"title": "recuNoticeNm", "date": "appDispEdDt", "company_name": "logoNm"},
            "id_field": "recuYy={recuYy}&recuType={recuType}&recuCls={recuCls}",
            "link_template": "https://talent.hyundai.com/apply/applyView.hc?{id}",
        }
    }
).list_config()


def test_every_posting_gets_the_three_parameters_in_its_address() -> None:
    """20건 모두 세 값이 들어간 주소를 갖는다."""
    result = build_items(LIST_PAYLOAD, CONFIG)

    assert len(result.items) == 20
    for item in result.items:
        assert item.link.startswith("https://talent.hyundai.com/apply/applyView.hc?recuYy=")
        assert "recuType=" in item.link
        assert "recuCls=" in item.link


def test_the_first_posting_matches_the_measured_address() -> None:
    """2026-08-25 측정에서 열린 주소와 같은 모양이어야 한다."""
    result = build_items(LIST_PAYLOAD, CONFIG)

    first = result.items[0]
    assert first.detail_key == "recuYy=2026&recuType=N2&recuCls=295"
    assert (
        first.link
        == "https://talent.hyundai.com/apply/applyView.hc?recuYy=2026&recuType=N2&recuCls=295"
    )


def test_each_posting_keeps_its_own_address() -> None:
    """공고마다 달라야 중복 판정이 선다."""
    result = build_items(LIST_PAYLOAD, CONFIG)

    assert len({item.link for item in result.items}) == 20


def test_a_missing_parameter_drops_the_item_instead_of_half_filling_it() -> None:
    """한 자리가 비면 그 항목은 남기지 않는다. 반쯤 채운 주소로 요청하지 않는다."""
    payload = {
        "data": {"applyList": [{"recuNoticeNm": "제목", "recuYy": "2026", "recuType": "N2"}]}
    }

    with pytest.raises(FieldParseError) as caught:
        build_items(payload, CONFIG)

    assert "id" in str(caught.value)


def test_a_plain_key_still_works() -> None:
    """`{}` 가 없으면 예전처럼 키 하나를 읽는다."""
    config = validate_api_config(
        {
            "list": {
                "url": "https://talent.hyundai.com/api/rec/AP-HM-FO-02730",
                "method": "GET",
                "items_path": "data.applyList",
                "fields": {"title": "recuNoticeNm"},
                "id_field": "recuCls",
                "link_template": "https://talent.hyundai.com/apply/applyView.hc?recuCls={id}",
            }
        }
    ).list_config()

    result = build_items(LIST_PAYLOAD, config)

    assert result.items[0].detail_key == "295"
