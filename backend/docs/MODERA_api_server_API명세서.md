# MODERA_API_FRONTEND 명세서

# API 명세서

기준: 기능명세서2 개정본(2026-07-16) · `backend/docs/database-erd.md` (Liquibase 034 반영)
Base URL: `https://{host}/api/v1`

## 설계 범위 및 반영 사항

- 객체 원본은 `user_schema`, `image_schema`, `taxonomy_schema`, `document_schema`, `schedule_schema`가 소유한다.
- 도메인 간 관계는 `library_schema`가 ID로 관리하고, 화면 조회는 `query_schema` Read Model을 사용한다.
- 문서와 일정은 현재 ERD에 존재하는 정식 도메인이다. DB 관계 기준의 권장 API 경로를 임시 등록했으며, 세부 Request/Response는 Spring Controller/DTO 확정 후 작성한다.
- 검색 기록 및 `image_search_document`는 현재 DB에서 제거되었으므로 관련 API는 명세 대상에서 제외한다.
- 스키마 간 물리 FK가 없는 관계는 Spring에서 객체 존재 여부와 사용자 소유권을 검증한다.
- DTL-002: OCR 텍스트는 수정 불가로 반영(제목·요약·태그·카테고리·구조화 필드만 수정 가능). ※ 헤딩에 취소선이 있어 기능 자체 제외인지 팀 확정 필요
- SCH-004 검색 대상에서 지식 엔티티·지식 관계 제거
- 인증은 로컬 ID/PW와 카카오 로그인을 지원한다. 카카오는 최초 로그인 시 자동 가입하며, 동의받은 이메일을 [users.email](http://users.email)에 저장·동기화한다.
- Swagger 구현 기준(2026-07-30): 현재 `/v3/api-docs`에 노출된 외부 API는 인증 5개와 이미지 등록, 업로드 URL 재발급, 목록, 단건 조회 총 9개다. 해당 API는 Swagger 경로와 DTO 기준이며, 나머지는 Spring 미완성 설계 초안이다.
- `A201`, `I201` 같은 도메인 성공 코드는 프론트 디버깅과 API별 응답 식별을 위해 Spring 응답에 그대로 사용한다.

---

# 1. 공통 규약

## 1.1 공통 응답 형식

모든 응답은 아래 envelope로 통일한다.

**단건/일반 응답**

```json
{
  "result": "SUCCESS",
  "code": "SUCCESS",
  "message": "요청이 성공했습니다.",
  "data": { },
  "timestamp": "2026-07-16T06:00:00.000Z"
}
```

**페이지 응답** — `data` 내부 구조 고정

```json
{
  "result": "SUCCESS",
  "code": "SUCCESS",
  "message": "요청이 성공했습니다.",
  "data": {
    "list": [ { } ],
    "page": 0,
    "size": 20,
    "totalElements": 135,
    "totalPages": 7,
    "hasNext": true,
    "hasPrevious": false
  },
  "timestamp": "2026-07-16T06:00:00.000Z"
}
```

**에러 응답** — HTTP 상태코드와 함께 사용, `data`에는 상세(문자열 또는 필드 오류 배열)

```json
{
	"result": "FAIL",
  "code": "IMAGE_NOT_FOUND",
  "message": "이미지를 찾을 수 없습니다.",
  "data": "imageId: 1024",
  "timestamp": "2026-07-16T06:00:00.000Z"
}
```

검증 오류(400)의 경우 `data`에 필드 단위 오류 배열을 허용한다.

```json
{
	"result": "FAIL",
  "code": "INVALID_PARAMETER",
  "message": "요청 값이 올바르지 않습니다.",
  "data": [
    { "field": "contentHash", "message": "필수 값입니다." }
  ],
  "timestamp": "2026-07-16T06:00:00.000Z"
}
```

## 1.2 규약

| 항목 | 규칙 |
| --- | --- |
| 인증 | `Authorization: Bearer {accessToken}` (auth API 제외 전부 필수) |
| Content-Type | 외부 API의 JSON Request/Response는 `application/json; charset=utf-8`. presigned URL을 이용한 S3 바이너리 업로드는 실제 파일의 미디어 타입을 따른다. |
| 시간 | ISO-8601 UTC (`2026-07-16T06:00:00.000Z`) |
| 페이지네이션 파라미터 | `page`(0-base), `size`(기본 20, 최대 100), `sort`(예: `updatedAt,desc`) |
| ID | 서버 발급 숫자 ID |
| 코드 | `result`는 성공 시 `SUCCESS` 고정. 성공 `code`는 도메인별 디버깅 코드(A/I/AN/T/S/U/D + 3자리), 실패 `code`는 1.3 및 각 API의 에러 코드. |
| Request | 요청 envelope는 사용하지 않는다. JSON body에는 DTO 필드만 전달하며 Path Parameter와 Query Parameter는 body와 분리한다. 요청 본문이 없으면 `Request Body: 없음`으로 표기한다. |
| Response | 외부 API 성공·실패 응답은 항상 1.1 envelope를 사용한다. 각 API의 `Response data` 예시도 확인 편의를 위해 전체 envelope로 표기한다. 단, `/internal/**`, actuator, Swagger 및 S3 직접 업로드 응답은 제외한다. |

## 1.3 공통 에러 코드

| code | HTTP |
| --- | --- |
| INVALID_PARAMETER | 400 |
| UNAUTHORIZED | 401 |
| FORBIDDEN | 403 |
| NOT_FOUND | 404 |
| CONFLICT | 409 |
| INTERNAL_ERROR | 500 |

도메인 에러 코드는 각 API에 표기. (예: `DUPLICATE_IMAGE`, `ANALYSIS_IN_PROGRESS`)

## 1.4 분석 단계와 상태 enum

```
stage
LLM                 analysis-worker 내부 OCR 정보성 분석 
IMAGE_ANALYSIS      서버 이미지 분석
AGENT               OCR·이미지 분석 결과 종합
INDEXING            결과 저장·임베딩·색인

status
QUEUED              실행 대기
PROCESSING          실행 중
COMPLETED           완료
FAILED              실패
EMPTY               OCR 텍스트가 비어 있어 LLM 판정을 수행할 수 없음(LLM 단계 전용)
CANCELED            취소
```

- API 서버는 이미지 업로드 완료 이벤트를 Redis Streams에 발행하고, analysis-worker가 이를 소비해 분석한다.
- analysis-worker가 발행한 분석 완료·실패 이벤트를 API 서버가 소비해 결과를 저장한다.
- 정보가 없다면 “기타”(미정)으로 분류한다.
- AGENT는 IMAGE_ANALYSIS가 `COMPLETED`일 때 시작한다.
- 개별 작업은 `stage + status` 조합으로 표현한다.
- `currentStatus`는 이미지 전체 분석의 종합 상태를 나타낸다.
- 종합 상태 우선순위는 `FAILED > CANCELED > PROCESSING > QUEUED > COMPLETED`이다. 생성되지 않은 분기 단계는 계산에서 제외하고, 필요한 모든 활성 단계가 완료되어야 `COMPLETED`가 된다.

---

# 2. 전체 API 명세표

| # | Method | Path | 설명 |
| --- | --- | --- | --- |
| 3-1 | POST | /auth/register | 로컬 회원가입 |
| 3-2 | POST | /auth/login | 로컬 로그인 |
| 3-3 | POST | /auth/kakao/login | 카카오 로그인 · 최초 로그인 시 자동 가입 |
| 3-4 | POST | /auth/refresh | 토큰 재발급 |
| 3-5 | POST | /auth/logout | 로그아웃 |
| 4-1 | POST | /images | 이미지·OCR 등록 및 Presigned URL 발급 |
| 4-2 | POST | /images/{imageId}/upload-url | 업로드 URL 재발급 |
| 5-1 | GET | /images | 이미지 목록 |
| 5-2 | GET | /images/{imageId} | 분석 완료 이미지 상세 조회 |
| 5-3 | DELETE | /images | 선택 이미지 삭제(단건·다건) |
| 5-4 | PUT | /images/{imageId}/favorite | 즐겨찾기 설정·해제 |
| 5-5 | POST | /images/search/semantic | 자연어 기반 AI 이미지 검색 |
| 5-6 | POST | /images/{imageId}/related | 이미지 상세 기반 관련 자료 검색 |
| 5-7 | POST | /images/documentize | 다중 이미지 문서화 기반 관련 자료 검색 |
| 6-1 | GET | /categories | 카테고리 목록 |
| 7-1 | GET | /user | 내 정보 |
| 7-2 | DELETE | /user/delete | 저장 데이터 초기화 |
| 7-3 | PATCH | /user/settings | 설정 변경 |
| 7-4 | PUT | /user/devices/{deviceId} | FCM 토큰 등록·갱신 |
| 7-5 | DELETE | /user/devices/{deviceId} | FCM 토큰 해제 |
| 7-6 | GET | /policies/privacy | 개인정보 처리 안내 |
| 7-7 | FCM | - | 이미지 업로드·분석 완료 또는 실패 알림 |
| 8-1 | GET | /documents | 내 문서 목록 조회 |
| 8-2 | POST | /documents | 문서 생성 |
| 8-3 | GET | /documents/{documentId} | 문서 상세 조회 |
| 8-4 | GET | /documents/{documentId}/images | 문서를 구성하는 이미지 목록 조회 |
| 8-5 | DELETE | /documents/{documentId} | 문서 삭제 |
| 8-6 | POST | /documents/{documentId}/images | 이미지 추가 후 새 문서 생성 요청 |
| 8-7 | POST | /documents/{documentId}/images/exclude | 이미지 제외 후 새 문서 생성 요청 |
| 9-1 | GET | /schedules | 내 일정 후보·캘린더 일정 목록 조회 |
| 9-2 | DELETE | /schedules/{scheduleId} | 일정 삭제 |
| 9-3 | PUT | /schedules/{scheduleId}/calendar | 캘린더 등록 상태 변경 |

---

# 3. 인증 API

## 3-1 로컬 회원가입

| 항목 | 내용 |
| --- | --- |
| API | `POST /api/v1/auth/register` |
| 인증 | 불필요 |
| 설명 | 사용자가 새로운 계정을 등록한다. 비밀번호는 bcrypt 암호화 저장 |

### Request

```json
{
  "loginId": "newUser123",
  "password": "securePassword123!",
  "email": "user@example.com"
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| loginId | String | 필수 | 로그인 ID, 중복 불가, 4~20자 |
| password | String | 필수 | 8자 이상, 서버에서 bcrypt 저장 |
| email | String | 필수 | 이메일 형식, 중복 불가 |

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "A201",
  "message": "회원가입이 완료되었습니다.",
  "data": {
    "userId": 1
  },
  "timestamp": "2026-07-16T06:00:00.000Z"
}
```

### 에러

| code | HTTP | 설명 |
| --- | --- | --- |
| DUPLICATE_LOGIN_ID | 409 | 이미 사용 중인 ID |
| DUPLICATE_EMAIL | 409 | 이미 사용 중인 이메일 |

---

## 3-2 로컬 로그인

| 항목 | 내용 |
| --- | --- |
| API | `POST /api/v1/auth/login` |
| 인증 | 불필요 |
| 설명 | ID/PW 검증 후 JWT 발급 |

### Request Body

```json
{
  "loginId": "newUser123",
  "password": "securePassword123!",
  "deviceId": "android-device-uuid"
}
```

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "A202",
  "message": "요청이 성공했습니다.",
  "data": {
    "accessToken": "eyJhbGciOi...",
    "refreshToken": "eyJhbGciOi...",
    "userId": 1
  },
  "timestamp": "2026-07-16T06:00:00.000Z"
}
```

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| accessToken | String | JWT, 만료 30분 |
| refreshToken | String | 만료 14일 |
| userId | Number | 사용자 ID |

### 에러

| code | HTTP | 설명 |
| --- | --- | --- |
| LOGIN_FAILED | 401 | ID 또는 비밀번호 불일치(구분하지 않음) |

---

## 3-3 카카오 로그인 · 최초 로그인 자동 가입

| 항목 | 내용 |
| --- | --- |
| API | POST /api/v1/auth/kakao/login |
| 인증 | 불필요 |
| 설명 | 카카오 인가 코드로 로그인한다. provider=KAKAO, providerId=카카오 사용자 ID 계정이 없으면 최초 로그인 시 자동 가입한다. |

### Request

```json
{
  "authorizationCode": "kakao-authorization-code",
  "deviceId": "android-device-uuid"
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| authorizationCode | String | 필수 | 카카오 OAuth 인가 코드 |
| deviceId | String | 필수 | Refresh Token을 관리할 기기 식별값 |

### 처리 규칙

- 카카오 사용자 ID로 기존 계정을 조회하며, 없으면 별도 회원가입 API 없이 자동 생성한다.
- 카카오 동의로 전달된 이메일은 소문자로 정규화하여 [users.email](http://users.email)에 저장한다.
- 기존 카카오 계정의 이메일이 비어 있거나 변경된 경우 다음 로그인 시 동기화한다.
- 동일 이메일이 다른 MODERA 계정에서 사용 중이면 이메일 저장만 생략하며 계정을 자동 병합하지 않는다.
- 같은 사용자의 동일 deviceId 로그인은 해당 기기의 Refresh Token을 갱신한다.

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "A203",
  "message": "요청이 성공했습니다.",
  "data": {
    "accessToken": "eyJhbGciOi...",
    "refreshToken": "eyJhbGciOi...",
    "userId": 1
  },
  "timestamp": "2026-07-16T06:00:00.000Z"
}
```

### 에러: KAKAO_LOGIN_FAILED (401)

---

## 3-4 토큰 재발급

| 항목 | 내용 |
| --- | --- |
| API | `POST /api/v1/auth/refresh` |
| 인증 | 불필요(refreshToken으로 검증) |

### Request Body

```json
{
  "refreshToken": "eyJhbGciOi...",
  "deviceId": "android-device-uuid"
}
```

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "A204",
  "message": "요청이 성공했습니다.",
  "data": {
    "accessToken": "eyJhbGciOi...",
    "refreshToken": "eyJhbGciOi...",
    "userId": 6
  },
  "timestamp": "2026-07-16T06:00:00.000Z"
}
```

Refresh Token은 회전 발급되며 기존 토큰은 즉시 무효화한다.

### 에러: `INVALID_REFRESH_TOKEN` (401)

---

## 3-5 로그아웃

| 항목 | 내용 |
| --- | --- |
| API | `POST /api/v1/auth/logout` |
| 인증 | Bearer |
| 설명 | 현재 기기의 refreshToken을 폐기해 로그아웃한다. accessToken은 짧은 만료 시간을 유지하거나 별도 denylist 정책을 적용한다. |

### Request

```json
{
  "refreshToken": "eyJhbGciOi...",
  "deviceId": "android-device-uuid"
}
```

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "A205",
  "message": "요청이 성공했습니다.",
  "data": {
    "loggedOut": true
  },
  "timestamp": "2026-07-16T06:00:00.000Z"
}
```

### 에러

| code | HTTP | 설명 |
| --- | --- | --- |
| INVALID_REFRESH_TOKEN | 401 | 유효하지 않거나 이미 폐기된 refreshToken |

---

# 4. 이미지 등록·업로드·OCR API

## 4-1 이미지 등록 및 Presigned URL 발급

| 항목 | 내용 |
| --- | --- |
| API | `POST /api/v1/images` |
| 인증 | Bearer |
| Swagger operationId | `register` |
| 설명 | 이미지 메타데이터와 클라이언트 OCR 원문을 등록하고, `contentHash`와 실제 MinIO/S3 객체 존재 여부에 따라 기존 사용자 관계를 복구하거나 직접 업로드용 presigned PUT URL을 발급한다. 실제 바이너리는 응답의 `presignedURL`로 직접 PUT한다. 별도 업로드 완료 API는 호출하지 않으며 ObjectCreated 이벤트를 API 서버가 받아 업로드 상태를 갱신하고 Worker에 분석 시작 이벤트를 발행한다. |

### Request Body

```json
{
  "images": [
    {
      "clientRequestId": "d95db8b7-897e-412c-8924-eef3c7bca039",
      "fileName": "60.jpg",
      "contentHash": "a1b2c3d4e5f678901234567890123456789012345678901234567890123456",
      "fileSize": 546543,
      "ocr": {
        "rawText": "클라이언트에서 인식한 OCR 원문"
      }
    }
  ]
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| images | Array | 필수 | 최소 1개 |
| images[].clientRequestId | UUID | 필수 | 클라이언트에서 작업 요청한 요청 ID 값 |
| images[].fileName | String | 필수 | 원본 파일 명 |
| images[].contentHash | String | 필수 | SHA-256, 64자리 16진수(`^[0-9a-fA-F]{64}$`) |
| images[].fileSize | Integer | 필수 | 파일 크기 |
| images[].ocr | Object | 필수 | 클라이언트 OCR 결과 |
| images[].ocr.rawText | String | 필수 | OCR 추출 문자열 |

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "I201",
  "message": "요청이 성공했습니다.",
  "data": {
    "registered": [
      {
        "clientRequestId": "d95db8b7-897e-412c-8924-eef3c7bca039",
        "imageId": 1024,
        "fileName": "60.jpg",
        "presignedURL": "https://minio.example.com/...",
        "uploadExpiresIn": 600
      }
    ],
    "duplicated": [
      {
        "clientRequestId": "d95db8b7-897e-412c-8924-eef3c7bca039",
        "fileName": "duplicate.jpg",
        "existingImageId": 987
      }
    ],
    "failed": [
      {
        "clientRequestId": "d95db8b7-897e-412c-8924-eef3c7bca039",
        "fileName": "invalid.gif",
        "reason": "UNSUPPORTED_FORMAT"
      }
    ]
  },
  "timestamp": "2026-07-28T06:00:00.000Z"
}
```

### 처리 규칙

- API 서버는 `contentHash`로 기존 `image_asset`을 조회하고 실제 MinIO/S3 객체 존재 여부도 확인한다.
- 신규 해시이면 이미지 메타데이터, 사용자-이미지 관계와 OCR 원문을 저장하고 `registered`에 Presigned URL을 반환한다.
- 기존 해시이고 스토리지 객체가 존재하면 바이너리를 다시 업로드하지 않는다. `del_yn='Y'`인 데이터는 존재하지 않는 것으로 간주하며 복구하지 않는다. 요청 사용자의 활성 `library_schema.user_image` 관계가 없으면 새 관계를 생성하고, 기존 이미지의 활성 분석 결과를 복사해 새 조회 행을 생성한 뒤 `duplicated`에 기존 `imageId`를 반환한다.
- 기존 해시이지만 스토리지 객체가 없으면 기존 `imageId`와 `s3Key`를 재사용하고 새 활성 사용자 관계를 생성한 뒤 `registered`에 새 Presigned URL을 반환하여 업로드부터 다시 진행한다.
- 같은 사용자의 활성 관계가 이미 있으면 새 이미지 객체나 관계를 만들지 않고 `duplicated`에 기존 `imageId`를 반환한다.
- `clientRequestId`는 클라이언트가 이미지별로 생성하며, `registered`, `duplicated`, `failed` 모든 결과에 그대로 반환한다.
- 같은 사용자가 동일한 `clientRequestId`로 재요청하면 새 이미지를 만들지 않고 기존 등록 결과를 반환하도록 멱등 처리한다.
- Swagger의 URL 필드명은 `presignedURL`을 유지한다.
- HTTP 400: 요청 검증 실패, HTTP 401: accessToken 없음 또는 무효.

### Storage 업로드 이후 API 서버 처리

1. 클라이언트가 `presignedURL`로 이미지 바이너리를 직접 PUT한다.
2. API 서버는 Storage의 ObjectCreated Webhook을 받아 `uploadStatus=UPLOADED`로 변경한다.
3. API 서버는 저장한 OCR을 조회하여 Worker에 `IMAGE_UPLOADED` 이벤트를 발행한다.
4. 이벤트 발행 시 `analysisStatus=PROCESSING`으로 변경한다.

### API 서버 → Worker 발행 이벤트 — 구현 예정 계약

```json
{
  "eventType": "IMAGE_UPLOADED",
  "version": 1,
  "payload": {
    "imageId": 1024,
    "userId": 1,
    "s3Key": "images/1024/original.jpg",
    "clientOcr": {
      "rawText": "클라이언트에서 인식한 OCR 원문"
    }
  }
}
```

> Worker → API 서버 분석 결과 이벤트의 payload는 Worker 담당자와 합의 후 별도로 확정한다. 이 명세에서는 API 서버가 발행하는 이벤트까지만 계약한다.
> 

---

## 4-2 이미지 업로드 URL 재발급

| 항목 | 내용 |
| --- | --- |
| API | `POST /api/v1/images/{imageId}/upload-url` |
| 기능 | IMG-001 |
| 설명 | 업로드하지 못한 상태에서 Presigned URL이 만료된 경우 재발급한다. |

### Request: 없음(빈 body)

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "I204",
  "message": "요청이 성공했습니다.",
  "data": {
    "imageId": 1024,
    "presignedURL": "https://s3.ap-northeast-2.amazonaws.com/...",
    "uploadExpiresIn": 600
  },
  "timestamp": "2026-07-16T06:00:00.000Z"
}
```

