# 정규화

`raw_jobs` 를 읽어 `normalized_jobs` 를 만드는 일이다. 구현은 `app/normalize/` 다.

## 방향은 뒤집히지 않는다

읽는 것은 `raw_jobs`, 쓰는 것은 `normalized_jobs` 다. `app/normalize/engine.py` 에
`raw_jobs` 를 대상으로 하는 UPDATE 나 DELETE 는 없다 — SELECT 하나뿐이다.

정규화가 raw 를 고치면 잘못된 규칙 하나가 수집 데이터를 영구히 망가뜨린다. 크롤링은 다시
돌릴 수 없다.

이 방향 덕분에 나쁜 규칙이 복구 가능해진다. 원본이 남아 있으므로 규칙을 고치고 다시 돌리면
된다.

## 값이 만들어지는 순서

```
raw_data_json 의 원문 필드
   ↓  규칙        priority 오름차순. 앞 결과가 뒤 입력이 된다
   ↓  분류        아홉 칸을 통째로 덮는다
   ↓  사람 보정   job_field_overrides 가 마지막에 덮는다
normalized_jobs 한 행
```

한 필드에 규칙이 여럿이면 `priority` 오름차순으로 적용하고, `enabled=false` 인 규칙은
건너뛴다. 같은 `priority` 면 `id` 순이다 — 순서가 정해지지 않으면 같은 입력에 같은 결과가
나온다는 보장이 없다.

정렬과 `enabled` 판정은 규칙을 어디서 받았든 엔진에서 다시 한다. DB 의 ORDER BY 에만 맡기면
규칙 목록을 손으로 만들어 넣는 경로에서 조용히 순서가 뒤집힌다.

## 규칙 타입은 다섯이다

규칙 하나는 "어떤 필드를(`field_name`) 어떤 방식으로(`rule_type`) 어떻게
(`rule_config_json`)" 의 세 조각이다.

| rule_type | 설정 키 | 하는 일 |
|---|---|---|
| `mapping` | `map` (필수), `default` | 값이 `map` 의 키와 정확히 같을 때 바꾼다 |
| `regex` | `pattern` (필수), `replacement` | `re.sub(pattern, replacement, value)` |
| `trim` | `collapse_whitespace`, `strip_chars` | 연속 공백을 하나로 접고 양끝을 깎는다 |
| `date_parse` | `formats` (필수), `output_format` | 적힌 순서대로 읽고 다시 쓴다 |
| `html_text` | 없음 | HTML 조각을 평문으로 편다 |

`mapping` 에서 표에 없는 값은 `default` 를 쓰고, `default` 도 없으면 원문을 그대로 둔다.

`html_text` 에 설정이 없는 이유는 무엇을 줄바꿈으로 볼지가 값마다 고를 일이 아니기 때문이다.
HTML 이 정하는 것이고 그 목록은 `app/crawler/parser.py` 에 하나만 있다.

## 검증은 저장 전에 한다

`rule_config_json` 은 자유 형식 문자열이라 DB 의 CHECK 로는 막을 수 없다. 컴파일되지 않는
정규식이나 알 수 없는 키가 들어간 설정은 저장되는 순간 그 규칙을 쓰는 모든 실행이 같은
예외로 죽는다.

키가 표에 없으면 거부한다. 오타 하나가 조용히 무시되면 규칙이 안 먹는 이유를 아무도 찾지
못한다.

| reason | 뜻 |
|---|---|
| `unknown_type` | `rule_type` 이 다섯 가지 중 하나가 아니다 |
| `unknown_field` | `field_name` 이 `normalized_jobs` 에 없는 컬럼이다 |
| `invalid_config` | 타입은 맞는데 설정이 그 타입의 스키마에 맞지 않다 |

## 여섯 칸은 수집이, 아홉 칸은 분류가 가진다

수집이 채우는 것은 `title` `body` `company` `deadline` `start_date` `source_url` 여섯이다.
나머지 아홉 칸은 공고를 읽어 나눈 결과가 채운다 (`.claude/docs/classification.md`).

분류가 있으면 그 아홉 칸은 전부 분류 값이다. 규칙이 만든 값이 있어도 덮고, 분류가 빈 칸을
냈으면 빈 칸이 된다. 칸의 출처가 하나여야 소비 측이 한 가지 규칙으로 읽는다 (2026-08-26 결정).

