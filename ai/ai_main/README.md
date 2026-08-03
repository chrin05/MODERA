# 스크린샷 지식 DB — AI 서비스 (FastAPI)

두 종류의 API 를 제공합니다.

- **`/api/v1/*` — 앱 직결 API.** Spring 의 ERD 재작업 기간 동안 앱이 직접 호출합니다.
  팀 외부 API 규약(공통 envelope·페이지 형식·에러 코드)을 따릅니다. **프론트가 쓰는 쪽입니다.**
- **`/internal/v1/*` — Spring 연동용 내부 API.** 명세 10장. Spring 복귀 시 쓰입니다.

**인증**: 지금은 `APP_API_AUTH=false` 라 **`/api/v1/*` 은 토큰 없이 호출됩니다.**
Android 가 Spring 을 거치지 않고 직접 부르는 기간용 임시 설정입니다.
`/internal/v1/*` 은 이 설정과 무관하게 항상 `X-Internal-Token` 을 요구합니다.

Spring 이 앞단에 서면 `APP_API_AUTH=true` 로 되돌립니다.

## 실행

```bash
cp .env.example .env        # 값 채우기
docker compose up -d --build
```

`GEMINI_API_KEY` 와 `INTERNAL_TOKEN` 은 기본값이 없습니다. 설정하지 않으면 기동 시 바로 실패합니다.
OpenSearch 는 기동에 30~45초 걸립니다. 그 전 요청은 연결 거부됩니다.

### LLM 프로바이더 전환

`LLM_PROVIDER` 하나로 Gemini ↔ Claude 를 바꿔 끼웁니다(기본 `gemini`, 기존 동작 그대로).

```bash
LLM_PROVIDER=claude
CLAUDE_API_KEY=<auth2api config.yaml 의 api-keys 첫 값>
CLAUDE_BASE_URL=http://127.0.0.1:8317
```

⚠️ 위 주소는 **호스트에서 uvicorn 으로 띄울 때만** 맞습니다. `docker compose` 로
띄우면 닿지 않습니다 — auth2api 가 `127.0.0.1:8317` 루프백에만 바인딩하므로
(`config.yaml` 의 `host: ''`) `host.docker.internal` 로 바꿔도 안 됩니다.
`docker-compose.yml` 의 MinIO 주석이 설명하는 것과 같은 제약입니다.
도커에서 쓰려면 (a) auth2api 를 같은 네트워크의 컨테이너로 올리거나,
(b) auth2api 를 `0.0.0.0` 으로 노출하거나, (c) AI 서버만 호스트에서 돌려야 합니다
— 자세한 내용은 `.env.example` 의 `CLAUDE_BASE_URL` 주석 참고.

Claude 모델 기본값은 AGENT `claude-sonnet-4-6`, 비전 `claude-haiku-4-5` 이고
정보성·문서·질의 변환은 AGENT 를 따릅니다. 역할별로 바꾸려면 `CLAUDE_*_MODEL_NAME`
(Gemini 쪽 `LLM_MODEL_NAME` 등과 **이름공간이 분리**되어 있어 서로 새지 않습니다).

⚠️ **임베딩은 이 토글과 무관하게 항상 Gemini** 입니다 — Anthropic 에는 임베딩
엔드포인트가 없습니다(`POST /v1/embeddings` → 404). 그래서 `LLM_PROVIDER=claude`
에서도 `GEMINI_API_KEY` · `GEMINI_BASE_URL` · `EMBEDDING_*` 는 그대로 필요합니다.
카테고리 벡터(768차원)와 그 인덱스는 전환해도 바뀌지 않습니다.

배선 점검(네트워크·SDK 없이 실행):

```bash
python scripts/llm_check.py
```

**Swagger: `http://<호스트>:8000/docs`** — `/api/v1/*` 은 Authorize 없이 바로
Try it out 이 됩니다. `/internal/v1/*` 만 우측 상단 Authorize 에 토큰이 필요합니다.
외부에 열린 서버에서 문서를 끄려면 `ENABLE_DOCS=false`.

## 앱 직결 API (`/api/v1/*`) — 프론트용

응답은 모두 팀 공통 envelope 입니다(썸네일 바이너리만 예외).

```json
{ "code": "SUCCESS", "message": "...", "data": {...}, "timestamp": "..." }
```

| 명세 | Method | Path | 비고 |
| --- | --- | --- | --- |
| 4-1 | POST | `/api/v1/images/upload` | 등록·중복판정·업로드 URL 발급. **바이너리 안 받음** |
| 4-2 | POST | `/api/v1/images/{imageId}/upload-complete` | 업로드 완료 통지 → 분석 시작 |
| 4-3 | POST | `/api/v1/images/{imageId}/ocr` | OCR 제출(4-1 에 함께 보내도 됨) |
| 4-5 | POST | `/api/v1/images/{imageId}/upload-url` | 만료된 업로드 URL 재발급 |
| 5-1 | GET | `/api/v1/analysis/summary` | 단계별·상태별 집계 |
| 5-6 | GET | `/api/v1/analysis/jobs` | `status`(콤마 구분)·`page`·`size` |
| 6-1 | GET | `/api/v1/images` | `status`·`categoryId`·`tagId`·`favorite`·`dateFrom/To`·`sort` |
| 6-2 | GET | `/api/v1/images/{imageId}` | 상세. 조회 시 `lastViewedAt` 갱신 |
| 6-6 | GET | `/api/v1/images/{imageId}/thumbnail` | `{thumbnailUrl, title, tags}` JSON |
| — | GET | `/api/v1/images/{imageId}/thumbnail/raw` | 썸네일 **JPEG 바이너리** (envelope 아님) |
| — | GET | `/api/v1/images/{imageId}/source` | **원본 이미지 바이너리** (상세 화면용) |
| 7-1 | GET | `/api/v1/tags` | `q`·`sort` |
| 7-2 | GET | `/api/v1/categories` | 카드용 썸네일·태그·개수. `sort`. **페이지네이션 없음** |
| 7-3 | GET | `/api/v1/home` | 홈 1콜 집계(현황·카테고리 8개·최근 4장) |
| 8-1 | GET | `/api/v1/search` | `q` 필수. `scope`·`sort` |