### 에러

| code | HTTP | 설명 |
| --- | --- | --- |
| IMAGE_NOT_FOUND | 404 | 이미지가 없음 |
| UPLOAD_ALREADY_COMPLETED | 409 | 업로드가 이미 완료됨 |
| ANALYSIS_IN_PROGRESS | 409 | 분석이 이미 시작됨 |

---

# 5. 이미지·지식 데이터 API

## 5-1 이미지 목록 조회

| 항목 | 내용 |
| --- | --- |
| API | `GET /api/v1/images` |
| 기능 | SCH-006(필터·정렬), 목록 화면 |

### Query Parameters

| 파라미터 | 타입 | 설명 |
| --- | --- | --- |
| favorite | Boolean | 즐겨찾기 true / false |
| page | Number | 페이지 번호, 0부터 시작. 기본값 `0` |
| size | Number | 페이지 크기. 기본값 `20`, 최대 `100` |
| sort | String | 정렬 기준. `TITLE_ASC`, `UPLOADED_DESC`, `UPLOADED_ASC` 중 하나. 기본값 `UPLOADED_DESC` |
| keyword | String | 태그, 제목에 속한 키워드 |
| categoryId | Number | 카테고리 번호 |

### 정렬 규칙

| sort 값 | 화면 표시 | 정렬 기준 |
| --- | --- | --- |
| `TITLE_ASC` | 사전순 | 이미지 제목 `title` 오름차순 |
| `UPLOADED_DESC` | 최신 업로드순 | `uploadedAt` 내림차순. 기본 정렬 |
| `UPLOADED_ASC` | 오래된 순 | `uploadedAt` 오름차순 |
- 동일한 정렬값은 `imageId ASC`를 보조 정렬로 사용해 페이지 간 순서를 고정한다.
- 지원하지 않는 `sort` 값은 `INVALID_PARAMETER`(400)로 응답한다.

