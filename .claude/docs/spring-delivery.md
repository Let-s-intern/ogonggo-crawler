# 오공고 백엔드 전달

정규화된 공고를 오공고 백엔드(Spring)로 보내는 경계면이다. `api-contract.md` 가 적은 제공
API 와 방향이 반대다 — 그쪽은 소비 측이 가져가는 자리이고, 이쪽은 우리가 보내는 자리다.

**아직 구현되지 않았다.** 이 문서는 양쪽 스키마를 대조해 무엇을 보낼 수 있고 무엇이 비는지
적어 둔 것이고, 구현 전에 정해야 할 것들을 함께 남긴다.

## 왜 푸시인가

`api-contract.md` 는 1차를 폴링으로 정했고 그 판단은 지금도 유효하다. 방향을 바꾸는 이유는
받는 쪽이 이미 만들어져 있기 때문이다.

오공고 백엔드에 `POST /api/v1/internal/jobs` 가 있고, `X-Internal-Api-Key` 헤더 하나로
서버 간 인증까지 끝나 있다. 우리 쪽 자물쇠는 운영자 비밀번호 하나짜리라 서버끼리 부르는 데
맞지 않고(`api-contract.md` 가 그렇게 적어 두었다), 폴링으로 가면 그 자물쇠를 새로 설계해야
한다. 이미 있는 것을 쓰는 편이 싸다.

제공 API(`GET /api/jobs`, `POST /api/jobs/delivered`)는 그대로 둔다. 지우지 않는 이유는
다른 소비 측이 생길 수 있고, 무엇을 보냈는지 사람이 확인하는 자리이기도 해서다.

## 받는 쪽

```
POST https://<오공고 admin API>/api/v1/internal/jobs
X-Internal-Api-Key: <키>
Content-Type: application/json
```

| 응답 | 뜻 | 우리가 할 일 |
|---|---|---|
| `200` | 등록됨 | `delivered_at` 을 찍는다 |
| `409` `JOB_ALREADY_EXISTS` | 같은 `sourceUrl` 이 이미 있다 | 성공으로 본다. 재시도하지 않는다 |
| `401` | 키가 없거나 다르다 | 설정 문제다. 재시도해도 같다 |
| `400` | 검증 실패 | 우리가 보낸 값이 규칙을 어겼다. 재시도해도 같다 |

중복 판정은 `sourceUrl` 하나로 한다. **갱신 경로가 없다** — 같은 URL 의 공고 내용이 바뀌어도
받는 쪽은 거절한다. 최초 1회만 들어간다.

## 필드 대응

`NORMALIZED_FIELDS`(`app/normalize/rules.py`)와 `CrawlerJobRegistrationRequest`
(오공고 `ogonggo-api-admin`)를 대조한 결과다.

| 크롤러 | 오공고 | 구분 | 비고 |
|---|---|---|---|
| `company` | `companyName` | 필수 | 150자 |
| `title` | `title` | 필수 | 255자 |
| `employment_type` | `employmentType` | 필수 | enum. 아래 매핑 |
| `source_url` | `sourceUrl` | 필수 | 2048자. 중복 판정 기준 |
| `parent_company` | `parentCompanyName` | 선택 | 150자 |
| `work_location` | `region` | 선택 | 100자 |
| `deadline` | `recruitmentEndAt` | 선택 | 시간대 확인 필요 |
| `start_date` | `recruitmentStartAt` | 선택 | 시간대 확인 필요 |
| `career_level` | `experienceMinYears` / `experienceMaxYears` | 선택 | **그대로 못 보낸다.** 아래 |
| `duties` | `responsibilities` | 선택 | |
| `requirements` | `qualifications` | 선택 | |
| `preferred` | `preferredQualifications` | 선택 | |
| `hiring_process` | `hiringProcess` | 선택 | |

### 보낼 곳이 없는 것

| 크롤러 | 왜 |
|---|---|
| `body` | 오공고가 본문을 일곱 칸으로 나눠 받는다. 통짜 본문을 담을 칸이 없다 |
| `etc_info` | 같은 이유 |
| `job_role` | 오공고에 직무 칸이 없다 |
| `job_major`, `job_minor` | `tags` 에 넣지 않기로 했다(2026-09-08 결정). 아래 참고 |

### tags 는 비운다

`tags` 는 오공고가 "AI가 생성한 태그 목록" 으로 받는 칸이고, 우리에게 `job_major`·`job_minor`
라는 분류 결과가 있다. **그래도 보내지 않는다**(2026-09-08 결정).

`job_major`·`job_minor` 는 `job_taxonomy` 표에서 고르는 닫힌 목록이고, 그 목록은 우리 DB 에
있어 배포 없이 바뀐다. 태그로 흘려보내면 오공고에 우리 분류 체계가 문자열로 복제되고, 목록을
고칠 때마다 이미 보낸 것과 어긋난다. 분류 체계를 넘기려면 태그가 아니라 그 목적의 필드가
있어야 한다.