### 흐름 (명세 4-1 → 4-2)

```
① 앱 → AI      POST /api/v1/images/upload
                { images: [{ clientRequestId, fileName, contentHash, fileSize, ocr }] }
              ← { registered: [{ clientRequestId, imageId, uploadUrl, uploadExpiresIn }],
                  duplicated: [...], failed: [...] }

② 앱 → 스토리지  PUT <uploadUrl>       원본 바이너리 직접 전송 (AI 서버 안 거침)

③ 앱 → AI      POST /api/v1/images/{imageId}/upload-complete
              ← { imageId, uploadCompleted, uploadedAt }      ← 여기서 분석 시작

④ 앱 → AI      GET /api/v1/analysis/jobs    폴링으로 진행 확인
⑤ 앱 → AI      목록·상세·검색·썸네일 조회
```

이미지 바이트가 AI 서버를 통과하지 않아 100장을 한꺼번에 올려도 서버가 흔들리지 않습니다.
`clientRequestId` 는 그대로 되돌려 주므로 앱이 원래 사진과 매칭할 수 있습니다.

### 등록 요청 예시

```bash
curl -X POST http://<호스트>:8000/api/v1/images/upload \
  -H 'Content-Type: application/json' \
  -d '{"images":[{"clientRequestId":"local-001","fileName":"a.png",
       "contentHash":"<SHA-256 64자>","fileSize":384211,
       "ocr":{"rawText":"OCR 텍스트","lang":"ko","confidence":0.93}}]}'
```

`contentHash` 로 중복을 판정합니다. 이미 올린 사진이면 `duplicated` 로 분류되고
새 `imageId` 를 발급하지 않아 Gemini 를 다시 돌리지 않습니다.

형식이 안 맞으면 `failed[].reason = "UNSUPPORTED_FORMAT"`,
5MB 를 넘으면 `FILE_SIZE_EXCEEDED` 로 항목별로 갈립니다(전체 요청은 실패하지 않습니다).

`status` 는 `QUEUED → PROCESSING → COMPLETED` 로 갑니다.
`stage` 는 `LLM → IMAGE_ANALYSIS → AGENT → INDEXING` 순입니다.
OCR 이 비었거나 정보성이 없다고 판정되면 분석을 건너뛰고 `EMPTY` 로 끝나며,
카테고리 `기타` 로 색인됩니다(목록·검색에는 그대로 나옵니다).

### 프론트가 알아둘 것

- **`userId` 는 보내지 않아도 됩니다.** 로그인 미구현 구간이라 서버가 `FIXED_USER_ID`(기본 1)로
  고정합니다. 보내도 무시됩니다. 로그인이 붙으면 서버 설정만 바꾸면 되고 앱은 그대로입니다.
- **이미지 주소는 화면에 따라 필드가 다릅니다.**

  | API | 필드 | 내용 |
  | --- | --- | --- |
  | 6-1 목록 `/api/v1/images` | `thumbnailUrl` | **정사각으로 잘린** 썸네일 |
  | 6-2 상세 `/api/v1/images/{id}` | `imageUrl` | **원본 전체** |

  목록에는 `imageUrl` 이, 상세에는 `thumbnailUrl` 이 **없습니다.** 화면 성격에 맞춰
  하나씩만 내려보냅니다 — 목록에서 원본을 여러 장 부르면 수 MB 씩이라 느려지고,
  상세에서 썸네일을 쓰면 위아래가 잘려 스크린샷 내용을 읽을 수 없기 때문입니다.

- **둘 다 경로만** 내려옵니다(`/api/v1/images/3/thumbnail/raw`, `/api/v1/images/3/source`).
  베이스 URL 을 앞에 붙여서 쓰면 됩니다. 만료가 없으므로 캐시해도 됩니다.
  **지금은 `APP_API_AUTH=false` 라 헤더 없이 그대로 이미지 로더에 넣으면 됩니다.**
  조회 URL 에는 **만료가 없습니다.** 업로드 URL(4-1 `uploadUrl`)만 presigned 라
  10분 만료가 있고, 만료되면 4-5 로 재발급받으면 됩니다.
  (`PRESIGNED_READ_URLS=true` 로 켜면 조회도 스토리지 presigned 로 바뀝니다.
  서버 트래픽은 줄지만 만료가 생겨 앱 이미지 캐시가 매번 깨지므로 기본은 꺼져 있습니다.)
- **태그·카테고리는 전부 AI 가 자동 생성합니다.** 사람이 직접 만들거나 고치는 경로는
  없습니다(6-3 수정 API 는 범위 밖). 그래서 `tags[].source` 와 `fieldSources` 는
  항상 `"AGENT"`, `TagItem.createdBy` 도 항상 `"AGENT"` 입니다.
  카테고리는 분석 시작 시 기본 17종을 후보로 쓰고, AGENT 가 그중 어디에도
  못 붙이겠다고 판단하면 새 이름을 만들어 늘어납니다.
- **카테고리(7-2)만 페이지네이션이 없습니다.** 프론트 요청으로 뺐습니다(명세와 다른 부분).
  `data.list` 에 전체가 들어오고 `page`·`size`·`totalElements` 등은 없습니다.
  `page`·`size` 를 보내도 무시됩니다. `sort` 는 그대로 동작합니다.
  카테고리 수가 적은 이유도 이것입니다 — 사람이 만드는 게 아니라 AI 가 필요할 때만 늘립니다.
  나머지 목록 API(6-1·7-1·8-1·5-6)는 페이지 형식 그대로입니다.