### 호출 예시

```
GET /api/v1/images?categoryId=3&keyword=프로그래밍&sort=UPLOADED_DESC&page=0&size=20
GET /api/v1/images?keyword=C%2B%2B&page=0&size=20
```

### Response `data` (페이지 형식, list 항목)

```json
{
  "result": "SUCCESS",
  "code": "I205",
  "message": "요청이 성공했습니다.",
  "data": {
    "list": [
      {
        "imageId": 1024,
        "title": "C++ 프로그래밍 입문",
        "summary": "교보문고에서 판매 중인 C++ 프로그래밍 입문서, 32,000원",
        "favorite": false,
        "thumbnailUrl": "https://.../thumb.jpg",
        "tags": ["C++", "쇼핑"],
        "category": "공부",
        "uploadedAt" : "2026-07-16T06:00:00.000Z"
      }
    ],
    "page": 0,
    "size": 20,
    "totalElements": 1,
    "totalPages": 1,
    "hasNext": false,
    "hasPrevious": false
  },
  "timestamp": "2026-07-16T06:00:00.000Z"
}
```

※ DB에는 썸네일 객체의 스토리지 식별자인 `thumbnail_key`를 저장하고, API 서버가 목록 조회 시 이 키로 Presigned GET URL을 생성하여 `thumbnailUrl`로 반환한다.

- `thumbnailUrl`의 유효시간은 1시간이며 URL이 만료되어도 스토리지의 썸네일 객체는 삭제되지 않는다.
- 클라이언트가 목록을 다시 조회하면 API 서버가 새로운 `thumbnailUrl`을 발급한다.
- `thumbnailUrl`은 썸네일이 아직 생성되지 않았거나 썸네일 객체가 삭제된 경우에만 `null`이다.

### 조회 규칙

- `analysis_status`가 `COMPLETED` 또는 `EMPTY`인 이미지만 목록에 반환한다. `EMPTY`는 OCR·분석 결과가 비어 있는 정상 처리 상태이므로 이미지 자체는 목록에 노출한다.
- 로그인한 사용자와 이미지의 `library_schema.user_image.del_yn='N'`이고, 대응하는 `query_schema.user_image_view.del_yn='N'`인 이미지만 목록에 반환한다.
- 두 관계 중 하나라도 `del_yn='Y'`이면 목록과 페이지 집계(`totalElements`, `totalPages`)에서 제외한다.
- `categoryId`와 `keyword`는 선택값이다.
- `keyword`는 앞뒤 공백을 제거하며, 빈 문자열은 미지정으로 처리한다.
- `keyword`가 이미지 제목 또는 태그 이름 중 하나에 부분 일치하면 조회한다.
- `categoryId`와 `keyword`를 함께 전달하면 카테고리 조건 AND (제목 OR 태그명) 조건으로 조회한다.

---

## 5-2 이미지 단건 조회

| 항목 | 내용 |
| --- | --- |
| API | `GET /api/v1/images/{imageId}` |
| 인증 | Bearer |
| Swagger operationId | `getImage` |
| 설명 | 본인이 등록한 이미지 중 분석이 완료됐거나 분석할 내용이 없어 `EMPTY`로 정상 종료된 이미지의 상세 정보를 조회한다. 분석 대기·진행 중·실패 상태의 이미지는 상세 조회할 수 없다. 다른 사용자 소유이거나 존재하지 않는 imageId는 리소스 존재 여부를 숨기기 위해 모두 IMAGE_NOT_FOUND(404)로 응답한다. |

### Path Parameter

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| imageId | Integer | 필수 | 조회할 이미지 ID |

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "I206",
  "message": "요청이 성공했습니다.",
  "data": {
    "imageId": 1024,
    "imageUrl": "https://...presigned-get...",
	  "thumbnailUrl": "https://...presigned-get...",
    "title": "C++ 프로그래밍 입문",
    "favorite": false,
    "summary" : "집에가고 싶다...",
    "category" : "집",
    "tags" : [ "C++", "쇼핑"],
    "keyInformation" : ["가격: 32,000원", "판매처: 교보문고"],
      "scheduledData": {
         "type": "schedule",
         "startAt": "2026-08-03T05:30:00Z",
         "endAt": "2026-08-03T07:00:00Z"
    }, 
    "isDocumented" : true,
    "isCalendared" : true
  },
  "timestamp": "2026-07-28T06:00:00.000Z"
}
```

### 처리 규칙

- `query_schema.user_image_view.analysis_status`가 `COMPLETED` 또는 `EMPTY`인 이미지만 상세 조회한다.
- `EMPTY`는 OCR·분석 결과가 비어 있는 정상 처리 상태이므로 이미지와 존재하는 메타데이터를 반환한다.
- 분석 상태가 `QUEUED`, `PROCESSING`, `FAILED` 또는 그 밖의 미완료 상태이면 `IMAGE_ANALYSIS_NOT_COMPLETED`(409)로 응답한다.
- `scheduledData`는 `type`이 `schedule`이면 AI가 추출한 년/월/일/시 조각을 한국 시간(Asia/Seoul)으로 해석해 `startAt`/`endAt` ISO-8601 UTC 타임스탬프로 변환해 내려준다(9-1 일정 응답과 같은 형식). 추출하지 못한 쪽은 `null`이다. 그 외 `type`은 `{type, fields}` 구조를 그대로 내려준다. AI 콜백 계약(년/월/일/시 조각)은 바뀌지 않는다.

### 에러

- `UNAUTHORIZED` (401)
- `IMAGE_NOT_FOUND` (404)
- `IMAGE_ANALYSIS_NOT_COMPLETED` (409)

---

## 5-3 선택 이미지 삭제(단건·다건)

| 항목 | 내용 |
| --- | --- |
| API | `DELETE /api/v1/images` |
| 기능 | MNG-002, 전체 사진 편집 모드 일괄 삭제 |
| 설명 | 하나 이상의 이미지 ID를 받아 요청 사용자와 이미지 사이의 관계 및 사용자별 조회 데이터를 soft delete한다. 단건 삭제도 `imageIds`에 ID 하나를 전달한다. 객체 테이블은 변경하지 않으며, MinIO/S3 객체 정리는 다른 활성 사용자 관계를 고려하는 별도 정리 정책으로 처리한다. |

### Request

```json
{
  "imageIds": [1024, 1025, 1026]
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| imageIds | Number[] | 필수 | 삭제할 이미지 ID. 1개 이상, 최대 100개 |

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "I207",
  "message": "요청이 성공했습니다.",
  "data": {
    "deletedImageIds": [1024, 1025, 1026],
    "alreadyDeletedImageIds": [], 
    "failed": [], 
    "deletedCount": 3,
    "failedCount": 0
  },
  "timestamp": "2026-07-28T06:00:00.000Z"
}
```

- 일부 이미지가 존재하지 않거나 삭제에 실패해도 처리 가능한 이미지는 계속 삭제하고 결과를 항목별로 반환한다.
- 요청 사용자와 이미지에 해당하는 `library_schema.user_image` 및 `query_schema.user_image_view` 행의 `del_yn`을 `N`에서 `Y`로 변경한다.
- 해당 사용자의 `library_schema.user_favorite_image` 관계가 있으면 `del_yn='Y'`로 변경한다.
- 삭제 이미지가 요청 사용자의 문서에 포함되어 있으면 해당 `library_schema.image_document`와 `query_schema.document_image_view` 행도 `del_yn='Y'`로 변경한다.
- 영향을 받은 각 문서의 `query_schema.user_document_view.del_image_count`를 증가시킨다. 클라이언트는 이 값이 `0`이 아니면 문서 갱신이 필요한 것으로 판단한다.
- 영향을 받은 각 문서의 `query_schema.user_document_view.image_count`는 `del_yn='N'`인 현재 `image_document` 관계 수를 COUNT하여 갱신한다.
- 이미지에서 분석된 일정과의 `library_schema.image_schedule` 관계는 `del_yn='Y'`로 변경한다. 일정은 이미지와 별개의 사용자 데이터로 유지하므로 `schedule_schema.schedule`, `library_schema.user_schedule`, `query_schema.user_schedule_view`는 변경하지 않는다.
- `query_schema.user_category_view.image_count`와 `latest_uploaded_at`은 남은 활성 사용자 이미지 기준으로 다시 계산한다. 활성 이미지가 0개가 되어도 카테고리와 사용자 카테고리 조회 행은 삭제하지 않으며 `image_count=0`, `latest_uploaded_at=null` 상태로 유지한다.
- 다른 사용자의 사용자-이미지 관계와 조회 행 및 다른 사용자가 소유한 문서는 변경하지 않는다.
- `image_schema.image_asset`, `image_schema.thumbnail`, `image_schema.ocr`, `taxonomy_schema.category`, `taxonomy_schema.tag`, `library_schema.image_category`, `library_schema.image_tag`는 변경하지 않는다.
- 동일 이미지에 `del_yn='N'`인 다른 사용자의 관계가 하나라도 남아 있으면 MinIO/S3 원본과 썸네일 객체를 유지한다.
- 활성 사용자 관계가 하나도 없어진 스토리지 객체는 별도 정리 정책에 따라 물리 삭제할 수 있으며, 이미지 객체 DB 행은 유지한다.
- 삭제한 사용자가 이미지를 재등록하면 4-1의 실제 스토리지 객체 존재 여부 검사에 따라, 객체가 있으면 관계만 복구하고 객체가 없으면 Presigned URL을 발급해 업로드부터 다시 진행한다.
- 동일 사용자가 이미 삭제한 이미지를 다시 요청하면 `alreadyDeletedImageIds`로 반환한다.

---

## 5-4 즐겨찾기 설정/해제

| 항목 | 내용 |
| --- | --- |
| API | `PUT /api/v1/images/{imageId}/favorite` |
| 기능 | MNG-001 |
| 설명 | 이미지의 즐겨찾기 여부를 변경하고 변경 직후의 전체 즐겨찾기 개수를 반환한다. |

### Request

```json
{ "favorite": true }
```

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "I208",
  "message": "요청이 성공했습니다.",
  "data": {
    "imageId": 1024,
    "favorite": true,
    "favoriteCount": 120
  },
  "timestamp": "2026-07-16T06:00:00.000Z"
}
```

