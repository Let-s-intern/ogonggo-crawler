"""JSON API 로 가져오는 크롤러의 설정 스키마와 검증.

`crawlers.api_config_json` 에 그대로 들어가는 모양을 여기서 정한다. 셀렉터 스키마
(`app/selector/schema.py`)와 같은 자리에 두는 이유는 같은 일을 하기 때문이다 — 저장하기 전에
"이 설정으로 실행할 수 있는가" 를 판정하고, 아니면 무엇이 없는지 이름을 대고 거절한다.

셀렉터가 HTML 의 어느 노드를 볼지 적는 것이라면, 이 설정은 어느 endpoint 에 무엇을 보내고
응답의 어느 경로를 읽을지 적는 것이다.

## 모양

```json
{
  "list": {
    "url": "https://api.example.test/jobs",
    "method": "POST",
    "body": {"page": 1},
    "items_path": "data.jobList",
    "fields": {"title": "jobName", "date": "endDate", "company_name": "companyName"},
    "id_field": "jobId",
    "link_template": "https://example.test/jobs/{id}"
  },
  "detail": {
    "url": "https://api.example.test/jobs/detail",
    "method": "POST",
    "body": {"jobId": "{id}"},
    "fields": {"title": "data.jobName", "body": "data.recList.0.context"}
  }
}
```

`items_path` 와 `fields` 의 값은 점 표기 경로다. 숫자 조각은 배열의 자리를 뜻한다 —
`recList.0.detailContext` 는 `recList` 의 첫 항목이다.

## 요청과 응답의 모양은 사이트가 정한다

| 키 | 값 | 왜 |
|---|---|---|
| `headers` | 사이트가 요구하는 기능성 헤더 | 현대는 `x-hkmc-service` 가 없으면 400 이다 |
| `body_format` | `json`(기본) 또는 `form` | 삼성·SK 목록은 폼이 아니면 답하지 않는다 |
| `response` | `json`(기본) 또는 `html` | 삼성 목록은 JSON 이 아니라 HTML 조각이다 |

`User-Agent` 는 `headers` 에 담을 수 없다. 이름을 정직하게 밝히는 것은 공용 fetch 클라이언트의
일이고, 설정이 덮을 수 있으면 브라우저 위장이 크롤러 등록만으로 가능해진다
(`.claude/rules/crawling.md`).

`date_is_deadline` 은 목록에서 읽은 `date` 가 그 공고의 마감일이라는 뜻이다. 참이면 마감이
지난 공고는 상세를 열지 않고 건너뛴다. 게시일을 적어 두는 사이트에 참을 주면 어제 올라온 새
공고가 조용히 버려지므로, 사이트마다 응답을 보고 적는다.

`fields` 의 값은 경로 하나이거나 경로의 배열이다. 배열이면 그 자리들을 모아 빈 줄로 잇는다.
경로 안의 `*` 는 배열 전체를 훑는다 — `recList.*.detailContext` 는 모집 부문마다 하나씩이다.
한 자리만 읽으면 나머지 부문의 본문이 수집 단계에서 사라진다.

`response` 가 `html` 이면 같은 키가 CSS 셀렉터로 읽힌다 — `items_path` 는 항목 셀렉터,
`fields` 는 항목 안의 셀렉터, `id_field` 는 `<셀렉터>@<속성>` 이다. `|digits` 를 붙이면
속성값에서 숫자만 남긴다 (삼성 공고 번호에 천 단위 쉼표가 있다).

`link_template` 은 사람이 볼 상세 주소를 만든다. `raw_jobs.source_url` 에 이 값이 들어가므로
공고마다 달라야 하고, 그래서 `{id}` 가 반드시 들어간다. 이것이 없으면 모든 공고가 같은 주소를
갖게 되고, 중복 판정과 소비 측 링크가 동시에 무너진다.

상세 설정도 `{id}` 가 `url` 이나 `body` 어딘가에 있어야 한다. 없으면 공고가 몇 건이든 같은
상세를 가져온다.

## 판정은 통과 아니면 실패다

셀렉터 스키마와 같은 이유로 추측해서 고치지 않는다 (`.claude/rules/llm.md`). 사유는 셋 중
하나이고 무엇이 문제인지 이름을 댄다.

| reason | 뜻 |
|---|---|
| `unparsable` | JSON 이 아니거나, 객체·문자열이 아닌 자리에 다른 타입이 왔다 |
| `unknown_field` | 스키마에 없는 필드명이 왔다 |
| `missing_field` | 필수 필드가 없거나 값이 비어 있다 |
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from app.selector.schema import DETAIL_FIELDS

# 목록 응답에서 읽는 값. 상세 링크는 `link_template` 이 만들므로 여기 없다
LIST_FIELDS: tuple[str, ...] = ("title", "date", "company_name")

# 목록 `fields` 에 적을 수 있는 이름 전부. 위의 셋에 상세 칸 이름이 더해진다.
#
# 목록 응답이 상세 칸의 값까지 들고 있는 사이트가 있다. 카카오 목록 API 는 직군·근무지·
# 모집인원·주요 업무·전형 절차를 항목마다 담아 주는데, 상세 문서에는 그것들이 한 덩어리로만
# 있어 셀렉터로 갈라낼 수 없다. 여기서 읽지 않으면 그 값들은 수집 단계에서 사라지고,
# 매핑하지 않은 값은 저장되지 않으므로 다시 얻을 길이 없다
# (`.claude/tasks/memos/보류/split-body/prd-split-body.md`).
#
# 상세에서 읽은 값이 있으면 그쪽이 이긴다. 목록에서 읽은 값은 상세가 비었을 때만 쓰인다
# (`app/crawler/runner.py` 의 `_record`).
LIST_ALLOWED_FIELDS: tuple[str, ...] = LIST_FIELDS + tuple(
    name for name in DETAIL_FIELDS if name not in LIST_FIELDS
)

# 보낼 수 있는 메서드. 목록·상세 API 는 둘 중 하나다. 그 밖의 메서드를 쓰는 곳이 없고,
# 조회에 PUT·DELETE 를 보내는 설정은 오타일 가능성이 훨씬 크다
METHODS: tuple[str, ...] = ("GET", "POST")

# 본문을 무엇으로 실어 보내는가. 삼성과 SK 는 폼이 아니면 답하지 않는다
JSON_BODY = "json"
FORM_BODY = "form"
BODY_FORMATS: tuple[str, ...] = (JSON_BODY, FORM_BODY)

# 응답을 무엇으로 읽는가. 삼성 목록은 JSON 이 아니라 HTML 조각이다
JSON_RESPONSE = "json"
HTML_RESPONSE = "html"
RESPONSE_FORMATS: tuple[str, ...] = (JSON_RESPONSE, HTML_RESPONSE)

# 쪽 넘김의 기본 상한과 절대 상한. 쪽마다 요청이 하나씩 나가므로 설정으로도 이 위로 못 올린다
DEFAULT_MAX_PAGES = 20
PAGE_LIMIT = 100

# HTML 응답에서 id 를 읽는 표기. `<셀렉터>@<속성>` 이고 셀렉터를 비우면 항목 노드 자신이다
ID_ATTRIBUTE_MARK = "@"
# 속성값에서 숫자만 남긴다. 삼성 공고 번호는 `22,878` 처럼 천 단위 쉼표가 찍혀 온다
DIGITS_FILTER = "|digits"

# 항목 id 가 들어갈 자리. 상세 요청과 상세 주소가 이것으로 공고마다 갈린다
ID_PLACEHOLDER = "{id}"

SECTIONS: tuple[str, ...] = ("list", "detail")

# 필드 하나가 읽을 자리. 배열이면 그 자리들을 모아 빈 줄로 잇는다
FieldPath = str | list[str]

# 설정으로 담을 수 없는 헤더. 이름을 정직하게 밝히는 것은 공용 fetch 클라이언트의 일이고,
# 크롤러 설정이 그것을 덮을 수 있으면 브라우저 위장이 설정 한 줄이 된다
BLOCKED_HEADERS: frozenset[str] = frozenset({"user-agent"})


class ApiConfigError(ValueError):
    """검증 실패. `reason` 은 위 표의 값 중 하나다."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class ApiPagination(BaseModel):
    """쪽을 넘기는 법. 없으면 한 번만 부른다.

    쪽 번호는 본문의 `param` 자리에 들어간다. 한화는 `page` 를 0 부터, 삼성은 `currentPageNo`
    를 1 부터 센다.

    마지막 쪽인지 아는 법이 사이트마다 다르다.

    | 키 | 사이트 | 뜻 |
    |---|---|---|
    | `has_next` | 한화 | 응답 안 그 경로가 참인 동안 계속한다 |
    | `total_pages_selector`+`total_pages_attribute` | 삼성 | 총 쪽 수를 읽어 그만큼만 돈다 |
    | 둘 다 없음 | - | 항목이 0건인 쪽이 나오면 멈춘다 |

    `max_pages` 는 어느 경우에도 넘지 않는 상한이다. 끝나지 않는 `has_next` 를 만나면 그
    사이트를 영원히 때리게 되므로, 판정이 무엇이든 상한이 마지막 안전장치다.
    """

    # 쪽 번호를 넣을 본문 키
    param: str
    # 첫 쪽 번호. 한화는 0, 삼성은 1 이다
    start: int = 1
    # 무한 반복을 막는 상한. 이 수만큼 부르고 무조건 멈춘다
    max_pages: int = DEFAULT_MAX_PAGES
    # 다음 쪽이 있는지를 담은 JSON 경로
    has_next: str = ""
    # 총 쪽 수가 든 노드와 그 속성. HTML 응답에서 쓴다
    total_pages_selector: str = ""
    total_pages_attribute: str = ""