- **카테고리 카드 이미지** 는 그 카테고리에 가장 최근 분류된 사진의 썸네일입니다.
  전용 이미지를 따로 만들지 않으므로 새 사진이 분류되면 자동으로 바뀝니다.
- **`categoryId`·`tagId` 는 이름에서 파생된 해시**입니다. 재시작해도 같은 값이지만
  Spring 복귀 시 DB 실제 ID 로 바뀝니다. **저장하지 말고 매번 응답값을 쓰세요.**
- **즐겨찾기(6-5)·삭제(6-4)는 AI 범위 밖입니다.** `favorite` 은 **항상 `false`** 로
  내려가고, 6-1 의 `favorite=true` 필터는 언제나 빈 결과입니다.
  필드와 필터는 나중에 6-5 가 붙어도 계약을 안 고치게 남겨 둔 것이니,
  **지금은 앱에서 즐겨찾기 UI 를 숨기는 편이 맞습니다.**
- **채우지 못하는 필드는 `null`** 로 내려갑니다(`structuredData`, `matchedIn`, `highlight` 등).
  필드 자체는 유지되므로 Spring 복귀 시 앱 모델을 고칠 필요가 없습니다.
- 에러는 `{"code": "INVALID_PARAMETER"|"IMAGE_NOT_FOUND"|..., "message": ..., "data": ...}` 입니다.

## Spring 연동 API (`/internal/v1/*`)

| 명세 | Method | Path | 처리 |
| --- | --- | --- | --- |
| 10-1 | POST | `/internal/v1/analyze` | 비동기. 202 반환 후 10-4 로 콜백 |
| 10-2 | POST | `/internal/v1/embed` | 동기 |
| 10-3 | POST | `/internal/v1/query/parse` | 동기. 실패 시 `parsedConditions: null` 로 degrade |
| — | POST | `/internal/v1/search` | 동기. OpenSearch 키워드(BM25) 검색. `userId` 로 격리 |
| — | POST | `/internal/v1/search/related` | 동기. 연관 이미지(1장 기준 인근 검색). 아래 참고 |
| — | POST | `/internal/v1/documents/candidates` | 동기. 문서화 이미지 선택(N장 누적 → 다음 후보). 아래 참고 |
| — | POST | `/internal/v1/documents` | 동기. Spring 이 보낸 분석 결과 여러 건 → 마크다운 문서 |
| — | POST | `/internal/v1/categories/reanalyze` | 동기. 카테고리 재분석(거부한 카테고리 배제, 최대 5회). [docs/CATEGORY_REANALYZE.md](docs/CATEGORY_REANALYZE.md) |
| — | GET | `/health` | 헬스체크 (토큰 불필요) |

FastAPI 가 호출하는 쪽: 10-4 콜백, 10-5 지식 후보 조회.
이 구간은 envelope 없이 raw JSON 이고, 에러는
`{"error": CODE, "message": ..., "detail": {}}` 형식입니다.

Spring 이 아직 안 떠 있으면 `SPRING_ENABLED=false` 로 두세요.
켜져 있으면 매 분석마다 10-5 조회 실패까지 약 4초를 버립니다.
`SPRING_ENABLED=false` 여도 요청에 `callbackUrl` 이 있으면 그 주소로는 콜백을 보냅니다.

### stage — `LLM` / `IMAGE_ANALYSIS` / `AGENT` / `FULL`

앞 세 개는 한 요청에 한 단계만 돌고, 다음 단계로 넘길지는 Spring 이 정합니다.
**`FULL` 은 한 요청에서 세 단계를 이어서 처리하고 콜백을 한 번만 보냅니다** —
`analysis-worker` 의 `FastApiAnalysisClient` 가 쓰는 방식입니다.

```json
POST /internal/v1/analyze          X-Internal-Token: <공유 토큰>
{ "jobId": 7001, "imageId": 7001, "userId": 1, "stage": "FULL",
  "input": { "image": { "s3Key": "u/1/a.png" } },
  "options": { "maxTags": 10, "language": "ko" },
  "callbackUrl": "http://analysis-worker:8081/internal/v1/callback/analysis" }
→ 202 { "jobId": 7001, "imageId": 7001, "stage": "FULL", "accepted": true, "status": "QUEUED" }
```

콜백(10-4)의 `result` 는 `title`·`summary`·`tags`·`category`·`keyInformation`·
`analysisConfidence`·`scheduleData`·`documentVector`(768차)·`thumbnailKey` 를 포함합니다 —
worker 의 `AnalysisCallbackService` 가 읽는 키입니다. `status` 는 `COMPLETED` / `EMPTY` / `FAILED`.
`thumbnailKey` 는 썸네일 버킷에 저장된 key(원본과 동일 문자열, 버킷만 다름)이고,
썸네일 생성이 실패한 경우 `null` 입니다. EMPTY 콜백에도 포함됩니다(비정보성 이미지도
목록에 나오므로 썸네일은 판정 전에 만들어 둡니다).
`informative`·`ocrRefinedText` 는 보내지 않습니다(2026-07-29 합의) — 정보성 여부는
`status=EMPTY` 로 구분되고, OCR 원문은 앱이 4-1 로 보낸 것을 Spring 이 자체 보관합니다.

`FULL` 요청에는 OCR 이 실려 오지 않으므로(worker 가 `input.image` 만 보냄) 이미지
분석을 먼저 돌려 거기서 읽어낸 텍스트를 OCR 대용으로 씁니다. `input.ocr` 이 오면 그게 우선입니다.

로컬에서 worker 와 붙여 돌리는 절차는 **[LOCAL_DEV.md](LOCAL_DEV.md)** 를 보세요
(`MOCK_AI=true` 로 Gemini 키 없이 띄우는 법 포함).

