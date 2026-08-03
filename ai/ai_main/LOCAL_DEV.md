# 로컬 실행 · analysis-worker 연동 테스트

`backend/local-infra` 스택(MinIO·Redis·analysis-worker)과 ai-server 를 로컬에서
같이 띄우고 FULL stage 를 왕복시키는 절차다.

---

## 0. 전제 — backend/local-infra 를 먼저 띄운다

```bash
cd backend/local-infra
docker compose up -d                      # 인프라만 (MinIO·Redis·DB)
docker compose --profile app up -d        # analysis-worker·api-server 까지
```

`local-infra/docker-compose.yml` 은 `develop/backend` 브랜치에만 있다. 이 브랜치
(`develop/ai` 계열)에는 `backend/` 소스가 없으므로, 워크트리를 따로 두거나
`develop/backend` 를 체크아웃한 사본에서 실행할 것.

호스트에 노출되는 포트:

| 서비스 | 컨테이너 | host 포트 | 비고 |
| --- | --- | --- | --- |
| MinIO API | `minio` | **9002** | 9000 은 다른 프로젝트가 점유 |
| MinIO 콘솔 | `minio` | 19001 | minioadmin / minioadmin |
| Redis | `redis` | 6380 | |
| analysis-db | `analysis-db` | 5434 | pgvector |
| api-server | `api-server` | 8090 | |
| **analysis-worker** | `analysis-worker` | **없음** | ⚠️ 아래 참고 |

> ⚠️ **analysis-worker 는 8081 을 호스트로 내보내지 않는다.** 그래서
> `callbackUrl=http://localhost:8081/...` 은 호스트에서 안 닿는다. 방법은 둘이다.
>
> 1. **(권장)** ai-server 를 같은 도커 네트워크에 넣고
>    `http://analysis-worker:8081/...` 로 부른다 → 2-B
> 2. local-infra 의 `analysis-worker` 서비스에 `ports: ["8081:8081"]` 을 추가하고
>    `docker compose --profile app up -d analysis-worker` 로 다시 띄운다 → 2-A

---

## 1. 환경변수

```bash
cd ai/ai_main
cp .env.local.example .env.local
```

로컬에서 반드시 맞춰야 하는 값만 추리면:

| 변수 | 로컬 값 | 이유 |
| --- | --- | --- |
| `INTERNAL_TOKEN` | `local-dev-internal-token` | worker 의 `internal.callback.token` 기본값과 **같아야** 한다. 양방향 공유 토큰이라 요청·콜백 양쪽에 쓰인다 |
| `MOCK_AI` | `true` | 모델 호출 없이 띄우기. 아래 "모델 키 없이 테스트하기" 참고 |
| `LLM_PROVIDER` | `gemini` | `claude` 로 바꾸면 auth2api 경유 Anthropic. 아래 "Claude 로 돌리기" 참고 |
| `GEMINI_API_KEY` | 비워 둠 | `MOCK_AI=true` 면 필요 없다. `LLM_PROVIDER` 와 무관하게 임베딩이 쓴다 |
| `S3_ENDPOINT` | `http://localhost:9002` (호스트) / `http://minio:9000` (컨테이너) | local-infra MinIO |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | `minioadmin` / `minioadmin` | local-infra 기본 계정 |
| `S3_BUCKET` / `S3_THUMBNAIL_BUCKET` | `pictures` / `thumbnails` | api-server 의 버킷명과 동일 |
| `EMBEDDING_DIM` | `768` | worker 의 `EMBEDDING_DIM`·pgvector `vector(768)` 과 일치해야 한다. 어긋나면 콜백은 가도 worker 가 "임베딩 차원 불일치" 로 벡터를 버린다 |
| `SPRING_ENABLED` | `false` | 아래 "SPRING_ENABLED" 참고 |
| `MAX_CONCURRENT_STAGES` | `4` | 동시 처리 상한 |

### SPRING_ENABLED 는 무슨 값이고, 콜백을 보내려면?