class ApiListConfig(BaseModel):
    """목록 API 하나. 응답의 `items_path` 가 공고 배열이다."""

    url: str
    method: str
    body: dict[str, Any]
    items_path: str
    # 공고 필드 -> 항목 안의 JSON 경로
    fields: dict[str, FieldPath]
    # 항목 안에서 공고 id 를 담은 키. 상세 요청과 `link_template` 이 이 값을 쓴다
    id_field: str
    # 사람이 볼 상세 주소. `{id}` 자리에 항목 id 가 들어간다
    link_template: str
    # 사이트가 요구하는 기능성 헤더. 없으면 응답을 주지 않는 API 가 있다
    headers: dict[str, str] = {}
    # 본문을 JSON 으로 보낼지 폼으로 보낼지. 폼이 아니면 500 을 주는 사이트가 있다
    body_format: str = JSON_BODY
    # 응답을 JSON 으로 읽을지 HTML 로 읽을지. `html` 이면 `items_path` 와 `fields` 는 CSS
    # 셀렉터이고 `id_field` 는 `<셀렉터>@<속성>` 이다
    response: str = JSON_RESPONSE
    # 쪽을 넘기는 법. 없으면 한 번만 부른다
    pagination: ApiPagination | None = None
    # 목록에서 읽은 `date` 가 그 공고의 마감일인가. 참이면 마감이 지난 공고는 상세를 열지 않고
    # 건너뛴다. 게시일을 적어 두는 사이트에서 참으로 두면 어제 올라온 새 공고가 버려진다
    date_is_deadline: bool = False

    @property
    def is_html(self) -> bool:
        return self.response == HTML_RESPONSE