빈 칸까지 덮는 것이 핵심이다. 채워진 칸만 덮으면 2026-08-26 이전에 수집된 행에 옛 매핑이
넣어 둔 값(`Permanent`)이 남고, 그 순간 판정 칸 둘의 닫힌 목록이 계약대로 성립하지 않는다
(`.claude/docs/api-contract.md`).

분류가 아직 돌지 않았으면 아무것도 하지 않는다. 그 행은 규칙이 만든 값을 그대로 유지하고,
나중에 분류를 돌리면 다음 정규화에서 넘어간다.

분류가 낸 값에는 규칙을 태우지 않는다. 규칙은 사이트가 준 원문의 모양을 맞추려고 쓴 것이고,
본문에서 그대로 옮겨 온 값에 걸면 뜻이 달라진다.

## 회사명 두 칸

| 칸 | 어디서 오나 | 비면 |
|---|---|---|
| `parent_company` | 그 크롤러의 `crawlers.default_company`, 오직 그것뿐 | NULL |
| `company` | `raw_data_json.company` 그대로 | NULL |

`parent_company` 가 비었을 때 크롤러 이름으로 대신 채우지 않는다. 2026-08-26 에는 그렇게
했지만, 그러면 모회사가 운영자가 적은 값인지 시스템이 추측한 값인지 화면에서 갈리지 않았다.
2026-08-29 에 크롤러 등록 화면이 이 칸을 필수로 바꾸면서 추측이 필요 없어졌다. 그 전에
등록돼 비어 있던 행은 `migrations/0022_backfill_default_company.sql` 이 한 번 채웠다.

`company` 를 모회사 이름으로 채우지 않는다. 채우면 두 칸이 같은 값이 되어 칸을 가른 일이
없던 일이 된다. 자회사가 비어 있다는 것은 "이 사이트는 계열사를 말하지 않는다" 는 사실이고,
그 사실이 값으로 남아야 한다.

`company` 에는 다른 필드와 똑같이 규칙이 적용된다. `삼성전기(주)` 를 `삼성전기` 로 맞추는
것은 `mapping` 규칙의 일이다. `parent_company` 에는 규칙을 태우지 않는다 — 사이트가 준
원문이 아니라 운영자가 크롤러에 적어 둔 값을 그대로 옮기는 칸이다.

## 처음 보는 회사는 행이 생긴다

`normalized_jobs` 에 한 건이 들어갈 때 그 회사의 `companies` 행이 없으면 로고가 빈 행을
만든다 (`app/companies.py`). 이름은 자회사가 있으면 자회사, 없으면 모회사다.

있는 행은 덮지 않는다. 만드는 자리는 `insert_normalized()` 하나다 — 값을 미리 보는 경로에서
만들면 규칙 화면에서 미리보기를 누른 것만으로 회사가 늘어난다.

## 사람이 고친 값이 마지막이다

`job_field_overrides` 에 그 건에 대해 사람이 고쳐 둔 값이 있으면 규칙이 만든 값 위에 덮는다.
보정이 없는 필드는 그대로 둔다.

## 재정규화는 운영자가 누를 때만 돈다

규칙을 저장해도 기존 `normalized_jobs` 는 그대로다 (2026-08-21 결정). 규칙 저장에 재처리를
묶지 않는 이유는 하나다 — 규칙 다섯 개를 손보는 동안 같은 데이터를 다섯 번 다시 쓰게 된다.

트리거는 `POST /api/rules/renormalize` 이고 동작은 `app/normalize/backfill.py` 다.
진행 상황은 `GET /api/rules/renormalize` 로 읽는다.

재정규화가 건드리지 않는 것이 둘 있다. `raw_jobs` 는 읽기만 하고, `delivered_at` 은 그대로
둔다 — 소비 측이 이미 가져간 표시를 지우면 같은 데이터가 다시 넘어간다
(`.claude/rules/data-safety.md`).

`crawl_runs` 에도 쓰지 않는다. 크롤링 실행이 아니기 때문이다.

동시에 하나만 돈다. 진행 상황은 한 프로세스 안의 메모리에 두고, 돌고 있는 동안 들어온 요청은
거부한다(`BackfillRunningError`).

## 실행 중 정규화가 실패하면

크롤링 실행 안에서 도는 정규화는 그 건에서 끝난다. 실패해도 `raw_jobs` 행은 남고
`fail_count` 로 세어져, 규칙을 고친 뒤 재정규화로 복구된다 (`.claude/docs/crawl-run.md`).