- **현재 값**: `config.py` 의 기본값은 `true`(`SPRING_ENABLED` 미설정 시). 하지만
  `.env.example` 이 `SPRING_ENABLED=false` 로 배포하고 있어 **실제 운영 값은 false** 다.
- **콜백에는 영향이 없다.** `spring_client.post_callback` 은
  `if not spring_enabled and not callback_url:` 일 때만 콜백을 건너뛴다.
  analysis-worker 의 `FastApiAnalysisClient` 는 요청마다 `callbackUrl` 을 실어 보내므로
  **`SPRING_ENABLED=false` 여도 그 주소로 콜백이 나간다.**
- `true` 로 올리면 AGENT 단계에서 10-5 지식 후보 조회
  (`GET {SPRING_BASE_URL}/internal/v1/user/{userId}/knowledge-candidates`)를 매번 시도한다.
  worker 에는 이 엔드포인트가 없어 건당 `SPRING_TIMEOUT`(3초)만 버린다.
  **로컬에서는 `false` 로 두는 게 맞다.**

### Claude 로 돌리기 (LLM_PROVIDER)

`LLM_PROVIDER` 하나로 LLM 호출을 Gemini ↔ Claude 로 바꿔 끼운다. 기본은 `gemini`
라 아무 것도 안 하면 기존 동작 그대로다.

```bash
LLM_PROVIDER=claude
CLAUDE_API_KEY=<auth2api config.yaml 의 api-keys 첫 값>
```

- auth2api(로컬 OAuth 프록시)를 먼저 띄워야 한다: `cd <auth2api> && npm start`.
  살아 있는지는 `curl http://127.0.0.1:8317/health` 로 확인한다.
- `pip install -r requirements.txt` 로 `anthropic` 을 깔아야 한다.
- ⚠️ **호스트에서 uvicorn 으로 띄울 때만 `127.0.0.1:8317` 이 통한다.**
  `docker-compose.local.yml` 로 띄우면 닿지 않고, `host.docker.internal` 로 바꿔도
  안 된다 — auth2api 가 `127.0.0.1:8317` **루프백에만** 바인딩하기 때문이다
  (`config.yaml` 의 `host: ''`). host-gateway 로 나간 요청은 호스트의 브리지 IP 로
  도착해 거부된다. `docker-compose.yml:64` 의 MinIO 주석과 같은 제약이다.
  도커에서 쓰려면 셋 중 하나: (a) auth2api 를 같은 compose 네트워크의 컨테이너로
  올려 `CLAUDE_BASE_URL=http://auth2api:8317`, (b) auth2api 를 `0.0.0.0` 으로 노출,
  (c) AI 서버만 호스트에서 돌린다. 상세는 `.env.example` 의 해당 주석.
- 모델 기본값은 AGENT `claude-sonnet-4-6`, 비전 `claude-haiku-4-5`. 역할별 재정의는
  `CLAUDE_*_MODEL_NAME` 이며, Gemini 쪽 `LLM_MODEL_NAME` 등과 이름공간이 분리되어
  있어 `.env.local` 에 gemini 모델명이 박혀 있어도 새지 않는다.
- ⚠️ **임베딩은 이 값과 무관하게 항상 Gemini** 다. Anthropic 에는 임베딩 엔드포인트가
  없다(`POST /v1/embeddings` → 404). claude 모드에서도 `GEMINI_API_KEY` ·
  `EMBEDDING_DIM`(768)은 그대로 필요하고, worker 의 `EMBEDDING_DIM` 과 계속 맞아야 한다.

배선만 확인하려면(네트워크·SDK 없이 돈다):

```bash
python scripts/llm_check.py
```

프로바이더 디스패치, 모델 이름공간 누출, Anthropic content 블록 변환, 이미지 축소
예산, MOCK 경로를 검증한다.

### 모델 키 없이 테스트하기

원래는 mock 모드가 없었다. 이번에 `MOCK_AI` 를 추가했다.

