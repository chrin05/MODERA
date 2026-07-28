"""내부 API 스키마 (명세 10-1 ~ 10-5).

파이썬 내부는 snake_case, 직렬화·역직렬화는 camelCase 별칭으로 처리한다.
Spring 과 주고받는 JSON 은 모두 camelCase 가 된다.
"""

from typing import Any, Generic, List, Literal, TypeVar

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        protected_namespaces=(),
    )


# ── 앱 API 공통 응답 (명세 1.1) ───────────────────────────────────────────
# 실제 응답은 responses.py 가 만들지만, 이 모델을 엔드포인트의 response_model 로
# 선언해야 Swagger 에 응답 구조가 나온다. 선언이 없으면 스키마가 비어(`{}`) 나와
# 프론트가 무엇을 받는지 문서만 봐서는 알 수 없다.
T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    code: str = "SUCCESS"
    message: str = "요청이 성공했습니다."
    data: T | None = None
    timestamp: str = "2026-07-16T06:00:00.000Z"


class PageData(CamelModel, Generic[T]):
    """명세 1.1 페이지 응답의 data 부분. 구조가 고정이다."""

    # 필드명이 명세상 `list` 라 클래스 안에서 내장 list 를 가린다. pydantic 이
    # 나중에 타입을 다시 해석할 때 `list` 가 이 필드를 가리켜 깨지므로
    # 타입은 가려지지 않는 typing.List 로 쓴다.
    list: List[T] = []
    page: int = 0
    size: int = 20
    total_elements: int = 0
    total_pages: int = 0
    first: bool = True
    last: bool = True
    has_next: bool = False
    has_previous: bool = False


# ── 10-1 단계별 분석 실행 요청 ────────────────────────────────────────────
class OcrInput(CamelModel):
    raw_text: str = ""
    refined_text: str | None = None
    lang: str | None = None
    confidence: float | None = None

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        protected_namespaces=(),
        json_schema_extra={
            "example": {
                "rawText": "오후 4:20 85% 교보문고 C++ 프로그래밍 입문 32,000원",
                "lang": "ko",
                "confidence": 0.93,
            }
        },
    )


class ImageInput(CamelModel):
    s3_key: str
    width: int | None = None
    height: int | None = None


class ImageAnalysisInput(CamelModel):
    description: str = ""
    detected_texts: list[str] = []
    objects: list[str] = []


class AnalyzeInput(CamelModel):
    image: ImageInput | None = None
    ocr: OcrInput | None = None
    image_analysis: ImageAnalysisInput | None = None


class AnalyzeOptions(CamelModel):
    max_tags: int | None = None
    language: str | None = None


Stage = Literal["LLM", "IMAGE_ANALYSIS", "AGENT"]


class AnalyzeRequest(CamelModel):
    job_id: int
    image_id: int
    user_id: int
    stage: Stage
    input: AnalyzeInput = AnalyzeInput()
    options: AnalyzeOptions | None = None
    callback_url: str | None = None


class AnalyzeAccepted(CamelModel):
    job_id: int
    image_id: int
    stage: str
    accepted: bool
    status: str


# ── 10-2 텍스트 임베딩 생성 ───────────────────────────────────────────────
class EmbedRequest(CamelModel):
    texts: list[str]
    purpose: Literal["DOCUMENT", "QUERY"] = "DOCUMENT"


class EmbeddingItem(CamelModel):
    index: int
    vector: list[float]


class EmbedResponse(CamelModel):
    model: str
    model_version: str
    dimension: int
    embeddings: list[EmbeddingItem]


# ── 10-3 자연어 → 구조화 조건 변환 ────────────────────────────────────────
class QueryParseRequest(CamelModel):
    query: str
    now: str | None = None


class ParsedConditions(CamelModel):
    keywords: list[str] = []
    price_min: int | None = None
    price_max: int | None = None
    brand: str | None = None
    category_hints: list[str] = []
    date_from: str | None = None
    date_to: str | None = None
    expires_before: str | None = None