## 문서화 (`POST /internal/v1/documents`)

> 전체 필드·에러 코드·Spring 연동 유의사항은 **[docs/DOCUMENT_API.md](docs/DOCUMENT_API.md)** 에 명세서 형식으로 정리했다. 아래는 요약이다.

분석이 끝난 이미지 여러 장을 묶어 하나의 마크다운 문서로 만듭니다.
분석 파이프라인과 별개 기능입니다 — 새 단계를 추가하지 않습니다.

**재료는 Spring 이 요청 본문에 실어 보냅니다(10-1 과 같은 방식).** AI 는 색인·저장소를
조회하지 않습니다. 데이터 보관 주체가 Spring 이므로 AI 쪽 사본을 읽으면 두 곳이 어긋날
수 있고, 조회 왕복·타임아웃·소유자 검증이 전부 딸려옵니다. **어떤 이미지가 이 사용자
것이고 분석이 끝났는지는 Spring 이 질의 단계에서 걸러서 보내야 합니다.**

**N장 → 문서 1개입니다. 이미지 한 장당 한 번 부르는 API 가 아닙니다.** 이미지들이
한 프롬프트에 함께 들어가야 모델이 비교·병합·시간순 서술을 할 수 있습니다. 장당 호출로
쪼개면 각 문서가 서로를 몰라 같은 결과가 나오지 않습니다. 응답에 `imageId` 는 없고,
`sourceImageIds` 와 섹션별 `imageIds` 로 어느 이미지에서 나왔는지만 표시합니다.

```
Spring → AI   POST /internal/v1/documents
{
  "userId": 1,
  "instruction": "구매 후보를 가격순으로 비교해 줘",   // 선택
  "title": null,          // 선택. 없으면 모델이 제목을 정한다
  "language": null,       // 선택
  "images": [                                     // ← 여러 장을 한 번에
    { "imageId": 101,
      "title": "교보문고 C++ 프로그래밍 입문",
      "summary": "C++ 입문서 32,000원, 10% 할인 중",
      "tags": ["도서", "프로그래밍"],               // 이름만. {tagId,name} 아님
      "category": "쇼핑",                          // 카테고리 이름
      "keyInformation": ["가격: 32,000원", "판매처: 교보문고"],   // 10-4 로 보낸 그 필드
      "ocr": { "rawText": "교보문고 C++ 프로그래밍 입문 32,000원", "refinedText": null },
      "createdAt": "2026-07-16T06:00:00.000Z" },

    { "imageId": 102, "title": "예스24 C++ 프로그래밍 입문",
      "summary": "같은 책 28,800원, 무료배송", "tags": ["도서", "프로그래밍"],
      "category": "쇼핑", "keyInformation": ["가격: 28,800원", "판매처: 예스24"],
      "ocr": { "rawText": "예스24 C++ 프로그래밍 입문 28,800원 무료배송" },
      "createdAt": "2026-07-16T06:02:00.000Z" },

    { "imageId": 103, "title": "알라딘 중고 C++ 프로그래밍 입문",
      "summary": "중고 상급 15,000원", "tags": ["도서", "중고"],
      "category": "쇼핑", "keyInformation": ["가격: 15,000원", "판매처: 알라딘"],
      "ocr": { "rawText": "알라딘 중고샵 C++ 프로그래밍 입문 상급 15,000원" },
      "createdAt": "2026-07-16T06:05:00.000Z" }
  ]
}

  ① prepare_sources    중복·빈 항목 정리 (조회 없음)
  ② generate_document  Gemini 1회 호출 — 3장이 한 프롬프트에 → {title, summary, sections[]}
  ③ render_markdown    파이썬이 마크다운으로 렌더 (모델이 아님)

            ← {
  "title": "C++ 입문서 구매 후보 비교",
  "summary": "같은 책을 세 곳에서 확인했다. 최저가는 알라딘 중고 15,000원.",
  "sections": [
    { "heading": "가격 비교", "body": "...",
      "bullets": ["알라딘 중고 15,000원 (상급)", "예스24 28,800원 (무료배송)",
                  "교보문고 32,000원 (10% 할인)"],
      "imageIds": [103, 102, 101] }        // ← 한 섹션이 세 장을 근거로 삼는다
  ],
  "markdown": "# C++ 입문서 구매 후보 비교\n\n...",
  "sourceImageIds": [101, 102, 103],
  "skipped": [],
  "modelVersion": "gemini-2.5-flash-lite",
  "generatedAt": "2026-07-16T06:10:00.000Z"
}
```

- **`markdown` 이 최종 산출물입니다.** `sections` 는 Spring/앱이 직접 재조립하고 싶을 때 쓰는 원자료입니다.
- 마크다운에는 이미지 id 와 출처 표를 넣지 않습니다(내부 id 는 사용자에게 의미가 없고 본문 흐름을 끊습니다). 근거 매핑은 `sections[].imageIds` 로만 나갑니다.
- 마크다운을 모델에게 시키지 않고 구조(JSON)만 받아 서버가 렌더합니다. 출력 모양이 항상 같고 코드펜스·잡문이 섞이지 않습니다. 모델이 규칙을 어기고 `summary`·`body` 에 마크다운을 넣어도 서버가 중화합니다(줄 앞 `#`·`>`·`|` 는 escape, 코드펜스 줄은 폐기).
- `title`·`summary`·`keyInformation`·`ocr` 이 **전부 빈** 항목은 `skipped: NO_CONTENT` 로 빠집니다(빈 블록을 넣으면 모델이 지어냅니다). 한 장도 못 쓰면 `NO_DOCUMENT_SOURCE`(400).
- 한 번에 **최대 30장**(`document.MAX_IMAGES`), 이미지당 OCR **1500자**까지 프롬프트에 넣습니다. OCR 은 자르지 말고 전체를 보내면 AI 가 자릅니다. 30장 초과는 400.
- `ocr` 은 `refinedText` 가 있으면 그쪽을, 없으면 `rawText` 를 씁니다(분석 단계와 같은 규칙).
- Gemini 호출 1회라 동기 응답입니다(분석처럼 job·콜백을 만들지 않습니다). 장수가 늘어 응답이 길어지면 10-1 처럼 202 + 콜백으로 바꾸면 됩니다.
- OpenSearch 에 의존하지 않으므로 검색이 죽어 있어도 동작하고, Swagger 에서 손으로 만든 JSON 으로 바로 시험할 수 있습니다.