```bash
MOCK_AI=true      # 모델 키 없이 기동 (LLM_PROVIDER 둘 다 동작)
```

- `generate_json` / `embed` 가 실제 모델을 부르지 않고 가짜 응답을 준다.
  임베딩은 텍스트 해시 기반 결정적 768차 단위 벡터라 매번 같은 값이 나온다.
- **확인되는 것**: S3 읽기, 썸네일 생성·업로드, 3단계 흐름, 색인, 콜백 전송,
  동시성·세마포어 동작.
- **확인 안 되는 것**: 분석 품질(요약·태그·카테고리 판정). 그건 실제 키가 필요하다.
- 원본 이미지는 실제로 내려받아 PIL 로 연다. 즉 MinIO 배선은 mock 에서도 진짜로 검증된다.
- ⚠️ 배포 환경에서 켜지 말 것.

---

## 2. ai-server 띄우기

### 2-A. uvicorn 직접 (호스트 실행)

```bash
cd ai/ai_main
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

set -a && . ./.env.local && set +a
./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

- MinIO 는 `http://localhost:9002` 로 붙는다(.env.local 기본값 그대로).
- 콜백은 `http://localhost:8081/...` 로 보낼 수 있지만, **worker 가 8081 을 노출해야** 한다.

### 2-B. docker compose (권장 — 컨테이너 이름으로 서로 닿는다)

```bash
cd ai/ai_main
docker compose -f docker-compose.local.yml up -d --build
```

`docker-compose.local.yml` 은 ai-server 를 local-infra 가 만든 네트워크
(`local-infra_modera-network`)에 함께 넣는다. 그래서

```
ai-server → MinIO            http://minio:9000
ai-server → analysis-worker  http://analysis-worker:8081
analysis-worker → ai-server  http://ai-server:8000
```

로 통한다. 호스트 포트 매핑에 기대지 않으므로 worker 가 8081 을 안 열어도 된다.
네트워크 이름이 다르면 확인해서 compose 파일의 `name:` 을 고칠 것:

```bash
docker network ls | grep modera
```

Swagger 는 `http://localhost:8000/docs`, health 는 `http://localhost:8000/health`.

### OpenSearch

**FULL 파이프라인은 OpenSearch 없이도 돈다.** 색인 실패는 경고만 남기고 통과한다
(`search.set_status` · `search.index_document` 는 전부 try/except). 검색·목록
API(`/api/v1/*`)까지 보려면:

```bash
docker build -t ai-opensearch-nori:latest -f opensearch.Dockerfile .
docker compose -f docker-compose.local.yml --profile search up -d
```

---

## 3. MinIO 시딩

local-infra 의 MinIO 는 버킷을 자동 생성하지 않는다. FULL 요청이 `s3Key` 로
원본을 읽으므로 미리 넣어야 한다.

```bash
./.venv/bin/python scripts/seed_minio.py --count 4
# 버킷 생성: pictures / thumbnails
# 업로드: s3://pictures/u/1/local-test-1.png ...
```

---

## 4. 테스트

### 4-1. 데드락·파이프라인 확인 (sink 모드)

콜백을 스크립트가 직접 받는다. worker DB 에 job 행이 없어도 되고 콜백 바디를 눈으로 볼 수 있다.

```bash
# ai-server 가 호스트(uvicorn)에서 돌 때
./.venv/bin/python scripts/full_stage_test.py --sink --count 4

# ai-server 가 컨테이너에서 돌 때 (콜백 주소를 host.docker.internal 로 바꾼다)
AI_IN_DOCKER=true ./.venv/bin/python scripts/full_stage_test.py --sink --count 4
```

`--count` 를 `MAX_CONCURRENT_STAGES` 와 같게 두는 것이 데드락 재현 조건이다.

### 4-2. 실제 worker 로 콜백 보내기

worker 는 `analysis_job` 에 없는 `jobId` 면 "콜백 대상 job 없음 → 무시" 로 200 만 주고
끝낸다(`AnalysisCallbackService.handle` 첫 분기). 저장까지 보려면 job 행을 먼저 만든다.

