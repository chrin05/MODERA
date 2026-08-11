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

        # 자격증명 (기본값 없음)
        # GMS_KEY: SSAFY GMS 프록시용 키. 현재 모든 Gemini 호출이 이걸 쓴다.
        #   Agent Platform 전환 후에도 임베딩은 GMS 를 유지하므로(768차원 pgvector
        #   계약 리스크 회피) 살아남는다. 구 이름은 GEMINI_API_KEY 였다.
        self.gms_key = "" if self.mock_ai else _required("GMS_KEY")
        # GEMINI_API_KEY: Agent Platform(구 Vertex AI) 표준 모드용 키.
        #   생성 호출(generate_content) 전부가 이걸 쓴다. 인증이 API 키라
        #   서비스 계정 JSON·GOOGLE_APPLICATION_CREDENTIALS 가 필요 없다.
        #   SDK 가 이 이름의 환경변수를 자동으로 집는데(get_env_api_key),
        #   enterprise 모드에서 필요한 키가 바로 이거라 이름이 맞아떨어진다.
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
        # BM25 질의 스트립(F4c). 문장형 질의의 무변별 토큰을 BM25 질의에서 걷어낸다.
        # "싸피와 관련된 사진"의 '관련된·사진'은 스크린샷 라이브러리에서 변별력이
        # 0인데 minimum_should_match 의 분모만 부풀려 정답을 차단한다(실측 8/08:
        # nori 8토큰 중 75%=6개 요구, 정답 문서엔 3개 → 0건. '관련된·사진'을 걷으면
        # 75% 그대로 정답만 1건). 시맨틱 임베딩은 항상 **원문**을 쓴다 — 문맥은
        # 벡터에 도움이 된다. 쉼표 구분, 빈 값이면 스트립을 끈다.
        self.search_query_stopwords = frozenset(
            w.strip() for w in os.environ.get(
                "SEARCH_QUERY_STOPWORDS",
                "사진,사진들,이미지,스크린샷,스샷,캡처,캡쳐,화면,"
                "관련,관련된,관련한,관한,대한,대해,대해서,"
                "찾아줘,찾아봐,찾아,검색,검색해,검색해줘,보여줘,보여줘요,"
                "알려줘,알려줘요,좀",
            ).split(",") if w.strip()
        )
        # 표기 별칭. 같은 대상을 한글 음차와 영문으로 달리 적은 문서를 잇는다.
        # bge-m3 는 음차를 원문과 연결하지 못한다(실서버 실측 8/11: 질의 "싸피" vs
        # SSAFY 문서 코사인 0.311, 무관 문서 0.274 — 마진 0.003 이라 임계값으로
        # 못 가른다). 그래서 시맨틱이 아니라 **질의측 치환**으로 푼다: 목록에 있는
        # 쌍만 잡는 의도적 절충이고, 재색인·재분석이 필요 없다는 것이 이 방식의 값어치다.
        # `가:나` 쌍을 쉼표로 나열하며 양방향으로 적용한다. 빈 값이면 끈다.
        # 근본 해법은 분석 프롬프트가 태그에 두 표기를 함께 넣는 것(기존 이미지는 재분석 필요).
        self.search_query_aliases = tuple(
            (a.strip(), b.strip())
            for a, _, b in (
                p.partition(":")
                for p in os.environ.get("SEARCH_QUERY_ALIASES", "싸피:SSAFY").split(",")
            )
            if a.strip() and b.strip()
        )
        # 하이브리드 후보풀 BM25 의 완화 msm. 풀은 리콜 담당이고 정밀도는 융합 전
        # 코사인 게이트가 지키므로 본검색(SEARCH_MIN_SHOULD_MATCH)보다 느슨하게
        # 잡는다(기본 "1" = 토큰 하나만 걸려도 후보). 임베딩 없는 문서는 게이트를
        # 면제받으므로 strict 매칭을 따로 요구해 이 완화의 영향권 밖에 둔다
        # (search._bm25_pool_body 의 named query 참고).
        self.search_hybrid_pool_msm = os.environ.get("SEARCH_HYBRID_POOL_MSM", "1")
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
        # cascade 승격 보강(F4b). 기존 "BM25 빈결과일 때만 승격"은 BM25 가 오탐을 잡아도
        # 승격을 막는다 — 실측(8/04, 24질의): "시험 신청한 내역"에 BM25 9건 전부 오탐,
        # 정답은 kNN 에 있는데 기회가 없었다. BM25 최고 _score 가 이 값 미만이면 결과가
        # 있어도 하이브리드로 보강한다. 0.0 = 기존 동작(빈결과만 승격, 하위호환 기본).
        self.search_cascade_min_top_score = float(
            os.environ.get("SEARCH_CASCADE_MIN_TOP_SCORE", "0.0")
        )

        # 모델
        # GMS 프록시가 모델을 화이트리스트로 막는다. 허용 확인된 값:
        # gemini-2.5-flash-lite / gemini-2.5-flash / gemini-2.5-pro / gemini-3.5-flash
        # (gemini-3.5-flash-lite 는 400 "Model is not available")
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
        self.informative_model_name = (
            os.environ.get("INFORMATIVE_MODEL_NAME") or self.llm_model_name)
        # 문서 생성(명세 11장) 전용 모델. 사용자 대면 장문이라 품질 우선 —
        # 대량 분석(AGENT)을 싼 모델로 내려도 문서 품질은 따로 지킨다.
        # 호출이 희소해 단가가 사실상 무의미한 경로다. 미설정 시 AGENT 를 따른다.
        self.document_model_name = (
            os.environ.get("DOCUMENT_MODEL_NAME") or self.llm_model_name)
        # 자연어 검색 조건 변환(10-3) 전용 모델. 키워드·가격·날짜 추출 수준이라
        # 최경량이면 충분하고, 검색은 사용자가 기다리는 경로라 지연에 민감하다.
        # AGENT 모델을 따라가게 두면 검색마다 상위 모델 단가·추론 토큰을 내게 된다
        # (GMS 실측 계기 — 2026-08-03). 미설정 시 AGENT 를 따른다(기존 동작 호환).
        self.query_parse_model_name = (
            os.environ.get("QUERY_PARSE_MODEL_NAME") or self.llm_model_name)
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

        # ── 카테고리 아이콘 생성 (OpenAI Images) ──────────────────────────
        # Gemini 경로와 완전히 분리한다. SSAFY GMS 프록시는 이미지 생성 모델을
        # 중계하지 않으므로 OpenAI 공식 엔드포인트로 직접 붙는다(팀 결정).
        # 키를 필수로 두지 않는다 — 아이콘은 부가 기능이라 미설정이 기동 실패가
        # 되면 안 된다. 실제 생성 시점에만 에러로 알린다(app/category_icon.py).
        self.openai_api_key = os.environ.get("OPENAI_API_KEY", "")
        self.openai_base_url = os.environ.get(
            "OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.image_model_name = os.environ.get("IMAGE_MODEL_NAME", "gpt-image-1-mini")
        # 생성 요청 크기. gpt-image-1 계열은 size 를 네 값만 받는다 —
        # 1024x1024 / 1024x1536 / 1536x1024 / auto. 216 을 보내면 400 이라
        # 받아서 줄이는 것 말고 방법이 없다(임의 크기는 dall-e-2 까지였다).
        # 정사각으로 둬야 216 정사각으로 줄일 때 크롭 손실이 없다.
        self.image_gen_size = os.environ.get("IMAGE_GEN_SIZE", "1024x1024")
        # low 로 시작한다. 아이콘은 단순 픽토그램이라 품질 단가를 올릴 이유가 없다.
        self.image_gen_quality = os.environ.get("IMAGE_GEN_QUALITY", "low")
        # 이미지 생성은 수십 초가 걸린다. Gemini 타임아웃과 분리한다.
        self.openai_timeout = float(os.environ.get("OPENAI_TIMEOUT", "120"))
        # 아이콘 한 변(px). 앱 카테고리 아이콘 규격.
        self.category_icon_size = int(os.environ.get("CATEGORY_ICON_SIZE", "216"))
        # 아이콘 전용 버킷. key 는 "{categoryId}.png" 하나뿐이라 매핑 테이블이 없다.
        self.s3_category_icon_bucket = os.environ.get(
            "S3_CATEGORY_ICON_BUCKET", "category-thumbnails")
        # 신규 카테고리일 때 분석 완료 콜백을 아이콘 생성만큼 늦추는 상한(초).
        # 이 콜백이 앱에게는 '카테고리 생겼다' 신호라, 아이콘보다 먼저 나가면 앱이
        # 빈 아이콘을 한 번 조회하고 끝난다(app/category_icon.py). 실제 생성 지연에
        # 맞춰 조정하는 값이다 — 모델·품질·리전이 바뀌면 여기부터 만진다.
        # 0 이면 기다리지 않는다. 기존 카테고리 분석은 이 값과 무관하게 즉시 통과.
        self.category_icon_wait_seconds = float(
            os.environ.get("CATEGORY_ICON_WAIT_SECONDS", "30"))

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

        # Agent Platform(구 Vertex AI) 대상 프로젝트/리전 — 생성 호출 경로.
        # 둘을 주면 요청 경로에 projects/{p}/locations/{l}/ 가 붙어 표준 모드가
        # 된다(생략하면 express mode). SDK 가 location 을 보고 호스트를 고른다:
        #   global → aiplatform.googleapis.com
        #   us/eu  → aiplatform.{loc}.rep.googleapis.com
        #   그 외  → {loc}-aiplatform.googleapis.com
        # 리전을 바꾸면 모델 가용성과 쿼터도 같이 바뀐다. global 로 실측 확인했다.
        # 프로젝트는 번호(741934320291)와 문자열 ID 둘 다 받는다.
        self.gcp_project = "" if self.mock_ai else _required("GCP_PROJECT")
        self.gcp_location = os.environ.get("GCP_LOCATION") or "global"

        # 임베딩 호출 엔드포인트. SSAFY GMS 프록시를 경유한다.
        # SDK 가 이 값 뒤에 `/v1beta/models/...` 를 붙이므로 프록시가 요구하는
        # 업스트림 호스트까지 포함한 전체 접두사를 넣는다.
        # 경로 접두사(/gmsapi/...)는 gRPC 로 표현할 수 없어 gemini_client 가
        # transport="rest" 로 고정한다.
        # ⚠️ 생성 경로는 이 값을 쓰지 않는다 — Agent Platform 은 SDK 가 호스트를
        # 조립하고, 여기에 값을 물려주면 그 조립을 덮어쓴다. GMS_KEY 와 짝이다.
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