class ApiDetailConfig(BaseModel):
    """상세 API 하나. 공고 하나를 지목해 가져온다."""

    url: str
    method: str
    body: dict[str, Any]
    # 상세 필드 -> 응답 안의 JSON 경로. 배열로 적으면 여러 자리를 모은다
    fields: dict[str, FieldPath]
    # 사이트가 요구하는 기능성 헤더. 목록과 상세가 서로 다른 `referer` 를 요구하기도 한다
    headers: dict[str, str] = {}


class ApiConfig(BaseModel):
    """`crawlers.api_config_json` 에 그대로 들어가는 모양.

    `api` 를 쓰지 않는 쪽은 없어도 된다. 목록만 `api` 인 크롤러는 `list` 만 적는다.
    """

    list: ApiListConfig | None = None
    detail: ApiDetailConfig | None = None

    def to_json(self) -> str:
        return self.model_dump_json()

    def list_config(self) -> ApiListConfig:
        """목록 설정. 없으면 실행할 수 없으므로 이름을 대고 실패한다."""
        if self.list is None:
            raise ApiConfigError("missing_field", "`list` 설정이 없다. 목록을 가져올 수 없다")
        return self.list

    def detail_config(self) -> ApiDetailConfig:
        """상세 설정. 없으면 실행할 수 없으므로 이름을 대고 실패한다."""
        if self.detail is None:
            raise ApiConfigError("missing_field", "`detail` 설정이 없다. 상세를 가져올 수 없다")
        return self.detail