```bash
python test/test_document.py   # N→1 묶기·건너뛰기·렌더링 자체 점검 (네트워크 불필요)
```

`ai/ai_main/test/` 는 `.gitignore` 대상입니다 — clone 만 한 상태에는 이 파일이 없습니다.
Gemini 호출은 `gemini_client.generate_json` 을 가짜 응답으로 바꿔치기해 대신하므로 키가
필요 없고, 확인하는 것은 모델 품질이 아니라 우리 코드의 계약(중복·빈 항목·거짓 출처
제거·제목 고정·렌더 형태)입니다.

### Spring 쪽에 필요한 작업

AI 가 만드는 것은 위 `/internal/v1/documents` 하나뿐입니다. 나머지는 Spring 몫입니다.

| 구분 | 내용 |
| --- | --- |
| 앱용 API | `POST /api/v1/documents` 등. 앱에서 `imageIds` 를 받아 처리 |
| DB 조회 | 그 imageIds 중 **해당 userId 소유 + `status=COMPLETED`** 만 골라 위 8개 필드로 변환 |
| AI 호출 | `/internal/v1/documents` 를 **1회** 호출 (장당 호출 아님) |
| 저장 | `document`(documentId PK, userId, title, markdown, modelVersion, generatedAt) + `document_image`(documentId, imageId, sortOrder) N:M |
| 조회 API | 문서 목록·상세 |

AI 가 Spring 을 조회하지 않으므로 **Spring 에 새 내부 조회 API 는 필요 없습니다.**
필터링(소유자·분석 완료)은 Spring 의 질의 단계에서 끝나야 합니다 — AI 는 받은 것을
그대로 씁니다.

확인이 필요한 필드 두 가지:

1. **`ocr.rawText` 보관 여부.** 10-4 콜백에 OCR 원문은 들어 있지 않습니다. 앱이 4-1 등록
   요청에 실어 보낸 것을 Spring 이 저장하고 있어야 합니다. 없으면 문서 품질이
   "요약문 이어붙이기" 수준까지 떨어집니다.
2. **`keyInformation` 보관 여부.** AI 가 10-4 로 보내고는 있지만 명세 밖 합의 필드라
   Spring 이 버리고 있을 수 있습니다. 가격·날짜 같은 구체 사실이 여기 담깁니다.

## 썸네일

사진 1장당 1개입니다. 분석할 때 만들어 **썸네일 버킷에 원본과 같은 key** 로 저장합니다.

```
pictures/u/1/a.png    원본
thumbnails/u/1/a.png  썸네일 (내용은 JPEG)
```

가운데를 잘라 정사각으로 만들고, 해상도는 기본적으로 축소하지 않습니다
(`THUMBNAIL_MAX_SIZE=0`). 목록이 무거우면 `480` 등을 넣어 줄일 수 있습니다.
저장본이 없으면 조회 시 즉석 생성하면서 버킷도 채웁니다.

명세 6-6 은 `{thumbnailUrl, title, tags}` JSON 을 요구하므로 그 경로는 JSON 을 주고,
실제 이미지는 `thumbnailUrl` 이 가리키는 `/thumbnail/raw` 가 줍니다.
스토리지 presigned GET 을 쓰지 않는 이유는 만료(1시간)가 있어 앱 이미지 캐시가
매번 깨지고, 스토리지를 외부에 공개해야 하기 때문입니다.

## 서버 배포

```bash
git pull
cd ai/ai_main
cp .env.example .env      # 최초 1회. 값 채우기
docker compose up -d --build
docker compose logs -f ai-service
curl localhost:8000/health
```

로컬 `.env` 와 서버 `.env` 에서 **달라야 하는 값**:

| 키 | 로컬 | 서버 |
| --- | --- | --- |
| `S3_ENDPOINT` | `http://host.docker.internal:9000` (SSH 터널) | `http://host.docker.internal:9000` (compose 의 host-gateway) 또는 MinIO 와 같은 네트워크면 `http://minio:9000` |
| `ENABLE_DOCS` | `true` | 통합 테스트 중엔 `true` 가 편하고, 외부 공개 시 `false` |

`.env` 는 커밋되지 않으므로 서버에서 직접 채워야 합니다.

확인할 것:
- **8000 포트가 앱에서 접근 가능한지**(보안그룹/방화벽)
- **9200 은 열지 말 것** — compose 가 `127.0.0.1` 로만 바인딩합니다
- MinIO 접근: `docker compose exec ai-service python -c "from app import storage; print(len(storage.fetch_image('<실제_s3Key>')))"`
- 썸네일 쓰기 권한: 위가 되면 같은 방식으로 `storage.store_thumbnail('<키>')`

## 구조