`tags` 는 항상 빈 배열이다.

### 우리가 채울 수 없는 것

`companyAndTeamIntroduction`, `compensation`, `benefits`, `educationLevel` 넷은 오공고가
받지만 `NORMALIZED_FIELDS` 에 대응하는 칸이 없다. 항상 비운 채로 보낸다.

`educationLevel` 은 생략하면 오공고가 `ANY` 로 채운다.

### 오공고가 값에서 유도하는 것

우리가 보내지 않아도 받는 쪽이 정한다.

```
experienceType    경력 연수가 둘 다 없으면 IRRELEVANT, 있으면 EXPERIENCED
recruitmentType   모집 일시가 둘 다 없으면 ALWAYS_OPEN, 있으면 PERIOD
publicationStatus 항상 PUBLISHED — 받는 즉시 공개된다
```

`publicationStatus` 가 `PUBLISHED` 로 고정이라는 것은, **보내는 순간 사용자에게 보인다**는
뜻이다. 검수 전 공고를 보내면 그대로 노출된다.

## employment_type 매핑

두 목록 다 닫혀 있다. 크롤러 쪽은 `api-contract.md` 가 정한 넷이고 분류가 그 안에서만
값을 낸다.

| 크롤러 | 오공고 |
|---|---|
| 정규직 | `FULL_TIME` |
| 계약직 | `CONTRACT` |
| 인턴 | `INTERN` |
| 기타 | `ETC` |
| `null` | **결정 필요** |

오공고의 `PART_TIME` 은 크롤러가 만들지 않는 값이라 쓰이지 않는다.

`null` 은 "본문만으로 판단할 수 없었다" 는 뜻인데 오공고에서는 필수 필드다. 둘 중 하나를
정해야 한다.

| 안 | 성질 |
|---|---|
| 보내지 않는다 | 아래 전송 기준에 자연히 걸린다. 정보가 없는 공고를 내보내지 않는다 |
| `ETC` 로 보낸다 | "기타" 와 "모름" 이 받는 쪽에서 구분되지 않는다 |

## career_level 변환

**그대로 보낼 수 없고, 잘못 보내면 값이 뒤집힌다.**

크롤러는 `신입` / `경력` / `무관` / `null` 넷이고 오공고는 숫자(`experienceMinYears`,
`experienceMaxYears`)를 받는다. 그런데 오공고는 그 숫자로 `experienceType` 을 유도한다 —
값이 있으면 `EXPERIENCED` 다.

그래서 `신입` 을 `0, 0` 으로 보내면 오공고에서 **경력 공고가 된다.** 신입 공고가 경력으로
뒤집히는 것이라 그냥 두면 안 된다.

세 갈래가 있다.

| 안 | 성질 |
|---|---|
| 보내지 않는다 | 전부 `IRRELEVANT` 가 된다. 정보를 버리지만 틀리지는 않는다 |
| 오공고에 `experienceType` 필드를 요청한다 | 오공고에 `NEWCOMER`/`EXPERIENCED`/`IRRELEVANT` enum 이 이미 있다. 우리 값이 그대로 들어간다 |
| `경력` 만 `experienceMinYears = 1` 로 보낸다 | 근거 없는 숫자를 만든다 |

두 번째가 맞다. 원인이 오공고 요청 DTO 가 enum 을 안 받고 숫자만 받는 데 있고, 필드 하나를
더하면 변환 자체가 없어진다.

## 시각 표기

오공고는 `LocalDateTime` 을 받는다 — 시간대가 없는 값이다. 우리는 UTC 로 저장하고 그대로
내보낸다(`api-contract.md` 의 "응답의 시각과 화면의 시각은 다르다").

**어느 시간대로 보낼지 정하지 않으면 아홉 시간이 어긋난다.** 마감일이 하루 밀리거나 당겨지는
종류의 오차라 조용히 틀린다. 오공고 쪽이 이 값을 무엇으로 읽는지 확인하고 정한다.

## 크롤러가 무엇을 어떻게 저장했나

보내기 전에 알아야 할 것은 "이 칸에 무엇이 들어 있나" 가 아니라 **"어떻게 들어왔고 어떤
모양인가"** 다. 칸 이름만 보고 짐작하면 어긋난다.

`normalized_jobs` 의 값은 출처가 셋이다.

| 출처 | 뜻 | 값이 없을 때 |
|---|---|---|
| 수집 | 셀렉터가 페이지에서 뽑고 정규화 규칙을 지난 값 | NULL |
| 판정 | 분류(LLM)가 본문을 읽고 **닫힌 목록에서 고른** 값 | NULL |
| 추출 | 분류가 본문에서 **뽑아 적은** 자유 텍스트 | NULL |