- 즐겨찾기 화면의 상단 개수는 `favoriteCount`로 즉시 갱신한다.
- 해제된 항목을 현재 목록에서 즉시 제거할지, 다음 조회 때 제거할지는 클라이언트 표시 정책으로 처리한다.

---

## 5-5 자연어 기반 AI 이미지 검색

| 항목 | 내용 |
| --- | --- |
| API | `POST /api/v1/images/search/semantic` |
| 인증 | Bearer |
| 설명 | 클라이언트가 입력한 문장 또는 단어를 Redis 이벤트로 Analysis Worker에 전달하고, Worker가 반환한 이미지 유사도 검색 결과의 `imageId`를 기준으로 5-1과 동일한 이미지 목록 응답을 반환한다. |

### Request

```json
{
  "query": "C++ 프로그래밍 책 가격을 보여줘",
  "page": 0,
  "size": 20
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| query | String | 필수 | 자연어 문장 또는 검색 단어. 앞뒤 공백 제거 후 빈 문자열은 허용하지 않는다. |
| page | Number | 선택 | 페이지 번호. 기본값 `0` |
| size | Number | 선택 | 페이지 크기. 기본값 `20`, 최대 `100` |

### API 서버 → Analysis Worker 이벤트

```json
{
  "eventType": "IMAGE_SEMANTIC_SEARCH_REQUESTED",
  "version": 1,
  "payload": {
    "correlationId": "0d2647bb-01b4-49b6-a8d4-d6b283ac9a7e",
    "userId": 1,
    "query": "C++ 프로그래밍 책 가격을 보여줘",
    "page": 0,
    "size": 20
  }
}
```

### Analysis Worker → API 서버 검색 결과 이벤트

```json
{
  "eventType": "IMAGE_SEARCH_COMPLETED",
  "version": 1,
  "payload": {
    "correlationId": "0d2647bb-01b4-49b6-a8d4-d6b283ac9a7e",
    "total": 2,
    "page": 0,
    "size": 20,
    "hits": [
      { "imageId": 101, "score": 3.9987202 },
      { "imageId": 102, "score": 1.8765116 }
    ]
  }
}
```

### Response `data`

- 응답 envelope, 페이지 정보 및 `list[]` 항목은 5-1 이미지 목록 조회와 동일하다.
- API 서버는 Worker의 `hits[].imageId` 순서를 유지하여 로그인 사용자의 활성 `user_image`와 `user_image_view`를 조회하고 5-1 DTO로 변환한다.
- `score`는 내부 정렬에만 사용하며 클라이언트 응답에는 포함하지 않는다.

```json
{
  "result": "SUCCESS",
  "code": "I209",
  "message": "요청이 성공했습니다.",
  "data": {
    "list": [
      {
        "imageId": 101,
        "title": "C++ 프로그래밍 입문",
        "summary": "C++ 입문서 정보",
        "favorite": false,
        "thumbnailUrl": "https://.../thumb.jpg",
        "tags": ["도서", "프로그래밍"],
        "category": "공부",
        "uploadedAt": "2026-07-16T06:00:00.000Z"
      }
    ],
    "page": 0,
    "size": 20,
    "totalElements": 2,
    "totalPages": 1,
    "hasNext": false,
    "hasPrevious": false
  },
  "timestamp": "2026-07-29T14:00:00.000Z"
}
```

### 처리 규칙

1. API 서버는 `correlationId`를 생성하고 사용자 ID와 검색 문자열을 Redis Streams 이벤트로 발행한다.
2. Analysis Worker는 해당 사용자가 조회할 수 있는 이미지 범위에서 검색하고 `total`, `page`, `size`, `hits`를 결과 이벤트로 반환한다.
3. API 서버는 같은 `correlationId`의 결과를 기다린 뒤 `hits[].imageId`에 해당하는 삭제되지 않은 사용자 이미지 조회 행을 가져온다.
4. Worker가 반환한 순서를 유지해 5-1 DTO로 변환하며, 접근할 수 없거나 soft delete된 이미지 ID는 응답에서 제외한다.
5. 지정된 대기 시간 안에 결과가 오지 않으면 `AI_SEARCH_TIMEOUT`으로 처리한다.

### 에러

- `INVALID_PARAMETER` (400)
- `AI_SEARCH_FAILED` (500)
- `AI_SEARCH_TIMEOUT` (504)

---

## 5-6 이미지 상세 기반 관련 자료 검색

| 항목 | 내용 |
| --- | --- |
| API | `POST /api/v1/images/{imageId}/related` |
| 인증 | Bearer |
| 설명 | 클라이언트가 전달한 `imageId`를 Analysis Worker에 보내 관련 이미지를 검색하고, 반환된 `imageId`를 기준으로 5-1과 동일한 이미지 목록 응답 DTO를 반환한다. |

### Path Parameter

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| imageId | Integer | 필수 | 관련 자료 검색의 기준이 되는 본인 소유 이미지 ID |

### Request Body

없음

### API 서버 → Analysis Worker 이벤트

```json
{
  "eventType": "RELATED_IMAGE_SEARCH_REQUESTED",
  "version": 1,
  "payload": {
    "correlationId": "2207708f-2f55-42b5-940c-10aeea0ea239",
    "userId": 1,
    "imageId": 1024,
    "limit": 10
  }
}
```

- Worker에는 이미지 관련 정보로 `imageId`만 전달한다. 이미지 상세 정보와 표시용 URL은 이벤트에 포함하지 않는다.

### Analysis Worker → API 서버 검색 결과 이벤트

Worker는 5-5와 동일한 `IMAGE_SEARCH_COMPLETED` 이벤트를 반환한다.

```json
{
  "eventType": "IMAGE_SEARCH_COMPLETED",
  "version": 1,
  "payload": {
    "correlationId": "2207708f-2f55-42b5-940c-10aeea0ea239",
    "total": 2,
    "page": 0,
    "size": 10,
    "hits": [
      { "imageId": 101, "score": 3.9987202 },
      { "imageId": 102, "score": 1.8765116 }
    ]
  }
}
```

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "I210",
  "message": "요청이 성공했습니다.",
  "data": {
    "list": [
      {
        "imageId": 101,
        "title": "C++ 프로그래밍 입문",
        "summary": "C++ 입문서 정보",
        "favorite": false,
        "thumbnailUrl": "https://.../thumb.jpg",
        "tags": ["도서", "프로그래밍"],
        "category": "공부",
        "uploadedAt": "2026-07-16T06:00:00.000Z"
      }
    ],
    "page": 0,
    "size": 10,
    "totalElements": 1,
    "totalPages": 1,
    "hasNext": false,
    "hasPrevious": false
  },
  "timestamp": "2026-07-29T14:00:00.000Z"
}
```

- 응답 envelope, 페이지 정보 및 `list[]` 항목은 5-1 이미지 목록 조회와 동일한 DTO를 사용한다.
- 클라이언트 페이지 요청은 받지 않고 `page=0`, `size=10`으로 고정한다.
- Worker의 `hits[].imageId` 순서를 유지하여 5-1 DTO로 변환하고, 기준 이미지 자체와 접근 불가능하거나 soft delete된 이미지는 제외한다.
- `totalElements`는 필터링을 마친 최종 `list` 개수로 계산한다.

### 처리 규칙

1. API 서버는 5-2와 동일하게 이미지 소유권, soft delete 여부, 분석 완료 상태를 검증한다.
2. API 서버는 `correlationId`, 사용자 ID, 기준 `imageId`, 최대 결과 수를 포함한 Redis Streams 이벤트를 발행한다.
3. Worker는 최대 10개의 `total`, `hits`를 반환하고, API 서버는 해당 `imageId`를 로그인 사용자의 활성 이미지로 다시 검증한다.
4. 기준 이미지 자체를 제외하고 유효한 결과를 Worker 순서대로 5-1 DTO로 변환한 뒤 고정 페이지 메타데이터를 채워 반환한다.
5. 지정된 대기 시간 안에 결과가 오지 않으면 `AI_SEARCH_TIMEOUT`으로 처리한다.

### 에러

- `IMAGE_NOT_FOUND` (404)
- `IMAGE_ANALYSIS_NOT_COMPLETED` (409)
- `AI_SEARCH_FAILED` (500)
- `AI_SEARCH_TIMEOUT` (504)

---

## 5-7 다중 이미지 문서화 기반 관련 자료 검색

| 항목 | 내용 |
| --- | --- |
| API | `POST /api/v1/images/documentize` |
| 인증 | Bearer |
| 설명 | 클라이언트가 전달한 여러 `imageIds`를 Analysis Worker에 보내 문서화에 사용할 관련 이미지를 검색하고, 반환된 `imageId`를 기준으로 5-1과 동일한 이미지 목록 응답 DTO를 반환한다. |

### Request Body