```bash
docker exec modera-analysis-db psql -U analysis_admin -d modera_analysis -c \
"INSERT INTO analysis_job (job_id, user_id, image_id, stage, status, attempt, trigger_type, queued_at, started_at)
 VALUES (7001, 1, 7001, 'FULL', 'PROCESSING', 1, 'INITIAL', now(), now())
 ON CONFLICT (job_id) DO UPDATE SET status='PROCESSING', completed_at=NULL, model_version=NULL;"
```

요청:

```bash
curl -X POST http://localhost:8000/internal/v1/analyze \
  -H 'X-Internal-Token: local-dev-internal-token' \
  -H 'Content-Type: application/json' \
  -d '{
    "jobId": 7001, "imageId": 7001, "userId": 1, "stage": "FULL",
    "input": { "image": { "s3Key": "u/1/local-test-1.png" } },
    "options": { "maxTags": 10, "language": "ko" },
    "callbackUrl": "http://analysis-worker:8081/internal/v1/callback/analysis"
  }'
# → 202 {"jobId":7001,...,"accepted":true,"status":"QUEUED"}
```

확인:

```bash
docker logs -f modera-analysis-worker | grep -i 콜백
docker exec modera-analysis-db psql -U analysis_admin -d modera_analysis -c \
  "SELECT job_id, status, model_version FROM analysis_job WHERE job_id=7001;
   SELECT job_id, image_id, summary, informative, analysis_confidence,
          (embedding IS NOT NULL) AS has_vector FROM analysis_result WHERE job_id=7001;"
```

### 4-3. worker 가 ai-server 를 부르게 하기 (스트림 경로 전체)

`ImageAnalysisConsumer` → `FastApiAnalysisClient` 경로까지 태우려면 worker 를
mock 이 아닌 fastapi 클라이언트로 띄워야 한다. local-infra 의 `analysis-worker`
서비스 environment 에:

```yaml
      ANALYSIS_CLIENT: fastapi
      AI_SERVER_URL: http://ai-server:8000
      CALLBACK_URL: http://analysis-worker:8081/internal/v1/callback/analysis
```

그다음 `image-analysis` 스트림에 `IMAGE_UPLOADED` 이벤트를 넣으면(api-server 로
업로드하거나 Redis 에 직접 XADD) 전 구간이 돈다.

---

## 알려진 함정

- **worker 컨테이너가 오래됐으면 8081 이 안 열린다.** 콜백 컨트롤러
  (`AnalysisCallbackController`)는 최근에 들어왔다. 그 전에 빌드된 이미지는 웹 서버가
  없어 `analysis-worker:8081` 연결이 전부 실패한다. 확인·재빌드:

  ```bash
  docker exec modera-ai-server python -c "import socket;s=socket.socket();s.settimeout(2);print(s.connect_ex(('analysis-worker',8081))==0)"
  # develop/backend 워크트리에서
  cd backend && ./gradlew :analysis-worker:bootJar
  cd local-infra && docker compose --profile app up -d --build analysis-worker
  ```

- **FULL 요청에는 OCR 이 실려 오지 않는다.** `FastApiAnalysisClient` 가 `input` 에
  `image` 만 담는다(`ImageUploadedPayload.clientOcr` 는 있는데 worker 가 전달하지 않음).
  그래서 FULL 은 이미지 분석을 먼저 돌려 거기서 읽어낸 텍스트를 OCR 대용으로 쓴다
  (`stages._run_full_pipeline` 의 `allow_vision_ocr`). worker 가 나중에 `input.ocr` 을
  채워 보내면 그 값이 우선한다 — AI 쪽은 고칠 것이 없다.

- **같은 `(jobId, stage)` 재요청은 200 + `DUPLICATE_JOB`** 이다(202 아님).
  테스트를 다시 돌릴 때는 `--job-id-base` 를 바꾸거나 ai-server 를 재시작할 것
  (`job_registry` 는 프로세스 메모리다).