class QueryParseResponse(CamelModel):
    model_version: str
    parsed_conditions: ParsedConditions | None = None
    confidence: float = 0.0


# ── 10-4 분석 결과 콜백 (FastAPI → Spring) ────────────────────────────────
class CallbackError(CamelModel):
    code: str
    message: str
    retryable: bool = False


class CallbackRequest(CamelModel):
    job_id: int
    image_id: int
    stage: str
    status: Literal["COMPLETED", "FAILED", "EMPTY"]
    result: dict[str, Any] | None = None
    error: CallbackError | None = None
    model_version: str
    completed_at: str


# ── 10-5 사용자 지식 후보 (Spring → FastAPI 응답) ─────────────────────────
class TagCandidate(CamelModel):
    tag_id: int
    name: str
    usage_count: int = 0


class CategoryCandidate(CamelModel):
    # 아직 DB 에 없는 기본 카테고리 후보는 categoryId 가 없다(신규 생성 대상).
    category_id: int | None = None
    name: str
    usage_count: int = 0
    # Spring 이 카테고리 대표 벡터를 내려주면 centroid 매칭에 사용한다(선택).
    representative_vector: list[float] | None = None


class KnowledgeCandidates(CamelModel):
    user_id: int
    tags: list[TagCandidate] = []
    categories: list[CategoryCandidate] = []


# ── 문서화 (분석 완료 이미지 → 모바일 HTML 문서) ──────────────────────────
# 10-1 과 같은 방식이다: Spring 이 필요한 데이터를 전부 실어 보내고 AI 는 조회하지
# 않는다. AI 쪽 색인·저장소에 의존하지 않으므로 OpenSearch 가 죽어도 동작하고,
# Spring 은 자기 DB 에서 뽑은 값만 넘기면 된다.
class DocumentImage(CamelModel):
    """문서 재료 1건. Spring 이 분석 완료(COMPLETED) 이미지만 골라 보낸다."""

    image_id: int
    title: str = ""
    summary: str = ""
    tags: list[str] = []              # 이름만. {tagId, name} 객체가 아니다
    category: str | None = None       # 카테고리 이름
    key_information: list[str] = []   # "항목: 값" 문자열 (10-4 keyInformation)
    ocr: OcrInput = OcrInput()        # refinedText 가 있으면 그쪽을 쓴다
    created_at: str | None = None     # ISO-8601. 시간순 서술·출처 표에 쓴다