```json
{
  "imageIds": [1024, 1025, 1026]
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| imageIds | Integer[] | 필수 | 문서화 관련 자료 검색의 기준이 되는 본인 소유 이미지 ID 목록. 빈 배열과 중복 ID는 허용하지 않는다. |

### API 서버 → Analysis Worker 이벤트

```json
{
  "eventType": "DOCUMENT_RELATED_IMAGE_SEARCH_REQUESTED",
  "version": 1,
  "payload": {
    "correlationId": "8aeec696-b173-4e91-9cba-7d179fc9da38",
    "userId": 1,
    "imageIds": [1024, 1025, 1026],
    "limit": 10
  }
}
```

- Worker에는 이미지 관련 정보로 `imageIds`만 전달한다. 이미지 상세 정보와 표시용 URL은 이벤트에 포함하지 않는다.

### Analysis Worker → API 서버 검색 결과 이벤트

Worker는 5-5와 동일한 `IMAGE_SEARCH_COMPLETED` 이벤트를 반환한다.

```json
{
  "eventType": "IMAGE_SEARCH_COMPLETED",
  "version": 1,
  "payload": {
    "correlationId": "8aeec696-b173-4e91-9cba-7d179fc9da38",
    "total": 2,
    "page": 0,
    "size": 10,
    "hits": [
      { "imageId": 101, "score": 3.9987202 },
      { "imageId": 102, "score": 1.8765116 }
    ]
  }
}
```

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "I211",
  "message": "요청이 성공했습니다.",
  "data": {
    "list": [
      {
        "imageId": 101,
        "title": "C++ 프로그래밍 입문",
        "summary": "C++ 입문서 정보",
        "favorite": false,
        "thumbnailUrl": "https://.../thumb.jpg",
        "tags": ["도서", "프로그래밍"],
        "category": "공부",
        "uploadedAt": "2026-07-16T06:00:00.000Z"
      }
    ],
    "page": 0,
    "size": 10,
    "totalElements": 1,
    "totalPages": 1,
    "hasNext": false,
    "hasPrevious": false
  },
  "timestamp": "2026-07-29T14:00:00.000Z"
}
```

- 응답 envelope, 페이지 정보 및 `list[]` 항목은 5-1 이미지 목록 조회와 동일한 DTO를 사용한다.
- 클라이언트 페이지 요청은 받지 않고 `page=0`, `size=10`으로 고정한다.
- Worker의 `hits[].imageId` 순서를 유지하여 5-1 DTO로 변환하고, 요청의 `imageIds` 전체와 접근 불가능하거나 soft delete된 이미지는 제외한다.
- `totalElements`는 필터링을 마친 최종 `list` 개수로 계산한다.

### 처리 규칙

1. `imageIds`가 비어 있지 않고 중복이 없는지 검증한다.
2. API 서버는 모든 기준 이미지에 5-2와 동일한 소유권, soft delete 여부, 분석 완료 상태 검증을 적용한다.
3. API 서버는 `correlationId`, 사용자 ID, 기준 `imageIds`, 최대 결과 수를 포함한 Redis Streams 이벤트를 발행한다.
4. Worker는 최대 10개의 `total`, `hits`를 반환하고, API 서버는 해당 `imageId`를 로그인 사용자의 활성 이미지로 다시 검증한다.
5. 요청의 기준 이미지 전체를 결과에서 제외한 뒤 유효한 결과를 Worker 순서대로 5-1 DTO로 변환하고 고정 페이지 메타데이터를 채워 반환한다.
6. 지정된 대기 시간 안에 결과가 오지 않으면 `AI_SEARCH_TIMEOUT`으로 처리한다.

### 에러

- `INVALID_PARAMETER` (400)
- `IMAGE_NOT_FOUND` (404)
- `IMAGE_ANALYSIS_NOT_COMPLETED` (409)
- `AI_SEARCH_FAILED` (500)
- `AI_SEARCH_TIMEOUT` (504)

---

# 6. 태그·카테고리·홈 API

## 6-1 카테고리 목록

| 항목 | 내용 |
| --- | --- |
| API | `GET /api/v1/categories` |
| 기능 | HOM-002, 카테고리 메인 |
| 설명 | 카테고리 이름·이미지 수·최신 업로드 날짜, 이미지를 포함한 카테고리 카드를 전체 목록으로 조회한다(페이지네이션 없음). |

### Query Parameters

| 파라미터 | 설명 |
| --- | --- |
| sort | `NAME_ASC`(이름순, 기본) / `UPDATED_DESC`(최신 업로드순) / `IMAGE_COUNT_DESC`(사진 많은 순). 대소문자 무관, 그 외 값은 `INVALID_PARAMETER`(400) |

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "T202",
  "message": "요청이 성공했습니다.",
  "data": {
    "list": [
      {
        "categoryId": 3,
        "name": "공부",
        "categoryImageUrl" : "http://asdasd", 
        "imageCount": 42,
        "latestUpdatedAt": "2026-07-17T06:00:00.000Z"
      },
      {
        "categoryId": 4,
        "name": "음식",
        "categoryImageUrl" : "http://asdasd",
        "imageCount": 52,
        "latestUpdatedAt": "2026-07-18T06:00:00.000Z"
      }
    ]
  },
  "timestamp": "2026-07-16T06:00:00.000Z"
}
```

---

# 7. 사용자·설정·알림 API

## 7-1 내 정보 조회

| 항목 | 내용 |
| --- | --- |
| API | `GET /api/v1/user` |

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "U201",
  "message": "요청이 성공했습니다.",
  "data": {
    "userId": 1,
    "loginId": "newUser123",
    "email": "user@example.com",
    "notification" : true,
    "backgroundAnalysis" : true
  },
  "timestamp": "2026-07-16T06:00:00.000Z"
}
```

---

## 7-2 저장 데이터 초기화

| 항목 | 내용 |
| --- | --- |
| API | `DELETE /api/v1/user/delete` |
| 기능 | SET-004 |
| 설명 | 사용자 계정은 유지하고, 해당 사용자 ID가 연결된 모든 관계 데이터, 사용자별 조회 데이터와 멱등성 요청 이력을 soft delete한다. 객체 스키마의 원본 데이터와 MinIO/S3 객체는 직접 삭제하지 않는다. 실수 방지를 위해 확인 문자열을 요구한다. |

### Request Body

```json
{ "confirm": "DELETE" }
```

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "U202",
  "data" : {},
  "message": "요청이 성공했습니다.",
  "timestamp": "2026-07-16T06:00:00.000Z"
}
```

### 처리 규칙

- `user_schema.users` 계정 행은 유지한다.
- `library_schema`에서 해당 사용자 ID가 연결된 이미지·문서·일정·즐겨찾기 등 모든 관계 행의 `del_yn`을 `Y`로 변경한다.
- `query_schema`의 해당 사용자 조회 행도 `del_yn='Y'`로 동기화하여 이후 목록·상세 조회에서 제외한다.
- `image_schema.image_registration_request`와 `document_schema.document_generation_request`에서 해당 사용자의 멱등성 요청 이력도 `del_yn='Y'`로 변경한다.
- `image_schema`, `document_schema`, `schedule_schema`, `taxonomy_schema`의 객체 원본 행은 변경하지 않는다. 단, 위 두 요청 이력 테이블은 원본 객체가 아닌 사용자별 처리 이력이므로 soft delete 대상에 포함한다.
- MinIO/S3 원본과 썸네일 객체는 이 API에서 삭제하지 않으며, 다른 사용자의 관계와 조회 데이터에도 영향을 주지 않는다.
- 처리 중 하나라도 실패하면 전체 soft delete 트랜잭션을 롤백한다.

### 에러: `CONFIRM_REQUIRED`(400)

---

## 7-3 사용자 설정 변경

| 항목 | 내용 |
| --- | --- |
| API | `PATCH /api/v1/user/settings` |
| 기능 | SET-001, SET-002, SET-003 |
| 설명 | 전달한 설정 필드만 변경한다. |

### Request (모든 필드 선택)

```json
{
  "notification" : true,
  "backgroundAnalysis" : true
}
```

- `notification`은 성공·실패 알림을 구분하지 않는 전체 알림 설정이며 `user_schema.user_setting.notification_enabled`에 저장한다.
- `backgroundAnalysis`는 `user_schema.user_setting.server_analysis_enabled`에 저장한다.

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "U204",
  "message": "요청이 성공했습니다.",
  "data": {
    "notification" : true,
	  "backgroundAnalysis" : true
  },
  "timestamp": "2026-07-16T06:00:00.000Z"
}
```

---

## 7-4 FCM 기기 토큰 등록·갱신

| 항목 | 내용 |
| --- | --- |
| API | `PUT /api/v1/user/devices/{deviceId}` |
| 기능 | SET-003, 분석 완료 알림 |
| 설명 | 로그인한 사용자의 기기와 FCM 토큰을 등록하거나 갱신한다. 동일 deviceId 요청은 멱등 처리한다. |

### Request

```json
{
  "platform": "ANDROID",
  "fcmToken": "fcm-registration-token",
  "appVersion": "1.0.0"
}
```

### Response `data`

```json
{
  "deviceId": "android-device-uuid",
  "registered": true,
  "updatedAt": "2026-07-16T06:00:00.000Z"
}
```

### 에러: `INVALID_FCM_TOKEN`(400)

---

## 7-5 FCM 기기 토큰 해제

| 항목 | 내용 |
| --- | --- |
| API | `DELETE /api/v1/user/devices/{deviceId}` |
| 기능 | SET-003 |
| 설명 | 로그아웃, 앱 삭제 토큰 정리 또는 해당 기기 알림 해제 시 서버의 기기 토큰을 비활성화한다. |

### Response `data`

```json
{
  "deviceId": "android-device-uuid",
  "deleted": true
}
```

---

## 7-6 개인정보 처리 안내 조회