def parse_api_config(text: str | None) -> ApiConfig:
    """저장된 문자열을 파싱하고 검증한다. 비어 있으면 두 쪽 다 없는 설정이다."""
    if text is None or not text.strip():
        return ApiConfig()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ApiConfigError("unparsable", f"JSON 으로 읽을 수 없다: {exc}") from exc
    return validate_api_config(data)


def validate_api_config(data: Any) -> ApiConfig:
    """파싱된 설정을 검증한다. 통과하면 `ApiConfig`, 아니면 `ApiConfigError`."""
    if not isinstance(data, Mapping):
        raise ApiConfigError("unparsable", f"설정이 객체가 아니다: {type(data).__name__}")
    _reject_unknown(data, SECTIONS, "최상위")

    list_config = _list_section(data.get("list"))
    detail_config = _detail_section(data.get("detail"))
    if list_config is None and detail_config is None:
        raise ApiConfigError("missing_field", "`list` 와 `detail` 이 둘 다 없다. 설정이 비었다")
    return ApiConfig(list=list_config, detail=detail_config)


def _list_section(value: Any) -> ApiListConfig | None:
    if value is None:
        return None
    section = _mapping(value, "list")
    _reject_unknown(section, tuple(ApiListConfig.model_fields), "list")

    config = ApiListConfig(
        url=_url(section, "list"),
        method=_method(section, "list"),
        body=_body(section, "list"),
        items_path=_required_text(section, "list", "items_path"),
        fields=_fields(section, "list", LIST_ALLOWED_FIELDS),
        id_field=_required_text(section, "list", "id_field"),
        link_template=_required_text(section, "list", "link_template"),
        headers=_headers(section, "list"),
        body_format=_choice(section, "list", "body_format", BODY_FORMATS, JSON_BODY),
        response=_choice(section, "list", "response", RESPONSE_FORMATS, JSON_RESPONSE),
        pagination=_pagination(section.get("pagination")),
        date_is_deadline=_flag(section, "list", "date_is_deadline"),
    )
    if ID_PLACEHOLDER not in config.link_template:
        # 이 값이 `raw_jobs.source_url` 이 된다. 공고마다 같으면 중복 판정도 링크도 무너진다
        raise ApiConfigError(
            "missing_field",
            f"`list.link_template` 에 {ID_PLACEHOLDER} 가 없다. 공고마다 다른 주소가 되지 않는다",
        )
    return config


def _detail_section(value: Any) -> ApiDetailConfig | None:
    if value is None:
        return None
    section = _mapping(value, "detail")
    _reject_unknown(section, tuple(ApiDetailConfig.model_fields), "detail")

    config = ApiDetailConfig(
        url=_url(section, "detail"),
        method=_method(section, "detail"),
        body=_body(section, "detail"),
        fields=_fields(section, "detail", DETAIL_FIELDS),
        headers=_headers(section, "detail"),
    )
    if ID_PLACEHOLDER not in config.url and ID_PLACEHOLDER not in json.dumps(config.body):
        raise ApiConfigError(
            "missing_field",
            f"`detail` 의 url 이나 body 에 {ID_PLACEHOLDER} 가 없다. "
            "공고가 몇 건이든 같은 상세를 가져온다",
        )
    return config