class DocumentRequest(CamelModel):
    """N장 → 문서 1개. 이미지 한 장당 한 번 부르는 API 가 아니다.

    여러 스크린샷을 종합해 하나의 글로 재구성하는 것이 목적이라, 이미지들이
    한 프롬프트에 함께 들어가야 모델이 비교·병합·시간순 서술을 할 수 있다.
    장당 호출로 쪼개면 각 문서가 서로를 모르므로 같은 결과가 나오지 않는다.
    """

    user_id: int                      # 로그·추적용
    # 문서로 묶을 이미지들. 순서는 참고용이며 구성은 모델이 정한다.
    images: list[DocumentImage]
    title: str | None = None          # 없으면 모델이 제목을 정한다
    instruction: str | None = None    # "여행 일정표로 정리해줘" 같은 추가 지시
    language: str | None = None

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        protected_namespaces=(),
        # Swagger 예시는 반드시 복수로 둔다. 한 장짜리를 보여 주면 장당 1콜로
        # 읽혀서, 이 기능의 목적(여러 장을 한 문서로 종합)이 전달되지 않는다.
        json_schema_extra={
            "example": {
                "userId": 1,
                "instruction": "구매 후보를 가격순으로 비교해 줘",
                "images": [
                    {
                        "imageId": 101,
                        "title": "교보문고 C++ 프로그래밍 입문",
                        "summary": "C++ 입문서 32,000원, 10% 할인 중",
                        "tags": ["도서", "프로그래밍"],
                        "category": "쇼핑",
                        "keyInformation": ["가격: 32,000원", "할인: 10%",
                                           "판매처: 교보문고"],
                        "ocr": {"rawText": "교보문고 C++ 프로그래밍 입문 "
                                           "32,000원 10% 할인"},
                        "createdAt": "2026-07-16T06:00:00.000Z",
                    },
                    {
                        "imageId": 102,
                        "title": "예스24 C++ 프로그래밍 입문",
                        "summary": "같은 책 28,800원, 무료배송",
                        "tags": ["도서", "프로그래밍"],
                        "category": "쇼핑",
                        "keyInformation": ["가격: 28,800원", "배송: 무료",
                                           "판매처: 예스24"],
                        "ocr": {"rawText": "예스24 C++ 프로그래밍 입문 "
                                           "28,800원 무료배송"},
                        "createdAt": "2026-07-16T06:02:00.000Z",
                    },
                    {
                        "imageId": 103,
                        "title": "알라딘 중고 C++ 프로그래밍 입문",
                        "summary": "중고 상급 15,000원",
                        "tags": ["도서", "중고"],
                        "category": "쇼핑",
                        "keyInformation": ["가격: 15,000원", "상태: 상급",
                                           "판매처: 알라딘"],
                        "ocr": {"rawText": "알라딘 중고샵 C++ 프로그래밍 입문 "
                                           "상급 15,000원"},
                        "createdAt": "2026-07-16T06:05:00.000Z",
                    },
                ],
            }
        },
    )


class DocumentSection(CamelModel):
    heading: str
    body: str = ""
    bullets: list[str] = []
    # 이 섹션의 근거가 된 이미지. 실제 소스에 있는 번호만 남는다.
    image_ids: list[int] = []


class SkippedImage(CamelModel):
    image_id: int
    # NO_CONTENT: 제목·요약·주요정보·OCR 이 전부 비어 문서에 넣을 내용이 없다.
    reason: str


class DocumentResponse(CamelModel):
    title: str
    summary: str = ""
    sections: list[DocumentSection] = []
    # 위 구조를 렌더링한 모바일용 HTML 문서(self-contained). WebView 에 그대로 load
    # 하면 된다. 레이아웃·서식은 내용과 무관하게 항상 같다. 보통 이 필드만 쓰면 된다.
    html: str
    source_image_ids: list[int] = []
    skipped: list[SkippedImage] = []
    model_version: str
    generated_at: str


# ── 검색 (OpenSearch 키워드/BM25) ─────────────────────────────────────────
class SearchRequest(CamelModel):
    user_id: int
    query: str
    category: str | None = None      # 카테고리명으로 필터 (선택)
    size: int = 10


class SearchHit(CamelModel):
    image_id: int
    title: str = ""
    summary: str = ""
    tags: list[str] = []
    category: str | None = None
    score: float = 0.0


class SearchResponse(CamelModel):
    total: int
    hits: list[SearchHit] = []


# ── 앱 API (Spring 우회 구간) ─────────────────────────────────────────────
# 팀 API 명세의 외부 API 형식을 따른다. Spring 이 복귀했을 때 앱이 응답 모델을
# 그대로 재사용할 수 있도록, 이 서비스가 채우지 못하는 값도 필드는 유지하고
# null 로 내려보낸다(예: favorite, fileName, structuredData).


class TagRef(CamelModel):
    tag_id: int
    name: str
    # 명세 6-2 는 tags[].source 를 요구한다(AGENT/USER). 6-3 수정 API 가 범위 밖이라
    # 현재 태그는 전부 AGENT 생성이다. 목록(6-1)에는 없는 필드라 기본 None 으로 두고
    # 상세에서만 채운다.
    source: str | None = None