| 항목 | 내용 |
| --- | --- |
| API | `GET /api/v1/policies/privacy` |
| 인증 | 불필요 |
| 기능 | SET-005 |
| 설명 | 앱 설정 화면에서 표시할 현재 개인정보 처리 안내의 버전과 본문 URL을 조회한다. 안내를 앱에 정적으로 포함한다면 이 API는 생략할 수 있다. |

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "U205",
  "message": "요청이 성공했습니다.",
  "data": {
    "version": "2026-07-16",
    "title": "개인정보 처리방침",
    "contentUrl": "https://service.example.com/policies/privacy/2026-07-16",
    "effectiveAt": "2026-07-16T00:00:00.000Z"
  },
  "timestamp": "2026-07-16T06:00:00.000Z"
}
```

---

## 7-7 이미지 업로드·분석 결과 알림 (FCM)

REST API는 아니지만 API 서버가 클라이언트에 전달하는 푸시 계약이므로 함께 정의한다.

- API 서버는 스토리지 업로드, Worker 분석, 분석 결과와 관계·조회 테이블의 DB 저장이 모두 완료된 뒤 `ANALYSIS_COMPLETED`를 전송한다.
- 완료 알림은 반드시 DB 트랜잭션 커밋 이후 전송한다.
- FCM 메시지 자체에는 최종 분석 데이터를 넣지 않는다. 클라이언트는 알림을 받은 뒤 `GET /api/v1/images/{imageId}`로 최신 데이터를 조회한다.

### 완료 알림 data payload

```json
{
  "result": "SUCCESS",
  "code": "ANALYSIS_COMPLETED",
  "message": "이미지 업로드 및 분석이 완료되었습니다.",
  "data": {
    "imageId": 1024,
    "clientRequestId": "d95db8b7-897e-412c-8924-eef3c7bca039"
  },
  "timestamp": "2026-07-29T06:00:00.000Z"
}
```

### 실패 알림 data payload

```json
{
  "result": "FAIL",
  "code": "ANALYSIS_FAILED",
  "message": "이미지 분석에 실패했습니다.",
  "data": {
    "imageId": 1024,
    "clientRequestId": "...",
    "errorCode": "IMAGE_ANALYSIS_TIMEOUT",
    "retryable": true
  },
  "timestamp": "2026-07-29T06:00:00.000Z"
}
```

### 클라이언트 처리 규칙

1. `ANALYSIS_COMPLETED` 수신 시 `GET /api/v1/images/{imageId}`를 호출해 최종 분석 결과를 갱신한다.
2. `ANALYSIS_FAILED` 수신 시 `errorCode`, `retryable`에 따라 실패 상태와 재시도 가능 여부를 표시한다.
3. FCM data payload의 값은 문자열로 전달한다.
4. FCM은 전달이 보장되지 않으므로 앱이 포그라운드로 진입하거나 새로고침할 때 이미지 목록 또는 상세 정보를 다시 조회한다.

---

# 8. 문서 API

문서는 `document_schema.document`를 원본으로 사용하고, 사용자 소유 관계는 `library_schema.user_document`, 포함 이미지 관계는 `library_schema.image_document`로 관리한다. 조회 응답은 `query_schema.user_document_view`, `query_schema.document_image_view`를 사용한다.

## 8-1 내 문서 목록 조회

| 항목 | 내용 |
| --- | --- |
| API | `GET /api/v1/documents` |
| 인증 | Bearer |
| 설명 | 로그인한 사용자의 삭제되지 않은 문서를 페이지 단위로 조회한다. |

### Query Parameters

| 파라미터 | 타입 | 설명 |
| --- | --- | --- |
| page | Number | 페이지 번호. 기본값 `0` |
| size | Number | 페이지 크기. 기본값 `20`, 최대 `100` |
| sort | String | `UPDATED_DESC`, `UPDATED_ASC` , `NAME_ASC`. 기본값 `UPDATED_ASC` |

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "D201",
  "message": "요청이 성공했습니다.",
  "data": {
    "list": [
      {
        "documentId": 101,
        "name": "성심당 케이크 리스트",
        "imageCount": 4,
        "delImageCount": 0,
        "updatedAt": "2026-07-29T02:00:00.000Z"
      }
    ],
    "page": 0,
    "size": 20,
    "totalElements": 1,
    "totalPages": 1,
    "hasNext": false,
    "hasPrevious": false
  },
  "timestamp": "2026-07-29T03:00:00.000Z"
}
```

- `delImageCount`가 `0`이 아니면 문서 생성 이후 원본 이미지가 삭제되어 갱신이 필요한 문서다.

---

## 8-2 문서 생성

| 항목 | 내용 |
| --- | --- |
| API | `POST /api/v1/documents` |
| 인증 | Bearer |
| 처리 방식 | 비동기 |
| 성공 상태 | `202 Accepted` |
| 설명 | 선택한 이미지의 분석 정보를 기반으로 AI 문서 생성을 요청한다. 실제 문서 생성과 DB 저장은 비동기로 처리한다. |

### Request

```json
{
  "clientRequestId": "d95db8b7-897e-412c-8924-eef3c7bca039",
  "imageIds": [1024, 1025, 1026, 1027]
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| clientRequestId | String(UUID) | 필수 | 요청 추적 및 중복 처리 방지를 위한 클라이언트 요청 ID |
| imageIds | Number[] | 필수 | 문서 생성에 사용할 본인 소유 이미지 ID. 1개 이상이며 중복 불가 |

### 요청 검증

- 모든 이미지가 요청 사용자의 소유인지 확인한다.
- 삭제된 이미지 또는 분석이 완료되지 않은 이미지는 사용할 수 없다.
- `imageIds`에 중복 값이 있으면 요청을 거부한다.
- 동일 사용자의 동일한 `clientRequestId`가 재요청되면 중복 실행으로 인한 예기치 않은 오류를 방지하기 위해 새 이벤트를 발행하지 않고 `DUPLICATE_CLIENT_REQUEST`(409)로 차단한다.

### 최초 응답 `202 Accepted`

```json
{
  "result": "SUCCESS",
  "code": "DOCUMENT_GENERATION_ACCEPTED",
  "message": "문서 생성 요청이 접수되었습니다.",
  "data": {
    "clientRequestId": "d95db8b7-897e-412c-8924-eef3c7bca039",
    "status": "QUEUED"
  },
  "timestamp": "2026-07-29T01:00:00.000Z"
}
```

### 전체 처리 흐름

1. 클라이언트가 API 서버에 문서 생성을 요청한다.
2. API 서버가 사용자 권한, 이미지 상태 및 요청 중복 여부를 검증한다.
3. API 서버가 Redis Streams에 `DOCUMENT_GENERATION_REQUESTED` 이벤트를 발행하고 클라이언트에 `202 Accepted`를 반환한다.
4. Analysis Worker가 이벤트를 수신한 뒤 AI 서버에 문서 생성을 요청한다.
5. AI 서버가 문서 이름과 Markdown 본문을 생성하여 Analysis Worker에 반환한다.
6. Analysis Worker가 Redis Streams에 `DOCUMENT_GENERATION_COMPLETED` 또는 `DOCUMENT_GENERATION_FAILED` 이벤트를 발행한다.
7. API 서버가 완료 이벤트를 수신하고 문서 원본, 관계 정보, 조회 모델을 하나의 트랜잭션으로 저장한다.
8. DB 트랜잭션 커밋이 완료된 후에만 클라이언트에 FCM 완료 알림을 전송한다.
9. 클라이언트는 알림의 `documentId`로 `GET /api/v1/documents/{documentId}`를 호출해 최종 문서를 조회한다.

### API 서버 → Analysis Worker 이벤트

```json
{
  "eventType": "DOCUMENT_GENERATION_REQUESTED",
  "version": 1,
  "payload": {
    "clientRequestId": "d95db8b7-897e-412c-8924-eef3c7bca039",
    "userId": 1,
    "images": [
      {
        "imageId": 1024,
        "s3Key": "images/1024/original.jpg",
        "title": "교보문고 C++ 프로그래밍 입문",
        "summary": "C++ 입문서 정보",
        "category": "쇼핑",
        "tags": ["도서", "프로그래밍"],
        "keyInformation": ["가격: 32,000원", "판매처: 교보문고"],
        "structuredData": {
          "type": null,
          "fields": {}
        }
      }
    ]
  }
}
```

### Analysis Worker → API 서버 완료 이벤트

```json
{
  "eventType": "DOCUMENT_GENERATION_COMPLETED",
  "version": 1,
  "payload": {
    "clientRequestId": "d95db8b7-897e-412c-8924-eef3c7bca039",
    "userId": 1,
    "imageIds": [1024, 1025, 1026, 1027],
    "result": {
      "name": "C++ 프로그래밍 입문 정리",
      "content": "# C++ 프로그래밍 입문\n\n선택한 이미지 분석 결과를 정리한 문서입니다."
    }
  }
}
```

### Analysis Worker → API 서버 실패 이벤트

```json
{
  "eventType": "DOCUMENT_GENERATION_FAILED",
  "version": 1,
  "payload": {
    "clientRequestId": "d95db8b7-897e-412c-8924-eef3c7bca039",
    "userId": 1,
    "errorCode": "DOCUMENT_GENERATION_FAILED",
    "retryable": true
  }
}
```

### DB 저장 규칙

문서 생성 요청을 접수할 때 `(user_id, client_request_id)` 기준으로 `document_schema.document_generation_request`를 먼저 생성하여 중복 이벤트 발행을 막는다. 요청 종류는 `CREATE`, `ADD_IMAGES`, `EXCLUDE_IMAGES`로 구분하고 상태는 `QUEUED`, `PROCESSING`, `COMPLETED`, `FAILED`로 관리한다.

완료 이벤트를 수신하면 아래 데이터를 하나의 트랜잭션으로 저장한다.

- `document_schema.document_generation_request`: 완료 상태 및 생성된 `result_document_id`
- `document_schema.document`: 문서 원본
- `library_schema.user_document`: 사용자와 문서 관계
- `library_schema.image_document`: 이미지와 문서 관계
- `query_schema.user_document_view`: 사용자별 문서 조회 모델
- `query_schema.document_image_view`: 문서별 이미지 조회 모델
- `query_schema.user_image_view`: 새 문서에 포함된 이미지의 `is_documented_yn='Y'` 동기화

DB 저장 중 하나라도 실패하면 전체 트랜잭션을 롤백하고 성공 알림을 전송하지 않는다.

### DB 커밋 후 FCM 완료 알림

```json
{
  "result": "SUCCESS",
  "code": "DOCUMENT_GENERATION_COMPLETED",
  "message": "문서 생성이 완료되었습니다.",
  "data": {
    "clientRequestId": "d95db8b7-897e-412c-8924-eef3c7bca039",
    "documentId": 101
  },
  "timestamp": "2026-07-29T01:05:00.000Z"
}
```

- 성공 알림은 DB 커밋 이후에 전송한다.
- FCM 전송에 실패해도 저장된 문서 데이터는 롤백하지 않으며 알림만 재시도할 수 있도록 처리한다.

### 에러

- `IMAGE_NOT_FOUND` (404)
- `INVALID_DOCUMENT_IMAGES` (400)
- `DOCUMENT_IMAGE_NOT_OWNED` (403)
- `IMAGE_ANALYSIS_NOT_COMPLETED` (409)
- `DUPLICATE_CLIENT_REQUEST` (409)
- `DOCUMENT_GENERATION_FAILED` (비동기 실패 이벤트 및 알림)

---

## 8-3 문서 상세 조회

| 항목 | 내용 |
| --- | --- |
| API | `GET /api/v1/documents/{documentId}` |
| 인증 | Bearer |
| 설명 | 본인이 소유한 문서의 Markdown 본문과 문서에 포함된 이미지 정보를 조회한다. |

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "D203",
  "message": "요청이 성공했습니다.",
  "data": {
    "documentId": 101,
    "name": "성심당 케이크 리스트",
    "content": "# 성심당 케이크 리스트\n\n선택한 이미지 분석 결과를 정리한 문서입니다.",
    "imageCount": 2,
    "delImageCount": 0,
    "imageIds": [1024, 1025],
    "updatedAt": "2026-07-29T02:00:00.000Z"
  },
  "timestamp": "2026-07-29T03:00:00.000Z"
}
```