```
app/
  config.py         환경변수 설정 (자격증명 기본값 없음)
  schemas.py        요청·응답 스키마 (내부 snake_case ↔ 경계 camelCase)
  llm/              LLM 프로바이더 디스패처 (LLM_PROVIDER 토글 하나로 전환)
    __init__.py     generate_json / image_part 만 노출. 호출부는 프로바이더를 모른다
    claude.py       Anthropic 호출 격리 (auth2api 경유). anthropic SDK 를 이 파일에만 가둠
  gemini_client.py  Gemini 호출 격리 + **임베딩 전담**(프로바이더 무관, 항상 여기)
  storage.py        S3 원본 이미지 조회
  spring_client.py  10-4 콜백 / 10-5 후보 조회
  category.py       카테고리 유사도 판정 (이름 임베딩 캐시 포함)
  category_store.py 카테고리 대표 벡터 저장소 (Gemini 768차원, 본문과 별 인덱스)
  stages.py         LLM / IMAGE_ANALYSIS / AGENT 단계 실행 + 썸네일 + 검색 색인
  document.py       Spring 이 보낸 분석 결과 묶어 마크다운 문서 생성 (파이프라인과 별개)
  search.py         OpenSearch 키워드 검색 (nori 색인/조회)
  jobs.py           jobId+stage 멱등 처리 / 앱 직결 작업 상태
  responses.py      앱 API 공통 envelope·페이지 형식
  timeutil.py       공용 시간 형식(now_iso, 명세 1.2) 단일 출처
  main.py           FastAPI 엔드포인트
scripts/
  seed_minio.py     로컬 MinIO 버킷·테스트 이미지 생성
  full_stage_test.py  FULL 동시 요청 + 콜백 수신 테스트(세마포어 회귀 테스트 겸용)
```

`stages.py` 의 동시 실행 세마포어는 **진입점(`execute_stage` / `run_app_analysis`)에서만**
잡는다. 파이프라인 본체(`_run_full_pipeline`)가 또 잡으면 `asyncio.Semaphore` 가
재진입 불가라 자기 자신을 기다리며 멈춘다.

## 검색 (OpenSearch)

FastAPI 가 색인·검색을 전담한다. AGENT 단계가 끝나면 요약·태그·카테고리·OCR
원문을 색인하고, `/internal/v1/search` 로 BM25 키워드 검색을 제공한다.
구조·운영 주의사항은 **[OPENSEARCH.md](OPENSEARCH.md)** 에 따로 정리했다(인프라 참고용).

한글 품질을 위해 **nori** 형태소 분석기를 쓰므로 플러그인이 설치된 이미지가 필요하다
(`opensearch.Dockerfile` 참고). 로컬/시연은 `docker-compose up -d` 로 nori 포함
OpenSearch + AI 서비스를 함께 띄운다.

> 색인은 best-effort 다. OpenSearch 가 잠시 죽어도 분석·콜백은 정상 진행되고,
> 해당 이미지는 검색 인덱스에만 누락된다(재분석 시 덮어쓰기).

### 연관 이미지 — `POST /internal/v1/search/related`

사용자가 이미지 한 장을 고르고 '연관 이미지 선택' 을 누르면 Spring 이 `imageId` 를
넘긴다. AI 는 그 이미지의 **색인된 요약 벡터를 그대로 질의로 써서** kNN 으로 인근
이미지를 찾는다(임베딩 호출 0회). 결과 중 한 장을 골라 다시 부르면 연쇄가 이어진다 —
서버는 상태를 들고 있지 않아 매 호출이 독립이다.

```json
POST /internal/v1/search/related   X-Internal-Token: <공유 토큰>
{ "userId": 1, "imageId": 101, "correlationId": "2207708f-...", "page": 0, "size": 10 }
```

```json
{ "eventType": "IMAGE_SEARCH_COMPLETED", "version": 1,
  "payload": { "correlationId": "2207708f-...", "total": 2, "page": 0, "size": 10,
    "hits": [ { "imageId": 204, "score": 0.812345 }, { "imageId": 77, "score": 0.60113 } ] } }
```

- `correlationId` 는 생략하면 서버가 UUID 를 만든다(응답에는 항상 있다).
- `score` 는 코사인 유사도. `SEARCH_KNN_MIN_COSINE`(기본 0.45) 미만은 잘라낸다 —
  "인근" 이 없으면 `hits` 는 빈 배열이고, 억지로 채우지 않는다.
- 기준 이미지 자신은 결과에서 뺀다. 요약 벡터가 없는 이미지(빈 OCR 등)도 빈 결과다.
- 남의 `imageId` 나 없는 `imageId` 는 둘 다 `404 IMAGE_NOT_FOUND` 다(존재 여부를
  알려 주면 남의 imageId 열거에 쓰인다).
- `total` 은 고정 후보풀 위의 생존자 수라 페이지를 넘겨도 흔들리지 않는다.

### 자연어 검색 (명세 5-5) — `POST /internal/v1/images/search/semantic`

앱의 `POST /api/v1/images/search/semantic` 뒤에 있는 AI 구간. API 서버가
`IMAGE_SEMANTIC_SEARCH_REQUESTED` 를 Redis Streams 로 발행하면 analysis-worker 가
이 엔드포인트를 부르고, 응답 봉투를 결과 스트림에 그대로 싣는다.

```json
POST /internal/v1/images/search/semantic   X-Internal-Token: <공유 토큰>
{ "userId": 1, "query": "C++ 프로그래밍 책 가격을 보여줘",
  "correlationId": "0d2647bb-...", "page": 0, "size": 20 }
```

```json
{ "eventType": "IMAGE_SEARCH_COMPLETED", "version": 1,
  "payload": { "correlationId": "0d2647bb-...", "total": 2, "page": 0, "size": 20,
    "hits": [ { "imageId": 101, "score": 3.9987202 }, { "imageId": 102, "score": 1.8765116 } ] } }
```

- 검색은 `/internal/v1/search` 기본 모드와 같은 **cascade(F4)**: BM25 먼저, 빈
  결과면 시맨틱(하이브리드) 승격. 문장·단어 모두 이 경로 하나로 처리한다.