class TagCount(CamelModel):
    tag_id: int
    name: str
    image_count: int


class CategoryRef(CamelModel):
    category_id: int
    name: str
    # 명세 6-2 categories[].confidence. 카테고리 판정 시의 코사인 유사도를 쓴다.
    # 목록(6-1)에는 없는 필드라 기본 None.
    confidence: float | None = None


# ── 4-1 이미지 등록 및 업로드 URL 발급 ───────────────────────────────────
class UploadItem(CamelModel):
    client_request_id: str
    file_name: str
    content_hash: str
    file_size: int
    # 명세 개정으로 4-3(OCR 제출)이 4-1 요청에 합쳐졌다. 온디바이스 OCR 결과를
    # 등록 시점에 함께 받아 두면 업로드 완료 즉시 분석을 시작할 수 있다.
    ocr: OcrInput = OcrInput()


class UploadRequest(CamelModel):
    images: list[UploadItem]

    # Swagger 의 Example Value 를 그대로 복사해 실행할 수 있게 실제 값을 넣는다.
    # 기본 생성 예시("string"·0)는 그대로 보내면 UNSUPPORTED_FORMAT 으로 떨어져
    # 프론트가 무엇을 보내야 하는지 알 수 없다.
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        protected_namespaces=(),
        json_schema_extra={
            "example": {
                "images": [
                    {
                        "clientRequestId": "local-001",
                        "fileName": "Screenshot_20260716_101010.png",
                        "contentHash": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
                                       "e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
                        "fileSize": 384211,
                        "ocr": {
                            "rawText": "교보문고 C++ 프로그래밍 입문 32,000원",
                            "lang": "ko",
                            "confidence": 0.93,
                        },
                    }
                ]
            }
        },
    )


class RegisteredUpload(CamelModel):
    client_request_id: str
    image_id: int
    file_name: str
    upload_url: str
    upload_expires_in: int


class DuplicatedUpload(CamelModel):
    client_request_id: str
    file_name: str
    existing_image_id: int


class FailedUpload(CamelModel):
    client_request_id: str
    file_name: str
    reason: str


class UploadResponse(CamelModel):
    registered: list[RegisteredUpload] = []
    duplicated: list[DuplicatedUpload] = []
    failed: list[FailedUpload] = []


class UploadCompleteResponse(CamelModel):
    image_id: int
    upload_completed: bool
    uploaded_at: str


class UploadUrlResponse(CamelModel):
    image_id: int
    upload_url: str
    upload_expires_in: int


class OcrStage(CamelModel):
    stage: str = "OCR"
    status: str = "COMPLETED"


class OcrSubmitResponse(CamelModel):
    image_id: int
    ocr: OcrStage = OcrStage()


class ThumbnailResponse(CamelModel):
    """명세 6-6 Response data. 실제 이미지는 thumbnailUrl 이 가리키는 경로가 준다."""

    thumbnail_url: str
    title: str = ""
    # 명세 6-6 의 tags 는 이름 배열이다(6-1·6-2 의 객체 배열과 다르다).
    tags: list[str] = []


class AnalysisJob(CamelModel):
    job_id: int
    image_id: int
    stage: str
    status: str
    file_name: str | None = None      # 미제공(Spring 영역)
    attempt: int = 1
    progress: int | None = None       # 미제공
    retryable: bool = False
    error_code: str | None = None
    updated_at: str | None = None


class AnalysisSummary(CamelModel):
    total: int
    stage_counts: dict[str, dict[str, int]] = {}
    overall_counts: dict[str, int] = {}


class ImageListItem(CamelModel):
    image_id: int
    file_name: str | None = None
    title: str = ""
    summary: str = ""
    status: str = "COMPLETED"
    favorite: bool | None = None
    # 목록은 썸네일만 준다(정사각 crop, 가볍다). 원본이 필요하면 상세(6-2)를 부른다.
    thumbnail_url: str | None = None
    tags: list[TagRef] = []
    categories: list[CategoryRef] = []
    created_at: str | None = None