| 칸 | 출처 | 저장된 모양 | 알아야 할 것 |
|---|---|---|---|
| `company` | 수집 | 자유 텍스트 | 공고가 말한 자회사다. 사이트가 주지 않으면 NULL |
| `parent_company` | 수집 | 자유 텍스트 | 규칙을 타지 않는다. `crawlers.default_company`, 비어 있으면 크롤러 이름 |
| `title` | 수집 | 자유 텍스트 | |
| `source_url` | 수집 | URL | NOT NULL. 중복 판정 기준 |
| `deadline` | 수집 | **`YYYY-MM-DD` 텍스트** | 시각도 시간대도 없다. `date_parse` 규칙의 `output_format` 기본값이 `%Y-%m-%d` 다 |
| `start_date` | 수집 | **`YYYY-MM-DD` 텍스트** | `deadline` 과 같다. 사이트가 시작일을 안 주는 곳이 많아 NULL 이 흔하다 |
| `body` | 수집 | 평문 | `html_text` 규칙이 HTML 을 편 것이다. **분류에 넣는 원문**이라 아래 아홉 칸의 출처다 |
| `employment_type` | 판정 | `정규직` `계약직` `인턴` `기타` | 이 넷뿐이다. 고르지 못하면 NULL |
| `career_level` | 판정 | `신입` `경력` `무관` | 이 셋뿐이다. 고르지 못하면 NULL |
| `job_major` | 판정 | `job_taxonomy` 표의 대분류 | 목록이 DB 에 있어 배포 없이 바뀐다 |
| `job_minor` | 판정 | `job_taxonomy` 표의 소분류 | 대분류만 있고 소분류가 NULL 인 행이 있다. 그 반대는 없다 |
| `job_role` | 추출 | 자유 텍스트 | 제목에서 직무를 가리키는 부분만 남긴 것. `전 직군 채용` 처럼 제목이 직무를 말하지 않으면 빈 값 |
| `work_location` | 추출 | 자유 텍스트 | |
| `duties` | 추출 | 자유 텍스트 | |
| `requirements` | 추출 | 자유 텍스트 | |
| `preferred` | 추출 | 자유 텍스트 | |
| `hiring_process` | 추출 | 자유 텍스트 | |
| `etc_info` | 추출 | 자유 텍스트 | |
| `normalized_at` | 자동 | ISO 문자열 | |
| `delivered_at` | 전달 | ISO 문자열 | **전달 경계면만 쓴다.** 재정규화가 건드리지 않는다 |

### 날짜가 날짜형이 아니다

`deadline` 과 `start_date` 는 `YYYY-MM-DD` **텍스트**다. 시각이 없고 시간대도 없다.

오공고는 `LocalDateTime` 을 받으므로 보낼 때 시각을 붙여야 한다. 마감일에 `00:00` 을 붙이면
그날 하루가 통째로 지난 것이 되므로, 마감일은 그날 끝으로 보내는 편이 뜻에 맞는다. 정하지
않으면 하루가 어긋난다.

### 판정 칸에 `판단불가` 는 저장되지 않는다

분류 모델은 `판단불가` 를 낼 수 있다(`app/classify/schema.py` 의 `UNDECIDED`). 그 값은
저장 전에 빈 값이 되고 NULL 로 들어간다(`app/classify/grounding.py`).

> 고르지 않았다는 답이다. 버린 것이 아니라 본문에 근거가 없다는 뜻이라 세지 않는다

그래서 `employment_type` 이 NULL 인 것은 "판단할 근거가 본문에 없었다" 는 뜻이지 오류가
아니다. 오공고에서 이 칸이 필수라는 것과 맞물리는 지점이다.

### 회사명·마감일·시작일은 분류가 덮지 않는다

이 셋은 수집이 채우고, 분류는 본문을 읽어 다른 값이 보여도 **제안만 낸다**
(`company_suggestion`, `deadline_suggestion`, `start_date_suggestion`). 이유가 코드에
적혀 있다.

> `deadline` 은 마감 지난 공고를 거르는 데 쓰이고 `company` 는 계열사를 가르는 값이라, 이
> 셋은 값이 있으면 아무리 근거가 있어도 자동으로 덮지 않고 제안으로만 낸다

제안은 검수 화면에서 사람이 받아들여야 값이 된다. **보낼 때는 제안이 아니라 확정된 값만
본다.**

### 나머지 아홉 칸은 본문에서 나온다

`start_date` 를 뺀 여덟 칸과 `requirements` 는 2026-08-26 부터 수집이 아니라 본문을 나눠서
채운다. 사이트마다 칸 매핑을 적는 방식이 640건에서 절반도 채우지 못했기 때문이다.

그래서 **`body` 가 비면 이 아홉 칸이 통째로 빈다.** 전송 기준 60% 를 못 넘기는 공고는
대개 상세 페이지를 못 가져왔거나 본문 셀렉터가 빗나간 경우다.