- `delImageCount`가 `0`이 아니면 문서 생성 이후 원본 이미지가 삭제되어 갱신이 필요한 문서다.

### 에러: `DOCUMENT_NOT_FOUND` (404)

---

## 8-4 문서 이미지 목록 조회

| 항목 | 내용 |
| --- | --- |
| API | `GET /api/v1/documents/{documentId}/images` |
| 인증 | Bearer |
| 설명 | 문서 ID를 기준으로 본인이 소유한 문서를 구성하는 삭제되지 않은 이미지 목록을 조회한다. 응답 항목은 5-1 이미지 목록 조회의 Response DTO를 그대로 사용한다. |

### Query Parameters

| 파라미터 | 타입 | 설명 |
| --- | --- | --- |
| page | Number | 페이지 번호, 0부터 시작. 기본값 `0` |
| size | Number | 페이지 크기. 기본값 `20`, 최대 `100` |
| sort | String | 정렬 기준. `TITLE_ASC`, `UPLOADED_DESC`, `UPLOADED_ASC` 중 하나. 기본값 `UPLOADED_DESC` |

### Response `data` (5-1과 동일한 페이지 형식 및 list 항목)

```json
{
  "result": "SUCCESS",
  "code": "D204",
  "message": "요청이 성공했습니다.",
  "data": {
    "list": [
      {
        "imageId": 1024,
        "title": "C++ 프로그래밍 입문",
        "summary": "교보문고에서 판매 중인 C++ 프로그래밍 입문서, 32,000원",
        "favorite": false,
        "thumbnailUrl": "https://.../thumb.jpg",
        "tags": ["C++", "쇼핑"],
        "category": "공부",
        "uploadedAt": "2026-07-16T06:00:00.000Z"
      }
    ],
    "page": 0,
    "size": 20,
    "totalElements": 1,
    "totalPages": 1,
    "hasNext": false,
    "hasPrevious": false
  },
  "timestamp": "2026-07-16T06:00:00.000Z"
}
```

### 처리 규칙

- 로그인한 사용자가 소유한 문서만 조회할 수 있다.
- `library_schema.image_document` 관계와 로그인 사용자의 `library_schema.user_image` 관계가 모두 유효한 이미지만 반환한다.
- 문서에 포함된 이미지가 없으면 `list`가 빈 배열인 정상 응답을 반환한다.

### 에러

- `DOCUMENT_NOT_FOUND` (404)
- `INVALID_PARAMETER` (400)

---

## 8-5 문서 삭제