- `query` 는 앞뒤 공백 제거 후 빈 문자열이면 `400 INVALID_REQUEST`.
- `size` 기본 20, 최대 100. `correlationId` 생략 시 서버가 UUID 를 만든다.
- `score` 는 정렬 근거일 뿐 스케일 의미가 없다(BM25/하이브리드에 따라 다름).
  API 서버는 순서만 유지하고 score 는 클라이언트에 내리지 않는다.

### 문서화 이미지 선택 — `POST /internal/v1/documents/candidates`

문서화 재료를 한 장씩 늘려 가며 고르는 화면. 지금까지 고른 이미지들의 **중심 벡터**로
다음 후보를 추천하고, 고른 것들은 결과에서 뺀다.

```
① 첫 장 선택            → {"imageIds": [101]}          → 추천
② 하나 고름             → {"imageIds": [101, 204]}     → 두 장의 중심 벡터로 추천
③ 또 고름               → {"imageIds": [101, 204, 77]} → 세 장의 중심 벡터로 추천
④ 최대 10장까지. 선택 완료 → POST /internal/v1/documents (문서 생성)
```

```json
POST /internal/v1/documents/candidates   X-Internal-Token: <공유 토큰>
{ "userId": 1, "imageIds": [101, 204], "correlationId": "2207708f-...",
  "page": 0, "size": 10 }
```

```json
{ "eventType": "IMAGE_SEARCH_COMPLETED", "version": 1,
  "payload": { "correlationId": "2207708f-...", "total": 2, "page": 0, "size": 10,
    "hits": [ { "imageId": 77, "score": 0.812345 }, { "imageId": 310, "score": 0.60113 } ],
    "selectedImageIds": [101, 204] } }
```

- **선택 목록은 서버가 들고 있지 않다.** 매 호출에 전체 `imageIds` 를 보낸다.
  서버 세션으로 만들면 프로세스 메모리(단일 인스턴스 전제)가 되어 재기동·스케일아웃에서
  선택이 날아간다. 정수 10개를 다시 보내는 비용으로 무상태를 산다 — 뒤로 가기·앱 재시작·
  중간 실패가 전부 "그 시점 목록으로 다시 호출" 하나로 복구된다.
  `selectedImageIds` 로 되돌려 주니 앱이 자기 누적 목록과 대조할 수 있다.
- **순서는 결과에 영향이 없다.** 중심 벡터는 집합 연산이라 첫 장과 열 번째 장의 무게가
  같다. 중복 id 는 서버가 제거한다(남기면 그 이미지가 중심에 두 번 반영돼 추천이 쏠린다).
- 최대 10장(`related.MAX_SELECTION`). 넘기면 400. 문서 생성기 상한
  (`document.MAX_IMAGES`=30)과는 별개 노브다.
- `score` 는 **정규화한 중심 벡터**와의 코사인. 순위 근거다.
- **컷은 중심 벡터가 아니라 "선택 중 아무 한 장과의 최대 코사인"** 이 `SEARCH_KNN_MIN_COSINE`
  (기본 0.45) 이상인지로 한다. 중심 벡터는 선택이 늘수록 어느 개별 문서와도 멀어져서
  (다양한 10장이면 관련 문서도 0.37 수준) 고정 임계값에 전부 걸린다 — 추천이 에러 없이
  마르는 조용한 고장이다. 최대 유사도는 선택 수와 무관하므로 캘리브레이션된 값을 그대로 쓴다.
  선택이 1장이면 두 기준이 같아져 연관 이미지와 결과가 동일하다.
- 없는 `imageId`·남의 `imageId` 는 **하나라도 섞이면** `404 IMAGE_NOT_FOUND`.
  하나만 허용해도 남의 벡터가 중심에 반영돼, 내 이미지 순위로 남의 내용을 추론할 수 있다.
- **추천 0건은 정상 상태다.** 더 붙일 이미지가 없다는 뜻이고, 사용자는 그 상태로 문서
  생성으로 넘어갈 수 있어야 한다. 요약 벡터가 있는 선택이 하나도 없을 때도 빈 결과다.
- 이 엔드포인트는 **문서를 만들지 않는다.** 선택이 끝나면 Spring 이 그 id 들로
  `POST /internal/v1/documents` 를 부른다.

**모듈 경계.** 화면 계약(`imageIds`·상한·`selectedImageIds`)은
[app/doc_selection.py](app/doc_selection.py) 가 들고, 검색·중심 벡터·사용자 격리는
[app/related.py](app/related.py) 의 `similar_images` **하나만** 쓴다. kNN 질의와
격리 필터를 복사하면 한쪽에서만 `user_id` 필터가 빠지는 사고가 난다.
점검: `python test/test_doc_selection.py`,
결과 기록 [test/results/test_doc_selection.md](test/results/test_doc_selection.md).

### 사용자 격리 (위 두 API 공통)

벡터는 사용자별 인덱스가 아니라 **하나의 인덱스·하나의 `embedding`
필드**에 전부 들어 있다 — 벡터 공간은 물리적으로 공유된다. 격리는 공간 분리가 아니라
모든 질의에 걸리는 `user_id` term 필터로 한다(목록·검색·집계 전부 같은 방식).
인근 검색(`related.similar_images`)은 여기서 두 겹 더 건다:

0. 선택 이미지 **전부** 소유 검증(mget 1왕복) — 하나라도 남의 것이면 404.
1. kNN 질의의 pre-filter — `{"term": {"user_id": ...}}` 를 `knn` 필드객체 안에 둔다
   (OpenSearch 2.4+ lucene efficient pre-filtering).
2. 돌아온 hit 마다 `user_id` 를 **다시 대조**해 안 맞으면 버리고 `ERROR` 로 남긴다.