class ImageDetail(CamelModel):
    image_id: int
    file_name: str | None = None      # 미제공
    content_hash: str | None = None   # 미제공
    status: str = "COMPLETED"
    favorite: bool | None = None      # 미제공
    title: str = ""
    summary: str = ""
    ocr: OcrInput | None = None
    tags: list[TagRef] = []
    categories: list[CategoryRef] = []
    structured_data: dict[str, Any] | None = None   # MVP 제외
    # 명세 6-2 fieldSources: 각 필드를 누가 채웠는지(AGENT/USER).
    # 6-3 수정 API 가 범위 밖이라 현재는 전부 AGENT 다.
    field_sources: dict[str, str] = {}
    analysis_confidence: float | None = None
    key_information: list[str] = []
    # 상세는 원본만 준다. 썸네일은 정사각으로 잘려 있어 스크린샷 내용을 다 읽을 수
    # 없으므로 상세 화면에는 부적합하다(명세 6-2 에도 thumbnailUrl 은 없다).
    image_url: str | None = None
    created_at: str | None = None
    uploaded_at: str | None = None
    updated_at: str | None = None
    last_viewed_at: str | None = None


class CategoryCard(CamelModel):
    category_id: int
    name: str
    thumbnail_url: str | None = None
    image_count: int
    tags: list[TagCount] = []
    updated_at: str | None = None


class CategoryListData(CamelModel):
    """7-2 카테고리 목록의 data.

    명세는 페이지 형식이지만 **프론트 요청으로 페이지네이션을 뺐다**(팀 합의).
    카테고리는 기본 17종에 AGENT 가 새로 만든 것이 더해지는 정도라 수가 적다
    (사람이 직접 만드는 경로는 없다). 나눠 줄 이유가 없어 전체를 한 번에 준다.
    `list` 위치는 다른 목록 API 와 같게 두어 응답 접근 경로를 통일한다.
    """

    list: List[CategoryCard] = []


# ── 7-3 홈 대시보드 요약 ─────────────────────────────────────────────────
class HomeUser(CamelModel):
    # 로그인 미구현 구간이라 채울 수 없다. 필드는 유지하고 null 로 내려보낸다.
    nickname: str | None = None


class ActiveAnalysis(CamelModel):
    job_id: int
    image_id: int
    file_name: str | None = None
    title: str = ""
    thumbnail_url: str | None = None
    stage: str
    status: str
    progress: int


class HomeAnalysisStatus(CamelModel):
    has_active_jobs: bool
    queued_count: int
    processing_count: int
    failed_count: int
    active_analysis: ActiveAnalysis | None = None


class HomeCategory(CamelModel):
    category_id: int
    name: str
    image_count: int
    tags: list[TagRef] = []
    updated_at: str | None = None


class HomeRecentImage(CamelModel):
    image_id: int
    title: str = ""
    thumbnail_url: str | None = None
    tags: list[TagRef] = []
    favorite: bool | None = None
    analyzed_at: str | None = None


class HomeResponse(CamelModel):
    home_date: str
    user: HomeUser = HomeUser()
    analysis_status: HomeAnalysisStatus
    categories: list[HomeCategory] = []
    recent_images: list[HomeRecentImage] = []


class TagItem(CamelModel):
    tag_id: int
    name: str
    usage_count: int
    created_by: str = "AGENT"


class SearchResultItem(CamelModel):
    image_id: int
    title: str = ""
    summary: str = ""
    thumbnail_url: str | None = None
    matched_in: list[str] | None = None   # 미제공
    highlight: str | None = None          # 미제공
    score: float = 0.0
    tags: list[TagRef] = []
    last_viewed_at: str | None = None     # 미제공
    uploaded_at: str | None = None        # 미제공
    created_at: str | None = None