| 항목 | 내용 |
| --- | --- |
| API | `DELETE /api/v1/documents/{documentId}` |
| 인증 | Bearer |
| 설명 | 문서 원본, 사용자·이미지 관계와 문서 조회 모델을 soft-delete한다. 문서에 사용된 원본 이미지는 삭제하지 않는다. |

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "D205",
  "message": "문서가 삭제되었습니다.",
  "data": {
    "documentId": 101,
    "deleted": true
  },
  "timestamp": "2026-07-29T05:00:00.000Z"
}
```

### 처리 규칙

- `document_schema.document`, `library_schema.user_document`, `library_schema.image_document`, `query_schema.user_document_view`, `query_schema.document_image_view`의 대상 문서 행을 하나의 트랜잭션에서 soft delete한다.
- 문서에서 제외된 각 이미지가 다른 활성 문서에도 포함되어 있는지 확인하고, 더 이상 포함된 활성 문서가 없으면 해당 사용자의 `query_schema.user_image_view.is_documented_yn='N'`으로 변경한다.
- 이미지 원본과 `library_schema.user_image` 관계는 변경하지 않는다.

### 에러: `DOCUMENT_NOT_FOUND` (404)

---

## 8-6 이미지 추가 후 새 문서 생성

| 항목 | 내용 |
| --- | --- |
| API | `POST /api/v1/documents/{documentId}/images` |
| 인증 | Bearer |
| 처리 방식 | 비동기 |
| 성공 상태 | `202 Accepted` |
| 설명 | 기존 문서의 이미지 목록에 이미지를 추가한 결과로 새 문서 생성을 요청한다. 새 문서 저장이 완료되면 기존 문서는 soft delete한다. |

### Request

```json
{
  "clientRequestId": "9e41d582-471f-47b1-b50b-d56d2a97ed92",
  "imageIds": [1028, 1029]
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| clientRequestId | String(UUID) | 필수 | 요청 추적 및 중복 처리 방지를 위한 요청 ID |
| imageIds | Number[] | 필수 | 기존 문서의 이미지 목록에 추가할 본인 소유 이미지 ID |

### Response `202 Accepted`

```json
{
  "result": "SUCCESS",
  "code": "DOCUMENT_GENERATION_ACCEPTED",
  "message": "이미지 추가를 반영한 새 문서 생성 요청이 접수되었습니다.",
  "data": {
    "clientRequestId": "9e41d582-471f-47b1-b50b-d56d2a97ed92",
    "sourceDocumentId": 101,
    "status": "QUEUED"
  },
  "timestamp": "2026-07-29T06:00:00.000Z"
}
```

### 처리 규칙

1. 기존 문서의 이미지 ID 목록을 조회한다.
2. 요청의 `imageIds`를 합치고 중복 ID를 제거하여 최종 이미지 ID 목록을 만든다.
3. 최종 이미지 ID 목록에 8-2와 동일한 소유권·삭제 여부·분석 완료 검증을 적용한다.
4. 8-2 문서 생성과 동일한 애플리케이션 서비스를 호출하여 `DOCUMENT_GENERATION_REQUESTED` 이벤트를 발행한다. API 서버 내부에서 8-2 HTTP API를 다시 호출하지 않는다.
5. Analysis Worker와 AI 서버의 처리가 끝나면 새 문서와 새로운 관계·조회 모델을 저장한다.
6. 새 문서 저장이 모두 성공한 같은 트랜잭션에서 기존 `document`, `user_document`, `image_document`, `user_document_view`, `document_image_view`를 soft delete한다. 원본 이미지는 변경하지 않는다.
7. 새 문서에 포함된 이미지의 `user_image_view.is_documented_yn`을 갱신하고, 기존 문서에만 포함됐던 이미지는 다른 활성 문서 포함 여부를 확인해 값을 다시 계산한다.
8. 새 문서의 DB 트랜잭션 커밋 후 8-2와 동일한 FCM 완료 알림을 전송한다.

### 에러

- `DOCUMENT_NOT_FOUND` (404)
- `IMAGE_NOT_FOUND` (404)
- `DOCUMENT_IMAGE_NOT_OWNED` (403)
- `IMAGE_ANALYSIS_NOT_COMPLETED` (409)
- `DUPLICATE_CLIENT_REQUEST` (409)

---

## 8-7 이미지 제외 후 새 문서 생성

| 항목 | 내용 |
| --- | --- |
| API | `POST /api/v1/documents/{documentId}/images/exclude` |
| 인증 | Bearer |
| 처리 방식 | 비동기 |
| 성공 상태 | `202 Accepted` |
| 설명 | 기존 문서의 이미지 목록에서 선택한 이미지를 제외한 결과로 새 문서 생성을 요청한다. 새 문서 저장이 완료되면 기존 문서는 soft delete하며 원본 이미지는 변경하지 않는다. |

### Request

```json
{
  "clientRequestId": "472b64ba-b6ef-4af3-a31c-73064a7ea8bc",
  "imageIds": [1024, 1025]
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| clientRequestId | String(UUID) | 필수 | 요청 추적 및 중복 처리 방지를 위한 요청 ID |
| imageIds | Number[] | 필수 | 새 문서 생성 시 기존 문서의 이미지 목록에서 제외할 이미지 ID |

### Response `202 Accepted`

```json
{
  "result": "SUCCESS",
  "code": "DOCUMENT_GENERATION_ACCEPTED",
  "message": "이미지 제외를 반영한 새 문서 생성 요청이 접수되었습니다.",
  "data": {
    "clientRequestId": "472b64ba-b6ef-4af3-a31c-73064a7ea8bc",
    "sourceDocumentId": 101,
    "status": "QUEUED"
  },
  "timestamp": "2026-07-29T07:00:00.000Z"
}
```

### 처리 규칙

1. 기존 문서의 이미지 ID 목록을 조회한다.
2. 요청의 `imageIds`를 제외하여 최종 이미지 ID 목록을 만든다.
3. 요청한 모든 이미지가 기존 문서에 포함되어 있는지 확인하고, 최종 이미지 ID 목록이 1개 이상인지 검증한다.
4. 최종 이미지 ID 목록에 8-2와 동일한 소유권·삭제 여부·분석 완료 검증을 적용한다.
5. 8-2 문서 생성과 동일한 애플리케이션 서비스를 호출하여 `DOCUMENT_GENERATION_REQUESTED` 이벤트를 발행한다. API 서버 내부에서 8-2 HTTP API를 다시 호출하지 않는다.
6. Analysis Worker와 AI 서버의 처리가 끝나면 새 문서와 새로운 관계·조회 모델을 저장한다.
7. 새 문서 저장이 모두 성공한 같은 트랜잭션에서 기존 `document`, `user_document`, `image_document`, `user_document_view`, `document_image_view`를 soft delete한다. 원본 이미지는 변경하지 않는다.
8. 새 문서에 포함된 이미지의 `user_image_view.is_documented_yn`을 갱신하고, 기존 문서에서 제외된 이미지는 다른 활성 문서 포함 여부를 확인해 값을 다시 계산한다.
9. 새 문서의 DB 트랜잭션 커밋 후 8-2와 동일한 FCM 완료 알림을 전송한다.

### 에러

- `DOCUMENT_NOT_FOUND` (404)
- `DOCUMENT_IMAGE_NOT_FOUND` (404)
- `INVALID_DOCUMENT_IMAGES` (400)
- `DUPLICATE_CLIENT_REQUEST` (409)

---

# 9. 일정 API

일정은 이미지 분석 결과로 생성되며 별도 생성 API를 제공하지 않는다. `schedule_schema.schedule`이 일정 원본을 소유하고, 사용자 캘린더 등록 상태는 `library_schema.user_schedule.is_calendared_yn`으로 관리한다. 조회 응답은 `query_schema.user_schedule_view`를 사용한다.

캘린더는 별도 저장소 없이 `is_calendared_yn` 상태로만 표현한다. 분석에서 생성된 일정은 미등록 후보(`'N'`)로 시작하며, 9-3 호출이 캘린더 등록/해제의 전부다. 캘린더 화면은 9-1의 `calendared=true` 조회를 사용한다. 기기 캘린더(구글 캘린더 등) 연동은 앱 책임 영역이며 서버는 이 플래그만 관리한다.

## 9-1 내 일정 후보·캘린더 일정 목록 조회

| 항목 | 내용 |
| --- | --- |
| API | `GET /api/v1/schedules` |
| 인증 | Bearer |
| 설명 | 분석에서 추출된 일정 후보와 캘린더에 등록한 일정을 조회한다. |

### Query Parameters

| 파라미터 | 타입 | 설명 |
| --- | --- | --- |
| calendared | Boolean | `true`는 캘린더 등록 일정, `false`는 미등록 후보. 미지정 시 전체 |
| from | String | 조회 시작 시각. ISO-8601 UTC, 선택 |
| to | String | 조회 종료 시각. ISO-8601 UTC, 선택 |
| page | Number | 페이지 번호. 기본값 `0` |
| size | Number | 페이지 크기. 기본값 `20`, 최대 `100` |
| sort | String | `START_ASC`, `START_DESC`, 기본값 `START_ASC` |

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "S201",
  "message": "요청이 성공했습니다.",
  "data": {
    "list": [
      {
        "scheduleId": 301,
        "imageId": 1024,
        "title": "팀 프로젝트 회의",
        "startAt": "2026-08-03T05:30:00.000Z",
        "endAt": "2026-08-03T07:00:00.000Z",
        "calendared": false,
        "updatedAt": "2026-07-29T01:00:00.000Z"
      }
    ],
    "page": 0,
    "size": 20,
    "totalElements": 1,
    "totalPages": 1,
    "hasNext": false,
    "hasPrevious": false
  },
  "timestamp": "2026-07-29T03:00:00.000Z"
}
```

- `startAt` 또는 `endAt`을 분석에서 추출하지 못한 경우 해당 값은 `null`이다.
- 출처 이미지가 삭제된 뒤에도 일정은 유지되므로(5-3 참고) `imageId`가 삭제된 이미지를 가리킬 수 있다. 이 `imageId`로 5-2를 조회하면 `IMAGE_NOT_FOUND`(404)가 될 수 있다.

### 조회·정렬 규칙

- `query_schema.user_schedule_view.del_yn='N'`인 행만 조회한다.
- `from`/`to`는 일정 기간 `[startAt, endAt]`과의 **겹침(경계 포함)** 기준이다(`COALESCE(endAt, startAt) >= from AND COALESCE(startAt, endAt) <= to`). 한쪽 시각만 있는 일정은 있는 시각을 기간으로 본다.
- `startAt`과 `endAt`이 모두 `null`인 일정(날짜 추출 실패 후보)은 `from` 또는 `to`를 지정하면 결과에서 제외된다. 날짜 없는 후보는 기간 없이 조회해야 한다.
- 정렬 시 시각이 `null`인 일정은 항상 마지막에 오고(NULLS LAST), 같은 정렬값은 `scheduleId ASC`를 보조 정렬로 사용해 페이지 간 순서를 고정한다.
- 지원하지 않는 `sort` 값, 범위를 벗어난 `page`/`size`, 형식이 잘못된 `from`/`to`, `from`이 `to`보다 늦은 요청은 `INVALID_PARAMETER`(400)로 응답한다.

---

## 9-2 일정 삭제

| 항목 | 내용 |
| --- | --- |
| API | `DELETE /api/v1/schedules/{scheduleId}` |
| 인증 | Bearer |
| 설명 | 일정 객체, 사용자 관계, 이미지-일정 관계와 조회 모델을 soft-delete한다. 출처 이미지는 삭제하지 않는다. |

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "S202",
  "message": "일정이 삭제되었습니다.",
  "data": {
    "scheduleId": 301,
    "deleted": true
  },
  "timestamp": "2026-07-29T05:00:00.000Z"
}
```

### 처리 규칙

- `schedule_schema.schedule`, `library_schema.user_schedule`, `library_schema.image_schedule`, `query_schema.user_schedule_view`를 하나의 트랜잭션에서 soft delete한다.
- 출처 이미지와 `library_schema.user_image` 관계는 변경하지 않는다.
- 해당 이미지에 캘린더 등록 상태인 다른 활성 일정이 없으면 `query_schema.user_image_view.is_calendared_yn='N'`으로 갱신한다. 활성 일정은 `image_schedule.del_yn='N'`이고 `user_schedule.del_yn='N'`인 일정이다. 출처 이미지 관계가 이미 삭제된 일정(5-3 이미지 삭제 이후)은 이 갱신을 생략한다.

### 에러: `SCHEDULE_NOT_FOUND` (404)

- 일정이 없거나, 요청 사용자 소유가 아니거나, 이미 삭제된 경우 모두 `SCHEDULE_NOT_FOUND`다.

---

## 9-3 캘린더 등록 상태 변경

| 항목 | 내용 |
| --- | --- |
| API | `PUT /api/v1/schedules/{scheduleId}/calendar` |
| 인증 | Bearer |
| 설명 | 분석에서 추출된 일정의 사용자 캘린더 등록 여부를 변경한다. |

### Request

```json
{
  "calendared": true
}
```

### Response `data`

```json
{
  "result": "SUCCESS",
  "code": "S203",
  "message": "캘린더 등록 상태가 변경되었습니다.",
  "data": {
    "scheduleId": 301,
    "calendared": true,
    "updatedAt": "2026-07-29T06:00:00.000Z"
  },
  "timestamp": "2026-07-29T06:00:00.000Z"
}
```

### 처리 규칙

- `library_schema.user_schedule.is_calendared_yn`과 `query_schema.user_schedule_view.is_calendared_yn`을 같은 트랜잭션에서 변경한다.
- 출처 이미지에 연결된 활성 일정 중 하나라도 캘린더 등록 상태이면 `query_schema.user_image_view.is_calendared_yn='Y'`, 하나도 없으면 `N`으로 갱신한다. 활성 일정은 `image_schedule.del_yn='N'`이고 `user_schedule.del_yn='N'`인 일정이다. 출처 이미지 관계가 이미 삭제된 일정은 이 갱신을 생략한다.
- 같은 값으로 다시 호출해도 성공이며(멱등) `updatedAt`만 갱신된다.

### 에러: `SCHEDULE_NOT_FOUND` (404)

- 일정이 없거나, 요청 사용자 소유가 아니거나, 이미 삭제된 경우 모두 `SCHEDULE_NOT_FOUND`다.

---

# 10. API 서버 내부 통신 범위

- API 서버와 analysis-worker의 분석 요청·결과 전달은 HTTP 내부 API가 아니라 Redis Streams 이벤트 버스를 사용한다.
- analysis-worker가 FastAPI와 통신하는 HTTP API는 파트너 명세서에서 별도로 관리한다.
- 따라서 이 프론트엔드용 명세서에는 기존 10-1~10-5 내부 HTTP API를 두지 않는다.

---

# 11. 에러 코드 총람

공통 에러 코드(`INVALID_PARAMETER`, `UNAUTHORIZED`, `FORBIDDEN`, `NOT_FOUND`, `CONFLICT`, `INTERNAL_ERROR`)는 1.3을 따른다. 아래 표에는 현재 활성 API와 비동기 알림 계약에서 추가로 사용하는 도메인 에러 코드만 정리한다.

| code | HTTP/전달 방식 | 사용 API | 설명 |
| --- | --- | --- | --- |
| DUPLICATE_LOGIN_ID | 409 | 3-1 | 이미 사용 중인 로컬 로그인 ID |
| DUPLICATE_EMAIL | 409 | 3-1 | 이미 사용 중인 이메일 |
| LOGIN_FAILED | 401 | 3-2 | 로그인 ID 또는 비밀번호 불일치 |
| KAKAO_LOGIN_FAILED | 401 | 3-3 | 카카오 인가 코드 교환 또는 사용자 정보 조회 실패 |
| INVALID_REFRESH_TOKEN | 401 | 3-4, 3-5 | 유효하지 않거나 만료·폐기된 Refresh Token |
| UNSUPPORTED_FORMAT | 200 응답의 `failed[]` | 4-1 | 배치 항목의 이미지 형식을 지원하지 않음 |
| IMAGE_NOT_FOUND | 404 | 4-2, 5-2, 5-6, 5-7, 8-2, 8-6 | 대상 이미지가 없거나 요청 사용자가 접근할 수 없음 |
| UPLOAD_ALREADY_COMPLETED | 409 | 4-2 | 스토리지 업로드가 이미 완료되어 URL을 재발급할 수 없음 |
| ANALYSIS_IN_PROGRESS | 409 | 4-2 | 이미지 분석이 이미 시작되어 업로드 URL을 재발급할 수 없음 |
| CONFIRM_REQUIRED | 400 | 7-2 | 저장 데이터 초기화 확인 문자열이 없거나 올바르지 않음 |
| INVALID_FCM_TOKEN | 400 | 7-4 | FCM 등록 토큰 형식이 올바르지 않음 |
| ANALYSIS_FAILED | FCM | 7-7 | 이미지 분석 비동기 처리 실패 알림 코드 |
| INVALID_DOCUMENT_IMAGES | 400 | 8-2, 8-7 | 문서 생성 이미지 목록이 비어 있거나 중복·제외 결과가 올바르지 않음 |
| DOCUMENT_IMAGE_NOT_OWNED | 403 | 8-2, 8-6 | 문서 생성에 사용할 이미지가 요청 사용자 소유가 아님 |
| IMAGE_ANALYSIS_NOT_COMPLETED | 409 | 5-2, 5-6, 5-7, 8-2, 8-6 | 대상 이미지의 분석이 완료되지 않음 |
| DUPLICATE_CLIENT_REQUEST | 409 | 8-2, 8-6, 8-7 | 동일 사용자의 동일한 clientRequestId 재요청을 중복 실행으로 인한 오류 방지를 위해 차단 |
| DOCUMENT_GENERATION_FAILED | Redis 이벤트·FCM | 8-2, 8-6, 8-7 | AI 문서 생성 비동기 처리 실패 |
| DOCUMENT_NOT_FOUND | 404 | 8-3, 8-4, 8-5, 8-6, 8-7 | 문서가 없거나 요청 사용자가 소유하지 않음 |
| DOCUMENT_IMAGE_NOT_FOUND | 404 | 8-7 | 제외하려는 이미지가 기존 문서에 포함되어 있지 않음 |
| SCHEDULE_NOT_FOUND | 404 | 9-2, 9-3 | 일정이 없거나 요청 사용자가 소유하지 않음 |
| AI_SEARCH_FAILED | 500 / Redis 결과 이벤트 | 5-5, 5-6, 5-7 | Analysis Worker의 이미지 검색 처리 실패 |
| AI_SEARCH_TIMEOUT | 504 | 5-5, 5-6, 5-7 | 지정된 대기 시간 안에 검색 결과 이벤트를 받지 못함 |

---