2번을 둔 이유는 1번이 OpenSearch 버전·엔진 지원에 의존하기 때문이다. 필터가 어떤
이유로든 안 먹으면 남의 이미지가 결과에 섞이는데, 코드가 그 사실을 스스로 알 방법이
없다. 그래서 값을 직접 본다. 로그에 이 `ERROR` 가 뜨면 인덱스 매핑·OpenSearch
버전을 확인해야 한다(결과가 새지는 않는다 — 버리고 나간다).

`userId` 는 0 이하를 거부한다(400). 0 은 카테고리 벡터 저장소에서 전역 시드
소유자(`search.SEED_USER_ID`)로 쓰는 값이다. `FIXED_USER_ID=0` 은 "고정 해제"
(요청 값 사용)를 의미하고, 그때부터 이 필터가 유일한 격리 장치다.

두 기능 다 **파일 하나 + `main.py` 의 `include_router` 한 줄**이 전부다.
`search.py`·`schemas.py` 는 안 닿는다 — 머지 충돌을 피하려는 의도이고, 되돌릴 때도
그 파일과 한 줄만 지우면 된다.

| 기능 | 모듈 | 점검 | 결과 기록 |
| --- | --- | --- | --- |
| 연관 이미지 + 인근 검색 코어 | [app/related.py](app/related.py) | `python test/test_related.py` | [test/results/test_related.md](test/results/test_related.md) |
| 문서화 이미지 선택 | [app/doc_selection.py](app/doc_selection.py) | `python test/test_doc_selection.py` | [test/results/test_doc_selection.md](test/results/test_doc_selection.md) |

둘 다 OpenSearch 없이 돈다(가짜 클라이언트는 `test/fakes.py` 하나를 공유한다).

## 팀 확인이 필요한 사항

1. **카테고리 대표 벡터.** AI 서버가 카테고리별 centroid 를 OpenSearch `{OPENSEARCH_INDEX}_categories` 인덱스에 직접 유지합니다(판정된 카테고리에 그 이미지의 요약 임베딩을 누적 평균으로 반영). 서버를 재기동해도 카테고리와 벡터가 남고, 이미지가 쌓일수록 정확해집니다. 기본 17종 이름 벡터는 기동 시 전역 시드(`user_id=0`)로 심습니다. 10-5 응답에 `representativeVector` 가 오면 그쪽을 우선 씁니다(스키마는 이미 준비돼 있음) — 백엔드가 pgvector centroid 를 유지한다면 어느 쪽을 정본으로 볼지 합의가 필요합니다. **추가 카테고리 벡터를 사용자별로 둘지 공유할지는 미결입니다** — 현재는 사용자별 격리. 트레이드오프와 바꿀 때 손댈 지점은 [docs/CATEGORY_VECTOR_STORE.md](docs/CATEGORY_VECTOR_STORE.md) 참고.
2. **기본 카테고리 위치.** 쌓인 카테고리가 하나도 없을 때(콜드 스타트) `stages.DEFAULT_CATEGORIES` 17개를 후보로 씁니다. 원칙적으로는 Spring DB 시드로 옮기는 편이 낫습니다.
3. **`categoryCreated` 필드.** AGENT 결과에 신규 카테고리 여부를 담았습니다. 명세에 없는 필드라 Spring 과 합의가 필요합니다(불필요하면 제거).
3-1. **`documentVector` 필드.** AGENT 결과에 검색용 문서 임베딩(요약 기준, `DOCUMENT`)을 함께 실어 보냅니다. Spring 은 콜백 한 번으로 메타데이터와 벡터를 같이 받아 pgvector 등에 적재하면 됩니다(임베딩 재호출 불필요). `embeddingModel`·`embeddingDimension` 도 함께 넘어가니 벡터 컬럼 차원과 일치하는지 확인이 필요합니다. 명세 밖 필드라 Spring 과 계약 확정이 필요합니다.
4. **`keyInformation`.** 명세 10-4·6-2 에 있는 필드라 AGENT 가 생성하도록 넣었습니다. MVP 에서 안 쓸 거면 프롬프트에서 빼면 됩니다.
5. **멱등 저장소.** `jobs.py` 는 프로세스 메모리 기반이라 단일 인스턴스 전제입니다. 다중 인스턴스·재시작 대응이 필요하면 Redis 로 옮겨야 합니다.
6. **SDK.** `google-generativeai` 는 지원 종료 예고 상태입니다. `gemini_client.py` 한 파일만 고치면 `google-genai` 로 옮길 수 있습니다.

## 일정 데이터 (scheduleData)

AGENT 가 스크린샷에서 캘린더용 일정(예약·티켓·행사·마감)을 추출해 10-4 콜백 `result.scheduleData` 로 보냅니다. 저장·앱 캘린더 서빙은 Spring 몫입니다.

```json
"scheduleData": { "type": "schedule", "fields": {
    "startYear": 2026, "startMonth": 8, "startDay": 3, "startTime": "14:30",
    "endYear": 2026, "endMonth": 8, "endDay": 3, "endTime": "16:00" } }
```

- 연·월·일은 정수, `startTime`/`endTime` 은 `"HH:MM"` 문자열(시각이 없으면 `null` = 종일).
- **시점이 하나뿐인 일정(예약·마감 "~까지")은 end 만** 채웁니다 — 단일 일정은 end 만 보면 됩니다.
  **start 는 기간 행사(시작+종료)에만** 채워집니다.
- 일정이 없는 스크린샷은 `{"type": null, "fields": {}}` 입니다(필드 자체는 항상 존재).
- 상대 날짜("내일" 등)는 서버 현재 시각(KST) 기준으로 해석합니다. 확인 안 된 값은 null(추측 금지).
- 앱 직결 API(6-2)의 `structuredData` 는 이름이 비슷하지만 별개 필드이고 항상 `null` 입니다.