## 전송 기준

**60% 채운 공고만 보낸다**(2026-09-08 결정). 비어 있는 칸이 많은 공고를 내보내면 사용자가
열어 보고 아무것도 없는 화면을 만난다.

세는 대상은 **위 대응표에서 실제로 채울 수 있는 13칸**이다. 오공고가 받지만 우리가 채우지
않는 다섯(`companyAndTeamIntroduction`, `compensation`, `benefits`, `educationLevel`, `tags`)은
빼고 센다 — 넣으면 아무리 잘 뽑아도 상한이 72% 라 기준이 뜻을 잃는다.

13칸의 60% 는 7.8 이므로 **8칸 이상**이다. 필수 4칸은 없으면 어차피 못 보내므로, 실질적으로는
**선택 9칸 중 4칸 이상**이라는 뜻이다.

`career_level` 을 보낼지가 아직 정해지지 않았다(위 절). 보내지 않기로 하면 세는 칸이 12개로
줄고 기준은 8칸 그대로다 — 선택 8칸 중 4칸이 된다.

이 기준은 값이 있는지만 본다. 내용이 쓸 만한지는 판단하지 않는다.

## 파이썬 쪽에 남은 일

### 구현

`app/deliver/` 에 설정(`settings.py`)만 있고 **보내는 코드가 없다.** 모듈 주석이 그렇게
적어 두었다 — "이 Push 는 자리를 만드는 데까지고, 실제 전송은 다음 일이다".

필요한 것은 넷이다.

- 대응표대로 `normalized_jobs` 행을 요청 본문으로 옮기는 변환
- 전송 기준(60%) 판정
- `409` 를 성공으로 보는 응답 처리와, 되풀이해도 소용없는 실패(`400`, `401`)의 구분
- 보낸 건에 `delivered_at` 찍기. 이 컬럼은 전달 경계면만 쓴다(`.claude/rules/data-safety.md`)

### 설정

`app/deliver/settings.py` 에 자리가 이미 있다 — `deliver_url`, `deliver_method`,
`deliver_auth_header`, `deliver_batch_size`. 화면은 `/ui/deliver` 다.

`auth_header` 는 `"이름: 값"` 한 줄이라 `X-Internal-Api-Key: <키>` 를 그대로 넣을 수 있다.

**마스킹이 없다.** `app/storage/settings.py` 에는 `SECRET_KEYS` 와 `mask()` 가 있고
`app/api/ui_storage.py` 가 끝 네 자리만 보여주는데, `deliver` 에는 그것이 없다.
`app/templates/fragments/deliver_form.html:40` 이 `type="text"` 로 값을 그대로 그린다.
키를 넣는 자리이므로 `storage` 와 같은 방식으로 가려야 한다.

### 규칙 문서

`.claude/rules/crawling.md` 가 직접 HTTP 호출을 `app/notify/` 와 `app/storage/` **둘만**
허용한다고 못 박고 있다("These two are the whole list"). `app/deliver/` 가 세 번째가 되므로
그 문단을 같은 커밋에서 고친다. 우리가 운영하는 서비스라는 예외 사유는 앞의 둘과 같다.

### 계약 문서

`.claude/docs/api-contract.md` 는 "소비 측인 채용공고 사이트는 아직 붙지 않았다" 와
"소비 측에 자격증명을 어떻게 줄지는 아직 정하지 않았다" 를 전제로 쓰여 있다. 푸시가 붙으면
그 두 문단이 사실과 어긋난다. 계약과 구현은 같은 커밋에서 고친다.

## 오공고 쪽에 요청할 것

| 요청 | 왜 |
|---|---|
| `experienceType` 필드 추가 | 위 career_level 변환. enum 이 이미 있는데 요청 DTO 가 숫자만 받는다 |
| `@Operation(operationId = ...)` 명시 | 별도 건이다. springdoc 이 메서드 이름으로 operationId 를 짓는데 `UserJobApi.getJobs` 와 `CompanyJobApi.getJobs` 가 겹쳐 `getJobs` / `getJobs_1` 로 갈린다. 번호가 스캔 순서에 달려 있어 클래스 이름만 바꿔도 프런트 생성 코드가 다른 API 를 가리킨다. jobs·bootcamps 목록·상세 4쌍이다 |

## 정하지 않은 것

- `employment_type` 이 `null` 인 공고를 보낼지
- `career_level` 을 어떻게 보낼지 (오공고 요청을 기다릴지, 지금은 버릴지)
- 시각을 어느 시간대로 보낼지
- 언제 보낼지 — 크롤링이 끝난 뒤 이어서인지, 별도 주기인지, 사람이 누르는지
- 실패한 건을 다시 보낼지, 보낸다면 몇 번인지