def _mapping(value: Any, section: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ApiConfigError("unparsable", f"`{section}` 이 객체가 아니다: {type(value).__name__}")
    return value


def _required_text(section: Mapping[str, Any], where: str, name: str) -> str:
    if name not in section:
        raise ApiConfigError("missing_field", f"`{where}.{name}` 이 없다")
    value = section[name]
    if not isinstance(value, str):
        raise ApiConfigError(
            "unparsable", f"`{where}.{name}` 이 문자열이 아니다: {type(value).__name__}"
        )
    text = value.strip()
    if not text:
        raise ApiConfigError("missing_field", f"`{where}.{name}` 이 비어 있다")
    return text


def _url(section: Mapping[str, Any], where: str) -> str:
    url = _required_text(section, where, "url")
    if not url.startswith(("http://", "https://")):
        raise ApiConfigError("missing_field", f"`{where}.url` 이 http 주소가 아니다: {url}")
    return url


def _method(section: Mapping[str, Any], where: str) -> str:
    if "method" not in section:
        # 안 적으면 POST 다. 이 서비스가 만난 목록·상세 API 가 전부 POST 였다
        return "POST"
    method = _required_text(section, where, "method").upper()
    if method not in METHODS:
        raise ApiConfigError(
            "unknown_field", f"`{where}.method` 는 {', '.join(METHODS)} 중 하나다: {method}"
        )
    return method


def _body(section: Mapping[str, Any], where: str) -> dict[str, Any]:
    if "body" not in section or section["body"] is None:
        return {}
    body = section["body"]
    if not isinstance(body, Mapping):
        raise ApiConfigError(
            "unparsable", f"`{where}.body` 가 객체가 아니다: {type(body).__name__}"
        )
    return dict(body)


def _flag(section: Mapping[str, Any], where: str, name: str) -> bool:
    """참/거짓 하나. 안 적으면 거짓이다 — 건너뛰는 쪽이 기본값이면 안 된다."""
    if name not in section or section[name] is None:
        return False
    value = section[name]
    if not isinstance(value, bool):
        raise ApiConfigError(
            "unparsable", f"`{where}.{name}` 이 참/거짓이 아니다: {type(value).__name__}"
        )
    return value


def _pagination(value: Any) -> ApiPagination | None:
    """쪽 넘김 설정. 없으면 None 이고, 그때는 목록을 한 번만 부른다."""
    if value is None:
        return None
    section = _mapping(value, "list.pagination")
    _reject_unknown(section, tuple(ApiPagination.model_fields), "list.pagination")

    config = ApiPagination(
        param=_required_text(section, "list.pagination", "param"),
        start=_whole_number(section, "start", 1, minimum=0),
        max_pages=_whole_number(section, "max_pages", DEFAULT_MAX_PAGES, minimum=1),
        has_next=str(section.get("has_next") or "").strip(),
        total_pages_selector=str(section.get("total_pages_selector") or "").strip(),
        total_pages_attribute=str(section.get("total_pages_attribute") or "").strip(),
    )
    if config.max_pages > PAGE_LIMIT:
        raise ApiConfigError(
            "unknown_field",
            f"`list.pagination.max_pages` 가 상한 {PAGE_LIMIT} 을 넘는다: {config.max_pages}. "
            "쪽마다 요청이 하나씩 나간다",
        )
    if config.has_next and config.total_pages_selector:
        # 둘 다 적으면 어느 쪽으로 멈춘 것인지 사유에서 알 수 없다
        raise ApiConfigError(
            "unknown_field",
            "`list.pagination` 에 `has_next` 와 `total_pages_selector` 가 함께 있다. "
            "마지막 쪽을 판정하는 법은 하나만 적는다",
        )
    if bool(config.total_pages_selector) != bool(config.total_pages_attribute):
        raise ApiConfigError(
            "missing_field",
            "`list.pagination` 의 `total_pages_selector` 와 `total_pages_attribute` 는 "
            "둘 다 있어야 한다. 어느 노드의 어느 속성인지가 함께 있어야 읽을 수 있다",
        )
    return config


def _whole_number(section: Mapping[str, Any], name: str, default: int, *, minimum: int) -> int:
    """쪽 번호와 상한. 정수가 아니면 무엇을 뜻하는지 추측하지 않는다."""
    if name not in section or section[name] is None:
        return default
    value = section[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiConfigError(
            "unparsable",
            f"`list.pagination.{name}` 이 정수가 아니다: {type(value).__name__}",
        )
    if value < minimum:
        raise ApiConfigError(
            "missing_field", f"`list.pagination.{name}` 은 {minimum} 이상이어야 한다: {value}"
        )
    return value


def _choice(
    section: Mapping[str, Any], where: str, name: str, allowed: tuple[str, ...], default: str
) -> str:
    """값이 정해진 몇 가지 중 하나인 설정. 안 적으면 기존 동작이 기본이다."""
    if name not in section or section[name] is None:
        return default
    value = _required_text(section, where, name).lower()
    if value not in allowed:
        raise ApiConfigError(
            "unknown_field", f"`{where}.{name}` 은 {', '.join(allowed)} 중 하나다: {value}"
        )
    return value


def _headers(section: Mapping[str, Any], where: str) -> dict[str, str]:
    """사이트가 요구하는 기능성 헤더. 없으면 빈 값이다.

    현대 목록 API 는 `x-hkmc-service` 와 `referer` 가 없으면 400 을 준다. 그런 헤더를 설정에
    담을 자리가 여기다.

    `User-Agent` 는 담을 수 없다. 이름과 연락처를 밝히는 것은 공용 fetch 클라이언트가 정하고,
    설정으로 덮을 수 있게 두면 브라우저 위장이 크롤러 등록만으로 가능해진다
    (`.claude/rules/crawling.md`).
    """
    if "headers" not in section or section["headers"] is None:
        return {}
    headers = section["headers"]
    if not isinstance(headers, Mapping):
        raise ApiConfigError(
            "unparsable", f"`{where}.headers` 가 객체가 아니다: {type(headers).__name__}"
        )

    result: dict[str, str] = {}
    for name, value in headers.items():
        key = str(name).strip()
        if key.lower() in BLOCKED_HEADERS:
            raise ApiConfigError(
                "unknown_field",
                f"`{where}.headers` 에 {key} 를 담을 수 없다. 이름은 공용 fetch 클라이언트가 "
                "정한다",
            )
        if not isinstance(value, str):
            raise ApiConfigError(
                "unparsable",
                f"`{where}.headers.{key}` 가 문자열이 아니다: {type(value).__name__}",
            )
        if not key:
            raise ApiConfigError("missing_field", f"`{where}.headers` 에 이름 없는 헤더가 있다")
        result[key] = value
    return result


def _fields(
    section: Mapping[str, Any], where: str, allowed: tuple[str, ...]
) -> dict[str, FieldPath]:
    """필드 이름과 경로. 스키마에 없는 이름은 거절한다 — 무엇을 말하려던 것인지 추측하지 않는다."""
    if "fields" not in section:
        raise ApiConfigError("missing_field", f"`{where}.fields` 가 없다")
    fields = section["fields"]
    if not isinstance(fields, Mapping):
        raise ApiConfigError(
            "unparsable", f"`{where}.fields` 가 객체가 아니다: {type(fields).__name__}"
        )
    if not fields:
        raise ApiConfigError(
            "missing_field", f"`{where}.fields` 가 비어 있다. 읽을 값이 하나도 없다"
        )

    unknown = sorted(str(key) for key in fields if key not in allowed)
    if unknown:
        raise ApiConfigError(
            "unknown_field",
            f"`{where}.fields` 에 스키마에 없는 필드가 있다: {', '.join(unknown)}. "
            f"쓸 수 있는 이름은 {', '.join(allowed)} 다",
        )

    result: dict[str, FieldPath] = {}
    for name, path in fields.items():
        result[str(name)] = _field_path(path, where, str(name))
    return result


def _field_path(path: Any, where: str, name: str) -> FieldPath:
    """경로 하나 또는 여럿. 여럿이면 그 자리들을 모아 한 필드를 만든다.

    현대는 주요 업무와 조직 소개가 다른 필드에 나뉘어 있어 한 자리만 읽으면 본문이 반쪽이
    된다. 그런 사이트를 위해 배열로도 적을 수 있다.
    """
    if isinstance(path, str):
        if not path.strip():
            raise ApiConfigError("missing_field", f"`{where}.fields.{name}` 이 비어 있다")
        return path.strip()
    if isinstance(path, list):
        if not path:
            raise ApiConfigError("missing_field", f"`{where}.fields.{name}` 이 빈 배열이다")
        paths: list[str] = []
        for item in path:
            if not isinstance(item, str) or not item.strip():
                raise ApiConfigError(
                    "unparsable",
                    f"`{where}.fields.{name}` 의 경로가 비었거나 문자열이 아니다: {item!r}",
                )
            paths.append(item.strip())
        return paths
    raise ApiConfigError(
        "unparsable",
        f"`{where}.fields.{name}` 이 문자열도 배열도 아니다: {type(path).__name__}",
    )


def _reject_unknown(data: Mapping[str, Any], allowed: tuple[str, ...], where: str) -> None:
    unknown = sorted(str(key) for key in data if key not in allowed)
    if unknown:
        raise ApiConfigError(
            "unknown_field", f"{where} 에 스키마에 없는 필드가 있다: {', '.join(unknown)}"
        )
