"""규칙 적용. `raw_jobs` 를 읽고 `normalized_jobs` 에만 쓴다.

이 방향은 뒤집히지 않는다. 정규화가 raw 를 고치면 잘못된 규칙 하나가 수집 데이터를 영구히
망가뜨리고, 크롤링은 다시 돌릴 수 없다 (`.claude/rules/data-safety.md`). 그래서 이 파일에
`raw_jobs` 를 대상으로 하는 UPDATE 나 DELETE 는 없다 — SELECT 하나뿐이다.

## 한 필드에 규칙이 여럿일 때

`priority` 오름차순으로 적용하고, 앞 규칙의 결과가 뒤 규칙의 입력이 된다. `enabled=false` 인
규칙은 건너뛴다. 같은 `priority` 면 `id` 순이다 — 순서가 정해지지 않으면 같은 입력에 같은
결과가 나온다는 보장이 없다.

정렬과 `enabled` 판정은 규칙을 어디서 받았든 이 파일에서 다시 한다. DB 의 ORDER BY 에만
맡기면 규칙 목록을 손으로 만들어 넣는 경로에서 조용히 순서가 뒤집힌다.

## 회사명은 두 칸이다

`parent_company` 는 그 크롤러의 `crawlers.default_company`, **오직 그것뿐이다.** 비어 있으면
NULL 이다 — 크롤러 이름으로 대신 채우지 않는다.

**2026-08-26 에는 비어 있으면 크롤러 이름을 대신 썼다.** 목록이 회사명을 주지 않는
사이트(토스·우아한형제들)를 위한 것이었지만, 그러면 모회사가 운영자가 적은 값인지 시스템이
추측한 값인지 화면에서 갈리지 않았다. 2026-08-29 에 크롤러 등록 화면이 이 칸을 필수로
바꾸면서(빈 값으로 저장할 수 없다) 그 추측이 필요 없어졌다 — 모회사는 언제나 **설정한 값**
이다. 이 결정 전에 등록돼 비어 있던 행은 `migrations/0022_backfill_default_company.sql`
이 그 시점의 크롤러 이름으로 한 번 채웠다. 그 뒤로 새로 만들거나 비운 행은 없다.

`company` 는 `raw_data_json.company` 그대로이고, 뽑히지 않았으면 NULL 이다. **모회사 이름으로
채우지 않는다.** 채우면 두 칸이 같은 값이 되어 칸을 가른 일이 없던 일이 된다. 자회사가 비어
있다는 것은 "이 사이트는 계열사를 말하지 않는다" 는 사실이고, 그 사실이 값으로 남아야 한다.

칸이 하나였을 때는 둘을 합쳐 넣고 어느 쪽을 썼는지 `company_source` 에 적었다. 칸 이름이
출처를 말하게 된 뒤로 그 열은 할 말이 없다 (`migrations/0018_parent_company.sql`).

`company` 에는 다른 필드와 똑같이 규칙이 적용된다. "삼성전기(주)" 를 "삼성전기" 로 맞추는
것은 `mapping` 규칙의 일이다. `parent_company` 에는 규칙을 태우지 않는다 — 사이트가 준 원문이
아니라 운영자가 크롤러에 적어 둔 값을 그대로 옮기는 칸이다.

## 처음 보는 회사는 행이 생긴다

`normalized_jobs` 에 한 건이 들어갈 때 그 회사의 `companies` 행이 없으면 로고가 빈 행을
만든다 (`app/companies.py`). 이름은 자회사가 있으면 자회사, 없으면 모회사다. 있는 행은
덮지 않는다 — 운영자가 화면에서 고친 로고와 모회사 이름이 다음 수집에 도로 덮이면 그 화면은
아무 일도 하지 못한다.

만드는 자리는 `insert_normalized` 하나다. 값을 미리 보는 경로(`normalized_values`)에서
만들면 규칙 화면에서 미리보기를 누른 것만으로 회사가 늘어난다.

## 여섯 칸은 수집이, 아홉 칸은 분류가 가진다

수집은 어느 사이트나 확실히 주는 여섯 칸만 한다 — `title` `body` `company` `deadline`
`start_date` `source_url`. 나머지 아홉 칸은 공고를 읽어 나눈 결과가 채운다 (`app/classify/`).
그 결과는 `job_classifications` 에 따로 남아 있다.

**분류가 있으면 그 아홉 칸은 전부 분류 값이다.** 규칙이 만든 값이 있어도 덮고, 분류가 빈 칸을
냈으면 빈 칸이 된다. 칸의 출처가 하나여야 소비 측이 한 가지 규칙으로 읽는다 (2026-08-26 결정).

빈 칸까지 덮는 것이 핵심이다. 채워진 칸만 덮으면 2026-08-26 이전에 수집된 행에 옛 매핑이 넣어
둔 값(`Permanent`)이 남고, 그 순간 판정 칸 둘의 닫힌 목록이
`.claude/docs/api-contract.md` 가 약속한 대로 성립하지 않는다.

수집이 주는 여섯 칸은 분류가 건드리지 않는다. `deadline` 은 마감 지난 공고를 거르는 데 쓰이고
`company` 는 계열사를 가르는 값이라 본문 판독으로 바꿀 것이 아니다.

분류가 없으면(아직 돌지 않았으면) 아무것도 하지 않는다. 그 행은 규칙이 만든 값을 그대로
유지하고, 나중에 분류를 돌리면 다음 정규화에서 넘어간다.

분류가 낸 값에는 규칙을 태우지 않는다. 규칙은 사이트가 준 원문의 모양을 맞추려고 쓴 것이고,
본문에서 그대로 옮겨 온 값에 걸면 뜻이 달라진다.

## 규칙 다음에 사람 보정이다

규칙을 다 태운 뒤 `job_field_overrides` 에 그 건의 그 필드가 있으면 사람이 정한 값으로 덮는다.
이 순서여야 둘 다 산다. 규칙을 개선하면 보정하지 않은 필드는 같이 좋아지고, 보정한 필드는
사람이 정한 값을 유지한다. 보정 행을 지우면 다음 정규화에서 규칙이 만든 값으로 돌아간다.

보정도 `raw_jobs` 처럼 읽기만 한다. 정규화가 사람이 고친 값을 다시 쓰면 규칙 하나가 검수 결과를
덮어쓰게 되고, 그것이 이 테이블을 따로 둔 이유를 없앤다.

`parent_company` 는 보정 대상이 아니다. 모회사가 틀렸으면 크롤러의 값을 고치고 재정규화한다 —
공고 한 건이 아니라 그 크롤러가 모은 전부가 함께 고쳐지고, 그것이 맞는 단위다.

## 빈 값에는 규칙을 적용하지 않는다

셀렉터가 아무것도 못 뽑은 필드는 정규화할 것이 없다. 규칙을 태우지 않고 NULL 로 둔다.
빈 문자열에 `date_parse` 를 걸면 값이 없다는 사실이 "규칙이 실패했다" 로 둔갑한다.

## 실패는 예외다

규칙이 값을 처리하지 못하면 `NormalizeError` 를 던지고, 그 건은 `normalized_jobs` 에
들어가지 않는다. 절반만 적용된 행은 겉보기에 멀쩡해서 아무도 틀린 줄 모른다. raw 는 남아
있으므로 규칙을 고쳐 재정규화하면 복구된다.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from bs4 import BeautifulSoup

from app import companies
from app.classify.schema import STORED_CLASSIFY_FIELDS
from app.classify.store import read_classification, read_parts

# 어디서 줄이 바뀌어야 하는지는 HTML 이 정하고, 그 목록은 저기 하나뿐이다. 여기에 같은
# 목록을 두 벌 두면 한쪽만 늘어나는 날이 오고 그때 어느 쪽이 진실인지 알 수 없다
# (`.claude/rules/core.md`).
from app.crawler.parser import BLOCK_TAGS
from app.normalize.rules import (
    NORMALIZED_FIELDS,
    DateParseConfig,
    HtmlTextConfig,
    MappingConfig,
    RegexConfig,
    Rule,
    RuleConfigError,
    TrimConfig,
    build_rule,
)

_WHITESPACE = re.compile(r"\s+")

# 값이 HTML 인지 판정하는 눈. 태그 하나도 엔티티 하나도 없으면 손대지 않는다 — 규칙이 걸린
# 필드에 평문이 들어오는 것은 정상이고, 그것을 파서에 넣으면 `a < b` 같은 문장이 깨진다.
_MARKUP = re.compile(r"<[A-Za-z/!][^>]*>|&(?:#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z]\w{1,31});")

# 블록 경계 표시. 텍스트에 원래 있던 줄바꿈과 구별해야 해서 제어문자를 쓴다. 경계가 겹치면
# (`</p><p>`) 줄 하나로 합친다 — 태그 두 개가 빈 줄을 만들 이유는 없다.
_MARK = "\x00"
_BOUNDARY = re.compile(rf"[\s{_MARK}]*{_MARK}[\s{_MARK}]*")

# 가로 공백만 줄인다. `&nbsp;` 가 풀린 U+00A0 도 여기서 보통 공백이 된다
_HORIZONTAL = re.compile(r"[^\S\n]+")

# 빈 줄이 셋 이상이면 둘로. 원문 텍스트에 있던 빈 줄만 여기까지 온다
_BLANK_RUN = re.compile(r"\n{3,}")

# 규칙이 만드는 필드가 아니라 크롤러가 정하는 값이다. `NORMALIZED_FIELDS` 에 넣지 않는다 —
# 그 목록은 "규칙이 값을 바꿀 수 있는 칸" 이고 이 칸은 그대로 옮기는 자리다.
PARENT_COMPANY = "parent_company"

# 사람이 고칠 수 있는 필드. `job_field_overrides.field_name` 의 CHECK 제약과 같은 값이어야
# 한다. `source_url` 은 공고의 신원이라 들어 있지 않다.
#
# 0012 가 그 CHECK 를 열여섯 칸으로 넓혔고, 0016 이 칸 셋을 지운 뒤로 CHECK 쪽이 더 넓다 —
# 지운 칸의 보정 행은 되돌릴 때 필요해서 남겨 두었다
# (`migrations/0016_drop_department_category_headcount.sql`). 이 목록에 없으므로 읽히지 않는다.
# 두 목록을 하나로 합치지 않는 것은 뜻이 다르기 때문이다 — 이쪽은 "사람이 고쳐도 되는 칸"
# 이고, 언젠가 고치면 안 되는 칸이 생기면 여기서만 빠진다. 늘릴 때는 마이그레이션과 같은
# 커밋에서 늘린다. 코드만 넓히면 DB 가 거절하고, 그 실패는 운영자가 저장을 누른 뒤에야
# 드러난다.
OVERRIDABLE_FIELDS: tuple[str, ...] = NORMALIZED_FIELDS


class NormalizeError(RuntimeError):
    """규칙 하나가 값을 처리하지 못했다. 그 건은 정규화되지 않는다."""

    def __init__(self, field_name: str, rule_type: str, message: str, rule_id: int | None) -> None:
        where = (
            f"{field_name}/{rule_type}"
            if rule_id is None
            else f"{field_name}/{rule_type}#{rule_id}"
        )
        super().__init__(f"정규화 실패({where}): {message}")
        self.field_name = field_name
        self.rule_type = rule_type
        self.rule_id = rule_id


class RawJobMissingError(LookupError):
    """정규화하려는 `raw_jobs` 행이 없다."""


def load_rules(conn: sqlite3.Connection) -> list[Rule]:
    """저장된 규칙을 읽는다. 설정이 깨진 행은 그 필드의 정규화를 실패시킨다.

    설정을 여기서 고치지 않는다. 저장 단계가 이미 검증했으므로, 읽는 쪽에서 깨진 설정을 만났다면
    누군가 DB 를 직접 고쳤다는 뜻이고 그것은 조용히 넘어갈 일이 아니다.
    """
    rows = conn.execute(
        """
        SELECT id, field_name, rule_type, rule_config_json, priority, enabled
          FROM normalization_rules
         ORDER BY field_name, priority, id
        """
    ).fetchall()
    return [_rule_from_row(row) for row in rows]


def normalize_fields(
    raw: Mapping[str, object],
    rules: Sequence[Rule],
    parent_company: str | None = None,
    classification: Mapping[str, str] | None = None,
) -> dict[str, str | None]:
    """원문 필드에서 `normalized_jobs` 의 값들을 만든다. 값이 없는 필드는 None 이다.

    `parent_company` 는 규칙을 타지 않고 받은 값 그대로 나온다. 빈 값은 NULL 이다 — 빈
    문자열로 채우면 "모회사를 모른다" 와 "모회사가 빈 이름이다" 가 구분되지 않는다.

    `company` 는 이제 다른 필드와 똑같다. 뽑히지 않았으면 NULL 이고, 모회사 이름이 그 자리를
    메우지 않는다.
    """
    ordered = _by_field(rules)
    result: dict[str, str | None] = {}
    for field_name in NORMALIZED_FIELDS:
        raw_value = raw.get(field_name)
        value = raw_value if isinstance(raw_value, str) else ""
        if not value:
            result[field_name] = None
            continue
        for rule in ordered.get(field_name, ()):
            value = _apply(value, rule)
            if not value:
                # 규칙이 값을 비웠으면 거기서 멈춘다. 빈 값을 다음 규칙에 넘기면
                # date_parse 가 읽을 것이 없다며 실패하고, 그 공고가 통째로 빠진다.
                # "상시채용" 을 mapping 으로 비우는 것이 이 경로다 — deadline 만 NULL 이 되고
                # 공고는 남아야 한다.
                break
        result[field_name] = value or None
    # 마감을 못 뽑았거나 규칙이 비웠으면(`상시채용` 매핑 등) "상시모집" 으로 채운다
    # (2026-08-29 결정). 빈 마감은 셀렉터가 놓친 것과 정말 마감이 없는 상시채용을 화면에서
    # 구분하지 못했다 — `career_level` 을 "무관" 으로 채운 것과 같은 실사용 판단이다
    if result.get("deadline") is None:
        result["deadline"] = "상시모집"
    result[PARENT_COMPANY] = parent_company if parent_company and parent_company.strip() else None
    return apply_classification(result, classification)


def apply_classification(
    fields: dict[str, str | None], classification: Mapping[str, str] | None
) -> dict[str, str | None]:
    """아홉 칸과 직무 분류 둘을 분류 결과로 바꾼다. 규칙이 만든 값이 있어도 덮고, 빈 칸이면
    비운다.

    분류가 없으면(아직 돌지 않았으면) 아무것도 하지 않는다. 그 공고는 규칙이 만든 값을 그대로
    가진 채로 남고, 나중에 분류를 돌리면 재정규화 없이도 다음 정규화에서 넘어간다.

    수집이 주는 여섯 칸은 `STORED_CLASSIFY_FIELDS` 에 없으므로 여기를 지나가지 않는다.
    `job_major`/`job_minor` 는 `job_taxonomy` 표에서 고르는 판정 칸이라 정적 스키마 목록
    (`CLASSIFY_FIELDS`)에는 없지만, 저장 경로는 이 둘까지 아는 `STORED_CLASSIFY_FIELDS` 를
    쓴다(`app/classify/schema.py`).

    **`career_level` 만 빈 칸을 "무관" 으로 채운다 (2026-08-28 결정).** 근거가 없어 판단하지
    못한 것과 사이트가 경력무관이라고 밝힌 것은 원래 다른 뜻이지만, 실사용에서는 사이트가
    경력을 아예 언급하지 않은 공고 대부분이 실제로 경력무관이다. 다른 칸은 이 규칙을 타지
    않는다 — 빈 칸이 그대로 있어야 검수 화면에서 못 뽑은 것을 잡아낼 수 있다.
    """
    if not classification:
        return fields
    for name in STORED_CLASSIFY_FIELDS:
        fields[name] = classification.get(name, "").strip() or None
    if fields.get("career_level") is None:
        fields["career_level"] = "무관"
    return fields


def read_parent_company(conn: sqlite3.Connection, raw_job_id: int) -> str | None:
    """그 공고를 모은 크롤러가 말하는 모회사. 없으면 None 이다. 읽기 전용이다.

    **운영자가 크롤러 등록·수정 화면에 적은 `crawlers.default_company` 그대로다.** 크롤러
    이름으로 대신 채우지 않는다 — 2026-08-29 부터 그 화면이 이 칸을 필수로 받으므로, 새로
    만든 행에는 빈 값이 없다. 이 화면 이전에 만들어져 비어 있던 행은
    `migrations/0022_backfill_default_company.sql` 이 한 번 채웠다.
    """
    row = conn.execute(
        """
        SELECT c.default_company AS default_company
          FROM raw_jobs r
          JOIN workflows w ON w.id = r.workflow_id
          JOIN crawlers c ON c.id = w.crawler_id
         WHERE r.id = ?
        """,
        (raw_job_id,),
    ).fetchone()
    if row is None or row["default_company"] is None:
        return None
    value = str(row["default_company"]).strip()
    return value or None


def read_raw(conn: sqlite3.Connection, raw_job_id: int) -> tuple[str, dict[str, object]]:
    """`raw_jobs` 한 행의 `source_url` 과 파싱된 원문 필드. 읽기 전용이다."""
    row = conn.execute(
        "SELECT source_url, raw_data_json FROM raw_jobs WHERE id = ?", (raw_job_id,)
    ).fetchone()
    if row is None:
        raise RawJobMissingError(f"raw_jobs {raw_job_id} 가 없다")
    try:
        data = json.loads(row["raw_data_json"])
    except json.JSONDecodeError as exc:
        raise NormalizeError(
            "(전체)", "json", f"raw_data_json 을 읽을 수 없다: {exc}", None
        ) from exc
    if not isinstance(data, dict):
        raise NormalizeError(
            "(전체)", "json", f"raw_data_json 이 객체가 아니다: {type(data).__name__}", None
        )
    return str(row["source_url"]), data


def read_overrides(conn: sqlite3.Connection, raw_job_id: int, part: int = 1) -> dict[str, str]:
    """그 건에 사람이 고쳐 둔 값. 필드명이 키다. 읽기 전용이다.

    허용 목록 밖의 필드명은 버린다. CHECK 가 이미 막고 있지만, 그 방어가 사라지는 경로는
    누군가 DB 를 직접 고친 경우뿐이고 그때 정규화가 엉뚱한 컬럼을 쓰게 두지 않는다.
    """
    rows = conn.execute(
        "SELECT field_name, value FROM job_field_overrides WHERE raw_job_id = ? AND part = ?",
        (raw_job_id, part),
    ).fetchall()
    return {
        str(row["field_name"]): str(row["value"])
        for row in rows
        if str(row["field_name"]) in OVERRIDABLE_FIELDS
    }


def apply_overrides(
    fields: dict[str, str | None], overrides: Mapping[str, str]
) -> dict[str, str | None]:
    """규칙이 만든 값 위에 사람이 고친 값을 덮는다. 보정이 없는 필드는 그대로 둔다.

    보정이 빈 문자열이면 그 필드는 NULL 이다. 규칙 결과가 빈 값일 때와 같은 취급이라
    읽는 쪽이 "값 없음" 을 한 가지로만 보게 된다.
    """
    for field_name, value in overrides.items():
        if field_name not in OVERRIDABLE_FIELDS:
            continue
        fields[field_name] = value or None
    return fields


@dataclass(frozen=True)
class PostingPart:
    """정규화할 공고 하나. 나누지 않은 공고는 1번 하나이고 `split` 이 False 다."""

    number: int = 1
    role: str = ""
    split: bool = False


def posting_parts(conn: sqlite3.Connection, raw_job_id: int) -> list[PostingPart]:
    """그 수집 건에서 나올 공고들. 분류가 나눈 대로이고, 아직 분류되지 않았으면 1번 하나다."""
    stored = read_parts(conn, raw_job_id)
    if not stored:
        return [PostingPart()]
    split = len(stored) > 1
    return [PostingPart(number=part.part, role=part.role, split=split) for part in stored]


def normalized_values(
    conn: sqlite3.Connection,
    raw_job_id: int,
    rules: Sequence[Rule],
    part: PostingPart | None = None,
) -> tuple[str, dict[str, str | None]]:
    """공고 하나의 `source_url` 과 확정 값. 규칙을 먼저 태우고 그 위에 사람 보정을 덮는다.

    최초 정규화와 재정규화가 같은 값을 내려면 두 경로가 이 함수 하나를 지나야 한다. 순서를
    각자 조립하면 한쪽에서만 보정이 빠지고, 그 차이는 재정규화를 돌린 뒤에야 드러난다.

    나눈 공고는 제목 뒤에 직무 이름을 붙이고(`원래 제목 - 직무 이름`) 주소 뒤에 번호를
    붙인다(`...#2`) (2026-09-11 결정). 주소가 같으면 소비 측이 같은 공고로 보고 뒤의 것을
    버린다. 사람이 고친 제목은 그 위에 덮인다.

    직무가 조직 아래 나뉜 공고는 직무 이름에 조직 이름 줄이 함께 온다(`HS사업본부` / `기계`).
    제목과 `job_role` 에는 한 줄로 이어 넣는다 — 줄바꿈이 든 제목은 목록에서 잘려 보인다.
    """
    part = part or PostingPart()
    source_url, data = read_raw(conn, raw_job_id)
    fields = normalize_fields(
        data,
        rules,
        read_parent_company(conn, raw_job_id),
        read_classification(conn, raw_job_id, part.number),
    )
    if fields.get("job_role"):
        fields["job_role"] = " ".join(str(fields["job_role"]).split())
    if part.split:
        source_url = f"{source_url}#{part.number}"
        role = " ".join(part.role.split())
        if role:
            title = fields.get("title")
            fields["title"] = f"{title} - {role}" if title else role
    return source_url, apply_overrides(fields, read_overrides(conn, raw_job_id, part.number))


def insert_normalized(
    conn: sqlite3.Connection,
    raw_job_id: int,
    rules: Sequence[Rule],
    part: PostingPart | None = None,
) -> int:
    """`raw_jobs` 한 행을 정규화해 `normalized_jobs` 에 넣는다. 새 행의 id 를 돌려준다.

    `delivered_at` 은 쓰지 않는다. 제공 API 경로만 쓴다 (`.claude/rules/data-safety.md`).
    """
    part = part or PostingPart()
    source_url, fields = normalized_values(conn, raw_job_id, rules, part)
    companies.register(conn, fields["company"], fields[PARENT_COMPANY])
    # 컬럼 이름은 이 모듈의 상수에서만 온다. 밖에서 오는 값이 들어오지 않는다. 손으로 적은
    # 목록을 두면 칸이 늘 때마다 여기와 `NORMALIZED_FIELDS` 가 갈리고, 갈린 순간 새 칸은
    # 조용히 NULL 로만 남는다
    columns = (*NORMALIZED_FIELDS, PARENT_COMPANY)
    cursor = conn.execute(
        f"""
        INSERT INTO normalized_jobs
               (raw_job_id, part, source_url, {", ".join(columns)})
        VALUES (?, ?, ?, {", ".join("?" for _ in columns)})
        """,
        (raw_job_id, part.number, source_url, *(fields[name] for name in columns)),
    )
    return int(cursor.lastrowid or 0)


def _by_field(rules: Sequence[Rule]) -> dict[str, list[Rule]]:
    """필드별 적용 순서. 꺼진 규칙은 여기서 빠진다."""
    grouped: dict[str, list[Rule]] = {}
    for rule in rules:
        if not rule.enabled:
            continue
        grouped.setdefault(rule.field_name, []).append(rule)
    for items in grouped.values():
        items.sort(key=lambda rule: (rule.priority, rule.id if rule.id is not None else 0))
    return grouped


def _apply(value: str, rule: Rule) -> str:
    config = rule.config
    if isinstance(config, MappingConfig):
        if value in config.map:
            return config.map[value]
        return value if config.default is None else config.default

    if isinstance(config, RegexConfig):
        try:
            return re.sub(config.pattern, config.replacement, value)
        except re.error as exc:
            raise NormalizeError(rule.field_name, rule.rule_type, str(exc), rule.id) from exc

    if isinstance(config, TrimConfig):
        trimmed = _WHITESPACE.sub(" ", value) if config.collapse_whitespace else value
        return trimmed.strip(config.strip_chars) if config.strip_chars else trimmed.strip()

    if isinstance(config, HtmlTextConfig):
        return flatten_html(value)

    return _parse_date(value, rule, config)


def flatten_html(value: str) -> str:
    """HTML 조각을 사람이 읽는 평문으로 편다. HTML 이 아니면 원문 그대로 돌려준다.

    LG 상세 API 가 `detailContext` 와 `requiredItem` 을 HTML 조각으로 주고, 그것이 그대로
    `normalized_jobs.body` 에 남아 소비 측으로 나갔다. 수집에서 지우면 `raw_jobs` 가 원본이
    아니게 되므로 (`.claude/rules/data-safety.md`) 여기서 편다.

    한 번에 세 가지가 일어난다. 블록 태그는 줄바꿈이 되고, 남은 태그는 사라지고, 엔티티는
    원래 글자로 돌아온다. `<br>` 를 그냥 지우면 앞뒤 문장이 한 줄로 붙어 버리므로 순서가
    아니라 한 동작이어야 한다.

    빈 줄은 원문 텍스트에 있던 것만 남는다. `</p><p>` 처럼 태그 경계가 겹쳐서 생긴 줄바꿈은
    하나로 합치고, `<p>&nbsp;</p>` 처럼 내용이 공백뿐인 문단은 경계에 흡수돼 사라진다.
    """
    if not _MARKUP.search(value):
        return value

    soup = BeautifulSoup(value.replace(_MARK, ""), "html.parser")
    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_before(_MARK)
        tag.insert_after(_MARK)

    # 주석(`<!--StartFragment-->`)은 get_text() 가 가져오지 않는다
    text = _BOUNDARY.sub("\n", soup.get_text())
    text = _HORIZONTAL.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_RUN.sub("\n\n", text).strip()


def _parse_date(value: str, rule: Rule, config: DateParseConfig) -> str:
    """`formats` 를 순서대로 시도한다. 하나도 맞지 않으면 실패다.

    맞지 않는 값을 원문 그대로 통과시키지 않는다. 그러면 `deadline` 컬럼에 날짜와 "상시채용"
    이 섞여 들어가고, 소비 측은 그것을 날짜로 읽는다. 날짜가 아닌 표기가 섞이는 사이트라면
    앞 순번에 `mapping` 규칙을 두어 먼저 걸러야 한다.
    """
    text = value.strip()
    for fmt in config.formats:
        try:
            return datetime.strptime(text, fmt).strftime(config.output_format)
        except ValueError:
            continue
    raise NormalizeError(
        rule.field_name,
        rule.rule_type,
        f"어느 형식으로도 날짜로 읽지 못했다: {value!r} (시도: {', '.join(config.formats)})",
        rule.id,
    )


def _rule_from_row(row: sqlite3.Row) -> Rule:
    try:
        return build_rule(
            row["field_name"],
            row["rule_type"],
            row["rule_config_json"],
            priority=int(row["priority"]),
            enabled=bool(row["enabled"]),
            rule_id=int(row["id"]),
        )
    except RuleConfigError as exc:
        raise NormalizeError(
            row["field_name"], row["rule_type"], f"저장된 설정이 잘못됐다: {exc}", int(row["id"])
        ) from exc
