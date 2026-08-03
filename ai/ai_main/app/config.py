"""환경변수 기반 설정.

API 키·내부 토큰은 기본값을 두지 않는다. 미설정 시 즉시 실패시켜
자격증명이 코드에 남는 일을 막는다.
"""

import os
from functools import lru_cache


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"환경변수 {name} 가 설정되지 않았습니다.")
    return value


class Settings:
    def __init__(self) -> None:
        # 모델 호출을 전부 가짜 응답으로 대체한다(로컬 배선 점검용).
        # Gemini 를 부르지 않으므로 API 키 없이 서버를 띄울 수 있고, 파이프라인
        # 왕복(S3 읽기 → 3단계 → 콜백)과 동시성 동작을 키 소모 없이 확인할 수 있다.
        # ⚠️ 분석 품질은 확인할 수 없다. 배포 환경에서는 절대 켜지 말 것.
        self.mock_ai = os.environ.get("MOCK_AI", "false").lower() == "true"

        # LLM 프로바이더 토글 — 이 값 하나로 Gemini ↔ Claude 를 바꿔 끼운다.
        # 해석은 app/llm/__init__.py 가 하고, 아래 *_model_name 이 프로바이더별
        # 이름으로 미리 확정된다. 호출부는 프로바이더를 모른다.
        #
        # ⚠️ 임베딩은 이 값과 무관하게 **항상 Gemini** 다 — Anthropic 에는 임베딩
        # 엔드포인트가 없다(auth2api 경유 POST /v1/embeddings → 404, 2026-08-04 실측).
        # 그래서 GEMINI_API_KEY 는 claude 모드에서도 여전히 필수다.
        self.llm_provider = (
            os.environ.get("LLM_PROVIDER", "gemini").strip().lower() or "gemini")
        if self.llm_provider not in ("gemini", "claude"):
            raise RuntimeError(
                f"LLM_PROVIDER 는 gemini 또는 claude 여야 합니다: {self.llm_provider!r}")
        _claude = self.llm_provider == "claude"

        # 자격증명 (기본값 없음)
        # embed 가 프로바이더와 무관하게 Gemini 를 쓰므로 항상 요구한다.
        self.gemini_api_key = "" if self.mock_ai else _required("GEMINI_API_KEY")
        self.internal_token = _required("INTERNAL_TOKEN")

        # Spring 내부 API (10-4 콜백, 10-5 후보 조회)
        self.spring_base_url = os.environ.get("SPRING_BASE_URL", "http://spring-api:8080")
        # Spring 연동 스위치. MVP 는 앱↔AI 직결이라 Spring 이 떠 있지 않은데,
        # 그 상태로 10-5 를 매 건 호출하면 연결 실패까지 약 4초를 그냥 버린다.
        # false 로 두면 조회를 시도하지 않고 기본 후보로 바로 진행한다.
        self.spring_enabled = os.environ.get("SPRING_ENABLED", "true").lower() == "true"
        # ponytail: Spring 완성 전까지의 임시 스위치. 지금은 Android 가 /api/v1 을
        # 직접 부르는데 앱에는 X-Internal-Token 을 넣을 수 없어 인증을 끈다.
        # Spring 이 앞단에 서면 APP_API_AUTH=true 로 되돌리고, 실제 로그인이
        # 붙으면 이 스위치와 require_internal_token 의 분기를 함께 지운다.
        # /internal/v1/* 은 이 값과 무관하게 항상 토큰을 요구한다.
        self.app_api_auth = os.environ.get("APP_API_AUTH", "false").lower() == "true"
        # Spring 내부 호출 타임아웃(초). Gemini 용 gemini_timeout 과 분리한다.
        # 같은 네트워크 안의 내부 호출이라 길게 잡을 이유가 없다.
        self.spring_timeout = float(os.environ.get("SPRING_TIMEOUT", "3"))

        # 오브젝트 스토리지 (원본 이미지). MinIO 는 S3 호환이라 같은 클라이언트를 쓴다.
        # endpoint 를 지정하면 MinIO, 비우면 AWS S3 로 붙는다.
        self.s3_bucket = os.environ.get("S3_BUCKET", "")
        self.s3_endpoint = os.environ.get("S3_ENDPOINT", "")
        self.s3_access_key = os.environ.get("S3_ACCESS_KEY", "")
        self.s3_secret_key = os.environ.get("S3_SECRET_KEY", "")
        # MinIO 는 path-style 접근이 필요하다(버킷을 호스트명이 아닌 경로로 붙임).
        self.s3_path_style = os.environ.get("S3_PATH_STYLE", "true").lower() == "true"
        self.aws_region = os.environ.get("S3_REGION", os.environ.get("AWS_REGION", "us-east-1"))

        # 썸네일 최대 변(px). 0 이면 축소하지 않고 해상도를 원본 그대로 둔다
        # (정사각으로 모양만 맞추는 용도).
        self.thumbnail_max_size = int(os.environ.get("THUMBNAIL_MAX_SIZE", "0"))
        self.thumbnail_quality = int(os.environ.get("THUMBNAIL_QUALITY", "82"))
        # 썸네일 전용 버킷. 분석할 때 리사이즈본을 만들어 **원본과 같은 key** 로 올려 둔다.
        # (원본 pictures/u/1/a.png → 썸네일 thumbnail/u/1/a.png, 내용은 JPEG)
        # 비워 두면 저장하지 않고 요청 때마다 원본을 읽어 즉석 생성한다(기존 동작).
        self.s3_thumbnail_bucket = os.environ.get("S3_THUMBNAIL_BUCKET", "thumbnail")
        # 스토리지 호출 타임아웃(초)과 재시도 횟수.
        # 기본값(연결 60초 × 재시도 5회)이면 MinIO 가 잠깐 내려갔을 때 목록·썸네일
        # 요청이 몇 분씩 매달린다. 짧게 끊고 에러를 돌려주는 편이 낫다.
        self.s3_connect_timeout = float(os.environ.get("S3_CONNECT_TIMEOUT", "3"))
        self.s3_read_timeout = float(os.environ.get("S3_READ_TIMEOUT", "10"))
        self.s3_max_attempts = int(os.environ.get("S3_MAX_ATTEMPTS", "2"))

        # 업로드 허용 최대 크기(MB). 명세 4-1 fileSize 는 최대 5MB 다.
        self.max_upload_mb = int(os.environ.get("MAX_UPLOAD_MB", "5"))
        # presigned 업로드 URL 유효시간(초). 명세 4-1 uploadExpiresIn 예시가 600 이다.
        self.upload_url_expires_in = int(os.environ.get("UPLOAD_URL_EXPIRES_IN", "600"))
        # 앱에 건네줄 presigned URL 을 만들 때 쓰는 공개 주소.
        #
        # presigned URL 은 S3_ENDPOINT 값을 host 로 그대로 박아서 만들어진다. 그런데
        # 서버 내부용 주소(http://minio:9000)는 휴대폰에서 접근할 수 없다. 그래서
        # 읽기·쓰기는 내부 주소로 하고, 앱에 주는 URL 만 이 공개 주소로 만든다.
        # 비워 두면 S3_ENDPOINT 를 그대로 쓴다(로컬 개발·터널 환경).
        self.s3_public_endpoint = os.environ.get("S3_PUBLIC_ENDPOINT", "")

        # 조회용 이미지 주소를 presigned URL 로 줄지 여부.
        #
        # 업로드는 presigned 가 필수다(앱이 스토리지에 직접 올려야 하므로).
        # 하지만 조회까지 presigned 로 하면 만료 때문에 손해가 크다:
        #   - 서명이 매번 바뀌어 앱 이미지 캐시가 URL 기준이면 같은 사진을 계속 다시 받는다
        #   - 목록 응답을 캐시해 두고 나중에 열면 이미지가 깨진다
        # 그래서 기본은 만료 없는 서버 경유 경로(/thumbnail/raw, /source)다.
        # 이미지 트래픽을 서버에서 덜어내고 싶어지면 true 로 바꾼다.
        self.presigned_read_urls = (
            os.environ.get("PRESIGNED_READ_URLS", "false").lower() == "true"
        )

        # 썸네일을 정사각으로 만들지 여부. 카테고리 카드·격자 목록이 모두 정사각이라
        # 서버에서 잘라 두면 앱이 자를 필요가 없다. false 면 비율을 유지한다.
        self.thumbnail_square = (
            os.environ.get("THUMBNAIL_SQUARE", "true").lower() == "true"
        )

        # OpenSearch (검색 색인/조회 — FastAPI 전담, 한글 nori analyzer)
        self.opensearch_host = os.environ.get("OPENSEARCH_HOST", "localhost")
        self.opensearch_port = int(os.environ.get("OPENSEARCH_PORT", "9200"))
        self.opensearch_use_ssl = os.environ.get("OPENSEARCH_USE_SSL", "false").lower() == "true"
        self.opensearch_user = os.environ.get("OPENSEARCH_USER", "")
        self.opensearch_password = os.environ.get("OPENSEARCH_PASSWORD", "")
        self.opensearch_index = os.environ.get("OPENSEARCH_INDEX", "screenshot_kb")
        # BM25 과다매칭 방지. nori 가 "5만원" 을 5/만/원 으로 쪼개면 "원" 하나만 걸린
        # 문서까지 딸려온다. 질의 토큰의 이 비율 이상이 있는 문서만 매칭한다.
        # OpenSearch minimum_should_match 표기("75%", "2<75%" 등)를 그대로 받는다.
        self.search_min_should_match = os.environ.get("SEARCH_MIN_SHOULD_MATCH", "75%")
        # 검색용 로컬 임베딩(자연어/시맨틱 검색). 카테고리 판정용 gemini-embedding-2(768,
        # pgvector)와는 완전히 별개다. knn_vector 필드 차원은 이 모델 출력과 일치해야 한다.
        # bge-m3 는 dense 1024 차원. 색인과 질의를 반드시 같은 모델로 임베딩해야 비교가 성립한다.
        self.search_embedding_model = os.environ.get("SEARCH_EMBEDDING_MODEL", "BAAI/bge-m3")
        self.search_embedding_dim = int(os.environ.get("SEARCH_EMBEDDING_DIM", "1024"))

        # F3 하이브리드 검색 (BM25 + kNN 시맨틱, RRF 순위결합 + 절대 임계값 컷).
        # relevance 정렬·scope=ALL 에서만 켜진다. 임베딩 모델이 없으면 자동으로 BM25 로 폴백한다.
        self.search_hybrid_enabled = (
            os.environ.get("SEARCH_HYBRID_ENABLED", "true").lower() == "true"
        )
        # RRF 상수. 순위결합에서 1/(k+rank) 의 k. 관례값 60. 상위 급경사를 원하면 낮춘다.
        self.search_rrf_k = int(os.environ.get("SEARCH_RRF_K", "60"))
        # 후보풀 크기 = kNN k. page 와 무관한 고정값이라 페이지를 넘겨도 total 이 흔들리지 않는다.
        # 융합 total 은 이 값의 약 2배(BM25∪kNN)를 상한으로 가진다. 코퍼스가 커지면 늘린다.
        self.search_hybrid_pool_size = int(os.environ.get("SEARCH_HYBRID_POOL_SIZE", "200"))
        # 억지 매칭 차단의 핵심. 재계산 코사인(단위벡터 내적)이 이 값 미만인 후보는 버린다.
        # 살아남는 후보가 없으면 "결과 없음".
        # 캘리브레이션(잠정): 두 A/B 코퍼스 관측 — 합성 12문서(관련 0.52~0.64 / 무관 0.44~0.45),
        # demo 8문서(관련 0.45~0.58 / 무관 ≤0.42). 0.50 은 짧은 자연어 질의를 과하게 컷해
        # (demo 에서 대부분 빈 결과) 0.45 로 낮췄다(정밀도 우선 상단값).
        # 실서버 실데이터로 최종 재확정 필요(0.43~0.50 구간 탐색).
        self.search_knn_min_cosine = float(os.environ.get("SEARCH_KNN_MIN_COSINE", "0.45"))
        # 임베딩이 없는 레거시 문서(빈 OCR·F2 이전)에만 적용하는 어휘 컷. 기본 off(하위호환).
        self.search_bm25_min_score = float(os.environ.get("SEARCH_BM25_MIN_SCORE", "0.0"))

        # 모델
        #
        # 두 프로바이더는 모델 카탈로그가 겹치지 않으므로 **env 이름공간도 분리**한다
        # (claude 쪽은 CLAUDE_ 접두사). 팀 .env 에 이미
        # LLM_MODEL_NAME=gemini-2.5-flash-lite 가 박혀 있어도 LLM_PROVIDER 만 바꾸면
        # 되고, gemini 모델명이 Anthropic 으로 새어 404 나는 일이 없다.
        #
        # [gemini] GMS 프록시가 모델을 화이트리스트로 막는다. 허용 확인된 값:
        # gemini-2.5-flash-lite / gemini-2.5-flash / gemini-2.5-pro / gemini-3.5-flash
        # (gemini-3.5-flash-lite 는 400 "Model is not available")
        #
        # [claude] 4-6 계열로 고정한다. 2026-08-04 실측에서 sonnet-5 는 같은 요청에
        # 529/503 을 3연속 냈고 sonnet-4-6·haiku-4-5 는 1차 성공이었다. 529 는
        # 업스트림 용량 문제라 코드로 없앨 수 없어 모델 선택으로 회피한다.
        # 비전은 haiku-4-5 — 최경량이면서 실측 신뢰도가 가장 높았고, 이 파이프라인은
        # 비전 모델에 정밀 OCR 을 요구하지 않는다(글자는 기기 OCR 이 원본 해상도로
        # 따로 넣고, 모델은 레이아웃·색·로고 같은 시각 신호만 쓴다).
        if _claude:
            self.llm_model_name = (
                os.environ.get("CLAUDE_LLM_MODEL_NAME") or "claude-sonnet-4-6")
            self.vision_model_name = (
                os.environ.get("CLAUDE_VISION_MODEL_NAME") or "claude-haiku-4-5")
        else:
            self.llm_model_name = os.environ.get("LLM_MODEL_NAME", "gemini-2.5-flash-lite")
            self.vision_model_name = os.environ.get("VISION_MODEL_NAME", "gemini-2.5-flash-lite")
        # 정보성 판별(EMPTY 거르기) 전용 모델 스위치. AGENT(LLM_MODEL_NAME)를 상위
        # 모델로 올릴 때 이 호출까지 따라 올라가 비용이 2배가 되는 것을 막는 용도
        # (GMS 실측 2026-07-31: 장당 67크레딧 중 절반이 이 호출).
        # ⚠️ 기본값은 안전하게 AGENT 모델을 따라간다 — GMS 의 2.5-flash-lite 는
        # 비정보성 화면(계산기·배터리 통계)을 통과시켰고, 그 누수가 AGENT 의
        # "기타 금지" 규칙과 결합하면 '계산'·'스마트폰' 같은 쓰레기 카테고리가
        # 생긴다(실측). 프롬프트 보강 후에도 4장 중 1장(배터리) 누수. lite 절감은
        # 실데이터 누수율 측정 후 INFORMATIVE_MODEL_NAME 로 명시 옵트인할 것.
        # 프로바이더별 env 이름공간(위 [모델] 주석 참고). claude 모드에서는
        # CLAUDE_INFORMATIVE_MODEL_NAME 등으로 개별 재정의한다.
        def _role_model(env: str) -> str:
            return os.environ.get(f"CLAUDE_{env}" if _claude else env) or self.llm_model_name

        self.informative_model_name = _role_model("INFORMATIVE_MODEL_NAME")
        # 문서 생성(명세 11장) 전용 모델. 사용자 대면 장문이라 품질 우선 —
        # 대량 분석(AGENT)을 싼 모델로 내려도 문서 품질은 따로 지킨다.
        # 호출이 희소해 단가가 사실상 무의미한 경로다. 미설정 시 AGENT 를 따른다.
        self.document_model_name = _role_model("DOCUMENT_MODEL_NAME")
        # 자연어 검색 조건 변환(10-3) 전용 모델. 키워드·가격·날짜 추출 수준이라
        # 최경량이면 충분하고, 검색은 사용자가 기다리는 경로라 지연에 민감하다.
        # AGENT 모델을 따라가게 두면 검색마다 상위 모델 단가·추론 토큰을 내게 된다
        # (GMS 실측 계기 — 2026-08-03). 미설정 시 AGENT 를 따른다(기존 동작 호환).
        self.query_parse_model_name = _role_model("QUERY_PARSE_MODEL_NAME")
        # 임베딩은 LLM_PROVIDER 와 무관하게 항상 Gemini 다(Anthropic 에 엔드포인트 없음).
        self.embedding_model_name = os.environ.get("EMBEDDING_MODEL_NAME", "gemini-embedding-2")
        # 임베딩 차원. 팀 합의로 768 고정이며 pgvector 컬럼(vector(768))과 일치해야 한다.
        # gemini-embedding-2 의 기본 출력은 3072 라 호출 시 명시적으로 줄여서 받는다.
        # pgvector 는 2000 초과 차원에 인덱스를 만들 수 없어 3072 는 쓰지 않는다.
        self.embedding_dim = int(os.environ.get("EMBEDDING_DIM", "768"))

        # (구) 요약↔centroid 흡수 관문 임계값 — 2026-07-31 관문 교체로 판정에는
        # 더 이상 쓰지 않는다. 엣지 40장 실측에서 이 관문이 정당한 신규 제안
        # 10건(부동산·뷰티·자동차 등)을 전부 흡수해 "반드시 새 카테고리가 나오게"
        # 팀 합의를 깨뜨렸다. 요약↔centroid 코사인은 주제가 아니라 문장 스타일
        # 유사도를 재서, 러프한 카테고리일수록 아무 요약과도 0.63~0.80 이 나온다.
        # env 호환(팀 .env 에 이미 존재)을 위해 필드만 유지한다.
        self.similarity_threshold = float(os.environ.get("SIMILARITY_THRESHOLD", "0.62"))
        # 카테고리 이름 중복 관문(임베딩 백스톱) — AGENT 가 후보에 없는 새 이름을
        # 제안했을 때, 표기 변형(포함 관계, category.py 의 결정적 규칙)이 아니면서
        # 이름 임베딩 코사인이 이 값 이상인 초근접 동의어만 흡수한다.
        # 기본 0.90: 어휘 92종 4186쌍 전수 실측(2026-07-31) — 0.80 부근에는
        # 형제 개념(야구~축구 0.853, 부동산~주식 0.851, 오토바이~자전거 0.880)이
        # 즐비해 어휘가 늘수록 오병합이 커진다. 0.90 이상은 카페~커피(0.903),
        # 캠핑~캠핑카(0.906, 포함 규칙이 먼저 잡음)뿐. 단어 임베딩은 동의어가
        # 아니라 관련성을 재므로 이 값은 낮추지 말 것 — 의미 중복의 1차 방어는
        # 프롬프트 재사용 규칙이다(실사진 55장 파편화 0 실측).
        self.category_name_dup_threshold = float(
            os.environ.get("CATEGORY_NAME_DUP_THRESHOLD", "0.90"))
        # 카테고리 오염 가드 — 이름 일치 판정이라도 그 카테고리 centroid 와의
        # 감사 코사인이 이 값 미만이면 centroid 에 누적하지 않는다. 카테고리
        # 수동 수정이 없는 제품이라 오분류가 쌓이면 자가 교정 경로가 없다
        # (동의서→금융 오염 실측). 0 이면 가드 끔.
        self.category_guard_min_cosine = float(os.environ.get("CATEGORY_GUARD_MIN_COSINE", "0.45"))

        # AGENT 태그 최대 개수 (10-1 options.maxTags 로 요청별 덮어쓰기 가능)
        self.default_max_tags = int(os.environ.get("DEFAULT_MAX_TAGS", "5"))

        # 로그인 미구현 구간용 고정 userId.
        # MVP 는 회원가입·로그인이 없어서 요청이 보내는 userId 를 신뢰할 수 없다.
        # 값이 있으면 요청 값을 무시하고 항상 이 값으로 처리한다. API 계약(userId 파라미터)은
        # 그대로 두므로, 로그인이 붙으면 0 으로 바꾸는 것만으로 실제 값 사용으로 돌아간다.
        self.fixed_user_id = int(os.environ.get("FIXED_USER_ID", "1"))

        # 외부 호출 타임아웃(초)
        self.http_timeout = float(os.environ.get("HTTP_TIMEOUT", "30"))

        # 동시 실행 단계 수 제한.
        # 수백 장이 한꺼번에 들어와도 Gemini 호출이 폭주(429)하지 않게 막는다.
        self.max_concurrent_stages = int(os.environ.get("MAX_CONCURRENT_STAGES", "4"))

        # Gemini 호출 엔드포인트. SSAFY GMS 프록시를 경유한다.
        # SDK 가 이 값 뒤에 `/v1beta/models/...` 를 붙이므로 프록시가 요구하는
        # 업스트림 호스트까지 포함한 전체 접두사를 넣는다.
        # 경로 접두사(/gmsapi/...)는 gRPC 로 표현할 수 없어 gemini_client 가
        # transport="rest" 로 고정한다.
        # 빈 값(GEMINI_BASE_URL=)도 기본값으로 떨어뜨린다. env_file 은 빈 문자열을
        # "설정됨" 으로 넘기므로 os.environ.get 의 기본값이 먹지 않는다.
        self.gemini_base_url = (
            os.environ.get("GEMINI_BASE_URL")
            or "https://gms.ssafy.io/gmsapi/generativelanguage.googleapis.com"
        ).rstrip("/")

        # Gemini thinking(내부 추론) 제어 — thinking 토큰은 **출력 단가로 과금**된다.
        # 세대별로 파라미터가 다르고(2.5=budget 정수, 3.x=level 문자열), 한 요청에
        # 둘을 같이 보내면 400 이라 gemini_client 가 모델명을 보고 하나만 고른다.
        #
        # GEMINI_THINKING_BUDGET (2.5 계열): 토큰 수. 0=끔, 음수=미전송(모델 기본값).
        #   2.5-flash 는 기본 ON(dynamic)이라 0 으로 꺼야 절감이 완성된다.
        #   ⚠️ 2.5-pro 는 끌 수 없다(최소 128) — pro 를 쓰면 음수 또는 128 이상으로.
        # GEMINI_THINKING_LEVEL (3.x 계열): minimal/low/medium/high. 빈 값=미전송.
        #   3.5-flash 기본값은 medium — 문서 생성처럼 품질 우선 경로는 빈 값이 안전.
        self.gemini_thinking_budget = int(os.environ.get("GEMINI_THINKING_BUDGET", "0"))
        self.gemini_thinking_level = os.environ.get("GEMINI_THINKING_LEVEL", "")

        # Gemini 429(rate limit) 재시도. 지수 백오프 + 지터로 재시도한다.
        self.gemini_max_attempts = int(os.environ.get("GEMINI_MAX_ATTEMPTS", "5"))
        self.gemini_backoff_base = float(os.environ.get("GEMINI_BACKOFF_BASE", "1.0"))
        # Gemini 가 40초 이상 대기를 요구하는 경우가 있어 상한을 넉넉히 둔다.
        self.gemini_backoff_max = float(os.environ.get("GEMINI_BACKOFF_MAX", "60"))
        # Gemini 호출 1회당 타임아웃(초). 없으면 SDK 가 무제한으로 기다려서, 응답이
        # 오지 않는 요청 하나가 asyncio.to_thread 스레드를 영구 점유한다.
        # nginx 의 proxy_read_timeout(120s) 안에 재시도 여유까지 두고 90 으로 잡는다.
        self.gemini_timeout = float(os.environ.get("GEMINI_TIMEOUT", "90"))

        # ── Claude (LLM_PROVIDER=claude 일 때만 쓰인다) ────────────────────
        # 기본 경로는 로컬 auth2api(OAuth 프록시). 비우면 SDK 기본 호스트로 직결한다.
        self.claude_base_url = (
            os.environ.get("CLAUDE_BASE_URL", "http://127.0.0.1:8317").rstrip("/"))
        # claude 모드가 아니면 요구하지 않는다 — gemini 사용자는 아무 것도 추가로
        # 설정할 필요가 없다. auth2api 를 쓰면 그 config.yaml 의 api-keys 값이다.
        self.claude_api_key = (
            "" if (self.mock_ai or not _claude) else _required("CLAUDE_API_KEY"))
        # 출력 상한. **목표치가 아니라 천장**이라 넉넉히 잡아도 비용이 늘지 않는다
        # (모델은 end_turn 에서 멈춘다). 문서 생성(명세 11장)은 다구간 장문이라
        # 8K 면 잘릴 수 있고, 비스트리밍은 ~16K 까지가 SDK HTTP 타임아웃 안쪽이다.
        self.claude_max_tokens = int(os.environ.get("CLAUDE_MAX_TOKENS", "16000"))
        # ⚠️ anthropic SDK 의 timeout 은 **초**다(google-genai 신 SDK 는 밀리초).
        self.claude_timeout = float(os.environ.get("CLAUDE_TIMEOUT", "90"))
        # SDK 가 408/409/429/5xx(529 overloaded, auth2api 의 503 포함)를 지수
        # 백오프로 자동 재시도한다. gemini 쪽 수동 재시도 루프에 대응하는 값.
        self.claude_max_retries = int(os.environ.get("CLAUDE_MAX_RETRIES", "5"))
        # 이미지 입력 예산(긴 변 px 상당, 실제로는 면적 기준 — llm/claude._fit 참고).
        # sonnet-4-6·haiku-4-5 는 1568px 계층이다. opus-4-7 이상이나 sonnet-5 로
        # 올릴 때 2576 으로 함께 올린다.
        self.claude_image_long_edge = int(
            os.environ.get("CLAUDE_IMAGE_LONG_EDGE", "1568"))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
