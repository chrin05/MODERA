"""FastAPI 내부 API 서버.

공통 응답 envelope 를 쓰지 않는 서비스 간 통신 전용이며,
에러는 {"error": CODE, "message": ..., "detail": {}} 형식으로 반환한다.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import BackgroundTasks, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyHeader

from . import document, gemini_client, responses, search, storage
from .config import get_settings
from .jobs import job_registry, job_store
from .schemas import (
    AnalysisJob,
    AnalysisSummary,
    AnalyzeAccepted,
    AnalyzeRequest,
    CategoryCard,
    CategoryListData,
    CategoryRef,
    ActiveAnalysis,
    ApiResponse,
    DocumentRequest,
    DocumentResponse,
    EmbedRequest,
    EmbedResponse,
    EmbeddingItem,
    HomeAnalysisStatus,
    HomeCategory,
    HomeRecentImage,
    HomeResponse,
    ImageDetail,
    ImageListItem,
    OcrInput,
    OcrSubmitResponse,
    PageData,
    ParsedConditions,
    QueryParseRequest,
    QueryParseResponse,
    SearchHit,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
    TagCount,
    TagItem,
    TagRef,
    ThumbnailResponse,
    UploadCompleteResponse,
    UploadRequest,
    UploadResponse,
    UploadUrlResponse,
    RegisteredUpload,
    DuplicatedUpload,
    FailedUpload,
)
from .stages import execute_stage, run_app_analysis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

# 문서 UI 는 개발·팀 공유 편의를 위해 켜둔다. 외부에 노출되는 서버라면
# ENABLE_DOCS=false 로 꺼야 한다(엔드포인트 구조가 그대로 드러난다).
# get_settings() 는 자격증명이 없으면 예외를 던지므로 여기서는 환경변수를 직접 읽는다.
_docs_enabled = os.environ.get("ENABLE_DOCS", "true").lower() == "true"

app = FastAPI(
    title="Screenshot Knowledge AI",
    description=(
        "스크린샷 지식 DB의 AI 서비스.\n\n"
        "- `/api/v1/*` : 앱 직결 API (Spring 우회 구간)\n"
        "- `/internal/v1/*` : Spring 연동용 내부 API\n\n"
        "모든 요청에 `X-Internal-Token` 헤더가 필요하다. "
        "우측 상단 **Authorize** 에서 토큰을 넣으면 아래 예제들을 그대로 실행할 수 있다."
    ),
    version="1.0.0",
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)


def _now_iso() -> str:
    """명세 1.2 시간 형식: ISO-8601 UTC, 밀리초 3자리."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ── OpenAPI 후처리 ────────────────────────────────────────────────────────
# FastAPI 는 검증 가능한 파라미터가 있는 엔드포인트에 422 를 자동으로 문서화한다.
# 그런데 우리는 RequestValidationError 를 400 INVALID_PARAMETER 로 바꿔 내보내므로
# 422 는 실제로 나가지 않는다. 문서에 남아 있으면 프론트가 없는 응답을 처리하게 된다.
# 대신 명세 1.3 의 실제 에러 응답을 채워 넣는다.
_APP_ERROR_EXAMPLE = {
    "code": "IMAGE_NOT_FOUND",
    "message": "이미지를 찾을 수 없습니다.",
    "data": "imageId: 1024",
    "timestamp": "2026-07-16T06:00:00.000Z",
}
_APP_ERRORS = {
    "400": ("INVALID_PARAMETER / INVALID_SORT", {
        "code": "INVALID_PARAMETER",
        "message": "요청 값이 올바르지 않습니다.",
        "data": [{"field": "sort", "message": "지원하지 않는 정렬입니다."}],
        "timestamp": "2026-07-16T06:00:00.000Z",
    }),
    "401": ("UNAUTHORIZED", {
        "code": "UNAUTHORIZED", "message": "인증이 필요합니다.",
        "data": None, "timestamp": "2026-07-16T06:00:00.000Z",
    }),
    "404": ("IMAGE_NOT_FOUND / THUMBNAIL_NOT_FOUND", _APP_ERROR_EXAMPLE),
}


def _custom_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(
        title=app.title, version=app.version,
        description=app.description, routes=app.routes,
    )
    for path, ops in schema.get("paths", {}).items():
        if not path.startswith("/api/v1"):
            continue
        for op in ops.values():
            resp = op.get("responses", {})
            resp.pop("422", None)
            for code, (desc, example) in _APP_ERRORS.items():
                resp.setdefault(code, {
                    "description": desc,
                    "content": {"application/json": {"example": example}},
                })
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi  # type: ignore[method-assign]


def _error(code: str, message: str, detail: Any = None, http_status: int = 400) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content={"error": code, "message": message, "detail": detail or {}},
    )


# APIKeyHeader 로 선언하면 Swagger 에 Authorize 버튼이 생겨 토큰을 넣고
# 문서에서 바로 호출해 볼 수 있다. 동작은 헤더 검사로 동일하다.
_internal_token_header = APIKeyHeader(
    name="X-Internal-Token",
    auto_error=False,
    description="서비스 간 공유 토큰. 팀에서 전달받은 값을 넣는다.",
)


async def require_internal_token(
    request: Request,
    token: str | None = Depends(_internal_token_header),
) -> None:
    # 임시: APP_API_AUTH=false 인 동안 /api/v1/* 은 토큰 없이 통과시킨다.
    # Android 가 Spring 을 거치지 않고 직접 부르는 기간용이며, 설정 설명은 config.py 참고.
    if _is_app_api(request) and not get_settings().app_api_auth:
        return
    if token != get_settings().internal_token:
        raise PermissionError("내부 토큰 불일치")


@app.exception_handler(search.InvalidSortError)
async def _invalid_sort_handler(request: Request, exc: Exception) -> JSONResponse:
    """명세 11: INVALID_SORT(400) — 지원하지 않는 정렬 필드 또는 방향."""
    return responses.failure("INVALID_SORT", str(exc),
                             [{"field": "sort", "message": str(exc)}], http_status=400)


def _is_app_api(request: Request) -> bool:
    """앱 직결 API 인지. 에러 형식이 내부 API 와 다르다(명세 1.1 vs 10 장)."""
    return request.url.path.startswith("/api/v1")


@app.exception_handler(PermissionError)
async def _permission_handler(request: Request, exc: PermissionError) -> JSONResponse:
    if _is_app_api(request):
        # 명세 1.1 공통 envelope + 1.3 UNAUTHORIZED(401).
        # 인증 실패 사유는 밖으로 내보내지 않는다.
        return responses.failure("UNAUTHORIZED", "인증이 필요합니다.", http_status=401)
    return _error("UNAUTHORIZED", str(exc), http_status=401)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    if _is_app_api(request):
        # 명세 1.1: 검증 오류는 data 에 {field, message} 배열을 담는다.
        fields = [
            {
                # loc 은 ("query", "page") 처럼 위치까지 담고 있어 마지막 요소만 쓴다.
                "field": str(e["loc"][-1]) if e.get("loc") else "",
                "message": e.get("msg", ""),
            }
            for e in exc.errors()[:5]
        ]
        return responses.failure("INVALID_PARAMETER", "요청 값이 올바르지 않습니다.",
                                 fields, http_status=400)
    return _error("INVALID_REQUEST", "요청 값이 올바르지 않습니다.",
                  detail={"errors": exc.errors()[:5]}, http_status=400)


# ── 사용자 식별 ───────────────────────────────────────────────────────────
# MVP 는 회원가입·로그인이 없다. 요청이 보내는 userId 를 검증할 방법이 없으므로
# FIXED_USER_ID 가 설정돼 있으면 요청 값을 무시하고 그 값으로 고정한다.
# 파라미터 자체는 그대로 받아 두기 때문에 로그인이 붙으면 설정만 0 으로 바꾸면 된다.
class _MissingUserId(Exception):
    pass


@app.exception_handler(_MissingUserId)
async def _missing_user_id_handler(request: Request, exc: _MissingUserId) -> JSONResponse:
    return responses.failure("INVALID_PARAMETER", "요청 값이 올바르지 않습니다.",
                             [{"field": "userId", "message": "필수 값입니다."}])


def resolve_user_id(requested: int | None) -> int:
    fixed = get_settings().fixed_user_id
    if fixed:
        return fixed
    if requested is None:
        raise _MissingUserId()
    return requested


async def _current_user_id(
    user_id: int | None = Query(
        None,
        alias="userId",
        # 명세 어디에도 userId 쿼리 파라미터는 없다(인증 토큰에서 사용자를 얻는 설계).
        # 값이 와도 FIXED_USER_ID 로 덮어쓰므로 문서에 노출하지 않는다.
        # 노출하면 프론트가 "무엇을 보내야 하나" 를 고민하게 되고, 보낸 값이
        # 무시되는 것도 혼란스럽다. 받기는 계속 받으므로 기존 호출은 깨지지 않는다.
        include_in_schema=False,
    ),
) -> int:
    return resolve_user_id(user_id)


CurrentUserId = Annotated[int, Depends(_current_user_id)]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ── 10-1 단계별 분석 실행 요청 ────────────────────────────────────────────
_REQUIRED_INPUT = {
    "LLM": ("ocr",),
    "IMAGE_ANALYSIS": ("image",),
    "AGENT": ("ocr", "image_analysis"),
}


@app.post("/internal/v1/analyze", dependencies=[Depends(require_internal_token)])
async def analyze(request: AnalyzeRequest, background_tasks: BackgroundTasks):
    missing = [
        field for field in _REQUIRED_INPUT[request.stage]
        if getattr(request.input, field, None) is None
    ]
    if missing:
        return _error("INVALID_REQUEST",
                      f"{request.stage} 단계에 필요한 input 이 없습니다.",
                      detail={"missing": missing}, http_status=400)

    if not job_registry.try_claim(request.job_id, request.stage):
        # 멱등 처리: 이미 처리 중이거나 완료된 작업 (명세상 HTTP 200)
        return JSONResponse(
            status_code=200,
            content={"error": "DUPLICATE_JOB",
                     "message": "동일 작업을 이미 처리 중이거나 완료했습니다.",
                     "detail": {"jobId": request.job_id, "stage": request.stage}},
        )

    background_tasks.add_task(execute_stage, request)
    accepted = AnalyzeAccepted(
        job_id=request.job_id, image_id=request.image_id,
        stage=request.stage, accepted=True, status="QUEUED",
    )
    return JSONResponse(status_code=202,
                        content=accepted.model_dump(by_alias=True))


# ── 10-2 텍스트 임베딩 생성 ───────────────────────────────────────────────
@app.post("/internal/v1/embed", dependencies=[Depends(require_internal_token)])
async def embed(request: EmbedRequest):
    if not request.texts:
        return _error("INVALID_REQUEST", "texts 가 비어 있습니다.", http_status=400)
    try:
        import asyncio
        model_name, vectors = await asyncio.to_thread(
            gemini_client.embed, request.texts, request.purpose
        )
    except Exception as e:
        logger.exception("임베딩 실패")
        return _error("EMBEDDING_FAILED", str(e)[:500], http_status=502)

    response = EmbedResponse(
        model=model_name,
        model_version=model_name,
        dimension=len(vectors[0]) if vectors else 0,
        embeddings=[EmbeddingItem(index=i, vector=v) for i, v in enumerate(vectors)],
    )
    return JSONResponse(status_code=200, content=response.model_dump(by_alias=True))


# ── 10-3 자연어 → 구조화 조건 변환 ────────────────────────────────────────
@app.post("/internal/v1/query/parse", dependencies=[Depends(require_internal_token)])
async def query_parse(request: QueryParseRequest):
    settings = get_settings()
    prompt = (
        "사용자의 자연어 검색어를 구조화된 검색 조건으로 변환하라. "
        "확인되지 않는 값은 null 로 두고 추측하지 마라.\n"
        f"상대 날짜 해석 기준 시각: {request.now or '서버 현재 시각'}\n"
        "반드시 아래 JSON만 출력. 마크다운·설명 금지.\n"
        '{"keywords":["..."],"price_min":null,"price_max":null,"brand":null,'
        '"category_hints":["..."],"date_from":null,"date_to":null,'
        '"expires_before":null,"confidence":0.0}\n\n'
        f"검색어: {request.query}"
    )
    try:
        import asyncio
        parsed = await asyncio.to_thread(
            gemini_client.generate_json, settings.llm_model_name, [prompt]
        )
        conditions = ParsedConditions.model_validate(
            {k: v for k, v in parsed.items() if k != "confidence"}
        )
        response = QueryParseResponse(
            model_version=settings.llm_model_name,
            parsed_conditions=conditions,
            confidence=float(parsed.get("confidence", 0.0)),
        )
    except Exception as e:
        # 완전 실패도 에러 코드로 올리지 않고 parsedConditions=null 로 degrade
        logger.warning("조건 변환 실패: %s", e)
        response = QueryParseResponse(
            model_version=settings.llm_model_name,
            parsed_conditions=None,
            confidence=0.0,
        )
    return JSONResponse(status_code=200, content=response.model_dump(by_alias=True))


# ── 키워드 검색 (OpenSearch BM25) ─────────────────────────────────────────
@app.post("/internal/v1/search", dependencies=[Depends(require_internal_token)])
async def keyword_search(request: SearchRequest):
    if not request.query or not request.query.strip():
        return _error("INVALID_REQUEST", "query 가 비어 있습니다.", http_status=400)
    try:
        import asyncio
        hits, total = await asyncio.to_thread(
            search.keyword_search,
            request.user_id, request.query, request.category, request.size,
        )
    except Exception as e:
        logger.exception("검색 실패")
        return _error("SEARCH_FAILED", str(e)[:500], http_status=502)

    response = SearchResponse(total=total, hits=[SearchHit(**h) for h in hits])
    return JSONResponse(status_code=200, content=response.model_dump(by_alias=True))


# ── 문서화 (분석 완료 이미지 → 모바일 HTML) ──────────────────────────────
@app.post("/internal/v1/documents", dependencies=[Depends(require_internal_token)],
          response_model=DocumentResponse,
          responses={400: {"description": "INVALID_REQUEST / NO_DOCUMENT_SOURCE"},
                     502: {"description": "DOCUMENT_GENERATION_FAILED"}})
async def create_document(request: DocumentRequest):
    """분석이 끝난 이미지들을 묶어 하나의 모바일용 HTML 문서로 만든다.

    분석 파이프라인과 무관한 별개 기능이다. 새로 분석하지 않는다.

    재료(`images`)는 **Spring 이 요청 본문에 실어 보낸다.** 10-1 과 같은 방식이라
    AI 는 아무것도 조회하지 않는다. 어떤 이미지가 이 사용자 것이고 분석이
    끝났는지는 Spring 이 질의 단계에서 이미 거른 상태여야 한다.

    응답의 `html` 이 최종 산출물이다. 외부 리소스를 참조하지 않는 self-contained
    문서라 Spring 은 그대로 저장하거나 앱에 내려보내 WebView 에 load 하면 된다.
    레이아웃·서식은 이미지 종류와 무관하게 항상 같다.
    """
    import asyncio

    if not request.images:
        return _error("INVALID_REQUEST", "images 가 비어 있습니다.", http_status=400)
    if len(request.images) > document.MAX_IMAGES:
        return _error(
            "INVALID_REQUEST",
            f"한 번에 최대 {document.MAX_IMAGES}장까지 묶을 수 있습니다.",
            detail={"requested": len(request.images), "limit": document.MAX_IMAGES},
            http_status=400,
        )

    try:
        result = await asyncio.to_thread(
            document.generate,
            request.user_id, request.images,
            request.title, request.instruction, request.language,
        )
    except document.NoSourceError as e:
        # 요청은 정상인데 재료가 전부 비어 있는 경우.
        return _error("NO_DOCUMENT_SOURCE", str(e), http_status=400)
    except Exception as e:
        logger.exception("문서 생성 실패 userId=%s", request.user_id)
        return _error("DOCUMENT_GENERATION_FAILED", str(e)[:500], http_status=502)

    response = DocumentResponse.model_validate(result)
    return JSONResponse(status_code=200, content=response.model_dump(by_alias=True))


# ─────────────────────────────────────────────────────────────────────────
# 앱 API (Spring 우회 구간)
#
# 팀 API 명세의 외부 API 규약을 따른다: 공통 envelope, 페이지 형식, 에러 코드.
# Spring 이 복귀하면 앱은 호출 대상만 Spring 으로 바꾸면 되도록 응답 구조를 맞춰 둔다.
# 이 서비스가 채울 수 없는 값(favorite, fileName, structuredData 등)은
# 필드를 유지한 채 null 로 내려보낸다.
# ─────────────────────────────────────────────────────────────────────────


def _tag_refs(names: list[str], source: str | None = None) -> list[TagRef]:
    """source 는 상세(6-2)에서만 채운다. 목록(6-1)에는 명세상 없는 필드다."""
    return [TagRef(tag_id=search.stable_id(n), name=n, source=source)
            for n in names or []]


def _category_refs(
    name: str | None, confidence: float | None = None
) -> list[CategoryRef]:
    if not name:
        return []
    return [CategoryRef(category_id=search.stable_id(name), name=name,
                        confidence=confidence)]


def _thumbnail_url(image_id: int, s3_key: str | None = None) -> str | None:
    """목록 격자용 썸네일 주소(정사각 crop).

    기본은 만료 없는 서버 경유 경로다. PRESIGNED_READ_URLS=true 로 켜면 스토리지
    presigned URL 을 준다(트래픽은 줄지만 만료가 생겨 앱 캐시가 매번 깨진다).
    """
    settings = get_settings()
    if s3_key and settings.presigned_read_urls and settings.s3_public_endpoint:
        url = storage.presigned_get_url(s3_key, settings.s3_thumbnail_bucket)
        if url:
            return url
    return f"/api/v1/images/{image_id}/thumbnail/raw"


def _image_url(image_id: int, s3_key: str | None = None) -> str | None:
    """원본 이미지 주소.

    썸네일은 정사각으로 잘려 있어 스크린샷 내용을 다 볼 수 없다. 상세 화면처럼
    전체를 봐야 하는 곳은 이 주소를 쓴다.

    조회 URL 방식은 _thumbnail_url 과 동일하게 PRESIGNED_READ_URLS 로 정한다.
    """
    if not s3_key:
        return None
    settings = get_settings()
    if settings.presigned_read_urls and settings.s3_public_endpoint:
        url = storage.presigned_get_url(s3_key, settings.s3_bucket)
        if url:
            return url
    return f"/api/v1/images/{image_id}/source"


def _to_list_item(img: dict[str, Any]) -> ImageListItem:
    return ImageListItem(
        image_id=img["image_id"],
        file_name=img.get("file_name"),
        title=img.get("title", ""),
        summary=img.get("summary", ""),
        status=img.get("status") or "COMPLETED",
        favorite=img.get("favorite", False),
        thumbnail_url=_thumbnail_url(img["image_id"], img.get("s3_key")),
        tags=_tag_refs(img.get("tags") or []),
        categories=_category_refs(img.get("category")),
        created_at=img.get("created_at"),
    )


async def _resolve_filters(
    user_id: int, category_id: int | None, tag_id: int | None
) -> tuple[str | None, str | None]:
    """categoryId·tagId 를 실제 이름으로 되돌린다. 없는 ID 면 None 이 된다."""
    import asyncio

    category = tag = None
    if category_id is not None:
        category = await asyncio.to_thread(
            search.resolve_name_by_id, user_id, "category_name", category_id
        )
    if tag_id is not None:
        tag = await asyncio.to_thread(
            search.resolve_name_by_id, user_id, "tags.keyword", tag_id
        )
    return category, tag


# ── 4-1 이미지 등록 및 업로드 URL 발급 ───────────────────────────────────
# 명세대로 **바이너리를 받지 않는다.** 메타데이터를 등록하고 콘텐츠 해시로 중복을
# 판정한 뒤 presigned PUT URL 을 발급한다. 앱이 그 URL 로 스토리지에 직접 올리고
# 4-2 로 완료를 통지하면 분석이 시작된다.
# 이미지 바이트가 AI 서버를 통과하지 않아 대량 업로드에도 서버가 흔들리지 않는다.
_ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "heic", "heif"}


def _extension(file_name: str) -> str | None:
    _, _, ext = (file_name or "").rpartition(".")
    ext = ext.lower()
    return ext if ext in _ALLOWED_EXTENSIONS else None


@app.post("/api/v1/images/upload", dependencies=[Depends(require_internal_token)],
          response_model=ApiResponse[UploadResponse])
async def app_image_upload(user_id: CurrentUserId, request: UploadRequest):
    """이미지를 등록하고 업로드 URL 을 발급한다(명세 4-1).

    항목별로 성공·중복·실패를 나눠 돌려주므로, 일부가 실패해도 나머지는 계속 진행된다.
    `clientRequestId` 를 그대로 되돌려 주어 앱이 원래 사진과 매칭할 수 있다.
    """
    import asyncio

    settings = get_settings()
    if not request.images:
        return responses.failure("INVALID_PARAMETER", "요청 값이 올바르지 않습니다.",
                                 [{"field": "images", "message": "필수 값입니다."}])

    limit = settings.max_upload_mb * 1024 * 1024
    registered: list[RegisteredUpload] = []
    duplicated: list[DuplicatedUpload] = []
    failed: list[FailedUpload] = []
    # 같은 배치 안의 중복도 잡아야 한다. 방금 등록한 문서는 색인 refresh 전이라
    # find_by_content_hash 로는 아직 안 보이기 때문이다.
    batch_hashes: dict[str, int] = {}

    for item in request.images:
        ext = _extension(item.file_name)
        if ext is None:
            failed.append(FailedUpload(client_request_id=item.client_request_id,
                                       file_name=item.file_name,
                                       reason="UNSUPPORTED_FORMAT"))
            continue
        if item.file_size <= 0 or item.file_size > limit:
            failed.append(FailedUpload(client_request_id=item.client_request_id,
                                       file_name=item.file_name,
                                       reason="FILE_SIZE_EXCEEDED"))
            continue
        if not item.content_hash.strip():
            failed.append(FailedUpload(client_request_id=item.client_request_id,
                                       file_name=item.file_name,
                                       reason="INVALID_CONTENT_HASH"))
            continue

        # 중복 판정: 같은 사용자가 이미 올린 같은 내용이면 재분석하지 않는다.
        existing = batch_hashes.get(item.content_hash)
        if existing is None:
            existing = await asyncio.to_thread(
                search.find_by_content_hash, user_id, item.content_hash
            )
        if existing is not None:
            duplicated.append(DuplicatedUpload(client_request_id=item.client_request_id,
                                               file_name=item.file_name,
                                               existing_image_id=existing))
            continue

        image_id = job_store.next_image_id()
        s3_key = f"u/{user_id}/{image_id}.{ext}"
        try:
            url, expires = await asyncio.to_thread(storage.presigned_put_url, s3_key)
        except Exception as e:
            logger.warning("업로드 URL 발급 실패 imageId=%s: %s", image_id, e)
            failed.append(FailedUpload(client_request_id=item.client_request_id,
                                       file_name=item.file_name,
                                       reason="UPLOAD_URL_ISSUE_FAILED"))
            continue

        ocr_text = (item.ocr.refined_text or item.ocr.raw_text or "").strip()
        await asyncio.to_thread(
            search.create_pending_document,
            image_id=image_id, user_id=user_id, s3_key=s3_key,
            file_name=item.file_name, content_hash=item.content_hash,
            file_size=item.file_size, created_at=_now_iso(),
            raw_text=ocr_text, ocr_lang=item.ocr.lang,
            ocr_confidence=item.ocr.confidence,
        )
        batch_hashes[item.content_hash] = image_id
        registered.append(RegisteredUpload(
            client_request_id=item.client_request_id, image_id=image_id,
            file_name=item.file_name, upload_url=url, upload_expires_in=expires,
        ))

    if registered:
        # 배치당 1회. 이걸 안 하면 방금 등록한 이미지가 6-1 목록에 안 나오고
        # 다음 요청의 중복 판정에서도 놓친다.
        await asyncio.to_thread(search.refresh_index)

    logger.info("이미지 등록 userId=%s 성공=%s 중복=%s 실패=%s",
                user_id, len(registered), len(duplicated), len(failed))

    body = UploadResponse(registered=registered, duplicated=duplicated,
                          failed=failed).model_dump(by_alias=True)

    # 한 건도 등록되지 않았는데 실패만 남았으면 성공이 아니다. code 까지 SUCCESS 로
    # 주면 프론트가 code 만 보고 성공 처리해 사진이 조용히 유실된다.
    # data 형태(registered/duplicated/failed)는 그대로 두므로 파싱은 깨지지 않는다.
    if failed and not registered:
        reasons = [f.reason for f in failed]
        logger.warning("이미지 등록 전건 실패 userId=%s 사유=%s", user_id, reasons)
        # URL 발급 실패는 스토리지 장애(서버 탓), 나머지는 요청 값 문제(클라이언트 탓).
        server_fault = "UPLOAD_URL_ISSUE_FAILED" in reasons
        return responses.failure(
            "UPLOAD_REGISTRATION_FAILED", "이미지를 등록하지 못했습니다.",
            body, http_status=502 if server_fault else 400,
        )

    return responses.success(body, "이미지가 등록되었습니다.")


# ── 4-2 업로드 완료 통지 ─────────────────────────────────────────────────
@app.post("/api/v1/images/{image_id}/upload-complete",
          dependencies=[Depends(require_internal_token)],
          response_model=ApiResponse[UploadCompleteResponse])
async def app_upload_complete(image_id: int, background_tasks: BackgroundTasks):
    """스토리지 업로드 완료를 통지받고 분석 파이프라인을 시작한다(명세 4-2).

    명세는 이 API 가 분석 작업을 만들지 않고 OCR 제출 후 LLM 부터 시작한다고 하지만,
    개정으로 OCR 이 4-1 요청에 합쳐졌다. 그래서 원본과 OCR 이 모두 갖춰지는 시점이
    바로 여기이고, 여기서 파이프라인을 시작한다.
    """
    import asyncio

    found = await asyncio.to_thread(search.get_image, image_id)
    if found is None:
        return responses.failure("IMAGE_NOT_FOUND", "이미지를 찾을 수 없습니다.",
                                 f"imageId: {image_id}", http_status=404)

    s3_key = found.get("s3_key")
    if not s3_key or not await asyncio.to_thread(storage.object_exists, s3_key):
        return responses.failure("UPLOAD_NOT_FOUND", "업로드된 파일을 찾을 수 없습니다.",
                                 f"imageId: {image_id}", http_status=409)

    uploaded_at = _now_iso()
    await asyncio.to_thread(search.mark_uploaded, image_id, uploaded_at)

    job = job_store.create(found.get("user_id") or 0, s3_key, image_id)
    background_tasks.add_task(
        run_app_analysis,
        job["job_id"], image_id, found.get("user_id") or 0, s3_key,
        found.get("raw_text", ""),
    )
    logger.info("업로드 완료 통지 imageId=%s jobId=%s", image_id, job["job_id"])

    return responses.success(
        UploadCompleteResponse(image_id=image_id, upload_completed=True,
                               uploaded_at=uploaded_at).model_dump(by_alias=True),
        "업로드가 완료되었습니다.",
    )


# ── 4-5 업로드 URL 재발급 ────────────────────────────────────────────────
@app.post("/api/v1/images/{image_id}/upload-url",
          dependencies=[Depends(require_internal_token)],
          response_model=ApiResponse[UploadUrlResponse])
async def app_reissue_upload_url(image_id: int):
    """presigned URL 이 만료됐는데 아직 업로드가 안 끝난 이미지에 새 URL 을 준다(명세 4-5)."""
    import asyncio

    found = await asyncio.to_thread(search.get_image, image_id)
    if found is None:
        return responses.failure("IMAGE_NOT_FOUND", "이미지를 찾을 수 없습니다.",
                                 f"imageId: {image_id}", http_status=404)
    if found.get("uploaded_at"):
        return responses.failure("UPLOAD_ALREADY_COMPLETED", "업로드가 이미 완료되었습니다.",
                                 f"imageId: {image_id}", http_status=409)
    if (found.get("status") or "QUEUED") not in ("QUEUED",):
        return responses.failure("ANALYSIS_IN_PROGRESS", "분석이 이미 시작되었습니다.",
                                 f"imageId: {image_id}", http_status=409)

    try:
        url, expires = await asyncio.to_thread(storage.presigned_put_url,
                                               found["s3_key"])
    except Exception as e:
        logger.exception("업로드 URL 재발급 실패 imageId=%s", image_id)
        return responses.failure("INTERNAL_ERROR", "업로드 URL 을 발급하지 못했습니다.",
                                 str(e)[:300], http_status=500)

    return responses.success(
        UploadUrlResponse(image_id=image_id, upload_url=url,
                          upload_expires_in=expires).model_dump(by_alias=True))


# ── 4-3 온디바이스 OCR 결과 제출 ─────────────────────────────────────────
@app.post("/api/v1/images/{image_id}/ocr",
          dependencies=[Depends(require_internal_token)],
          response_model=ApiResponse[OcrSubmitResponse])
async def app_submit_ocr(image_id: int, ocr: OcrInput):
    """모바일 OCR 결과를 저장한다(명세 4-3).

    개정 명세는 OCR 을 4-1 요청에 함께 받도록 바뀌었지만, 명세표에 4-3 이 그대로
    살아 있어 별도 제출도 받는다. 같은 내용의 재전송은 멱등하게 처리한다.
    """
    import asyncio

    found = await asyncio.to_thread(search.get_image, image_id)
    if found is None:
        return responses.failure("IMAGE_NOT_FOUND", "이미지를 찾을 수 없습니다.",
                                 f"imageId: {image_id}", http_status=404)

    incoming = (ocr.refined_text or ocr.raw_text or "").strip()
    existing = (found.get("raw_text") or "").strip()
    if existing and existing != incoming:
        return responses.failure("OCR_ALREADY_SUBMITTED",
                                 "다른 OCR 결과가 이미 제출되었습니다.",
                                 f"imageId: {image_id}", http_status=409)

    await asyncio.to_thread(search.save_ocr, image_id, incoming,
                            ocr.lang, ocr.confidence)
    return responses.success({
        "imageId": image_id,
        "ocr": {"stage": "OCR", "status": "COMPLETED"},
    })


# ── 5-1 분석 현황 요약 ────────────────────────────────────────────────────
@app.get("/api/v1/analysis/summary", dependencies=[Depends(require_internal_token)],
         response_model=ApiResponse[AnalysisSummary])
async def app_analysis_summary(user_id: CurrentUserId):
    jobs = job_store.list_by_user(user_id)

    stage_counts: dict[str, dict[str, int]] = {}
    overall = {"QUEUED": 0, "PROCESSING": 0, "COMPLETED": 0,
               "FAILED": 0, "EMPTY": 0, "CANCELED": 0}
    for job in jobs:
        stage, status = job["stage"], job["status"]
        stage_counts.setdefault(stage, {})
        stage_counts[stage][status] = stage_counts[stage].get(status, 0) + 1
        if status in overall:
            overall[status] += 1

    summary = AnalysisSummary(
        total=len(jobs), stage_counts=stage_counts, overall_counts=overall
    )
    return responses.success(summary.model_dump(by_alias=True))


# ── 5-6 분석 작업 목록 ────────────────────────────────────────────────────
@app.get("/api/v1/analysis/jobs", dependencies=[Depends(require_internal_token)],
         response_model=ApiResponse[PageData[AnalysisJob]])
async def app_analysis_jobs(
    user_id: CurrentUserId,
    status: str | None = Query(None),
    page: int = Query(0),
    size: int = Query(20),
):
    jobs = job_store.list_by_user(user_id)
    if status:
        wanted = {s.strip().upper() for s in status.split(",") if s.strip()}
        jobs = [j for j in jobs if j["status"] in wanted]

    # 최근 갱신 순으로 보여 준다.
    jobs.sort(key=lambda j: j.get("updated_at") or "", reverse=True)
    total = len(jobs)
    window = jobs[max(0, page) * size: max(0, page) * size + size]

    items = [
        AnalysisJob(
            job_id=j["job_id"], image_id=j["image_id"],
            stage=j["stage"], status=j["status"],
            retryable=bool(j.get("error", {}) or {}) and
                      bool((j.get("error") or {}).get("retryable")),
            error_code=(j.get("error") or {}).get("code"),
            updated_at=j.get("updated_at"),
        ).model_dump(by_alias=True)
        for j in window
    ]
    return responses.success(responses.page_data(items, page, size, total))


# ── 6-1 이미지 목록 ───────────────────────────────────────────────────────
@app.get("/api/v1/images", dependencies=[Depends(require_internal_token)],
         response_model=ApiResponse[PageData[ImageListItem]])
async def app_image_list(
    user_id: CurrentUserId,
    status: str | None = Query(None, description="분석 상태 필터, 콤마로 복수 전달"),
    category_id: int | None = Query(None, alias="categoryId"),
    tag_id: int | None = Query(None, alias="tagId"),
    favorite: bool | None = Query(None),
    date_from: str | None = Query(None, alias="dateFrom", description="ISO-8601"),
    date_to: str | None = Query(None, alias="dateTo", description="ISO-8601"),
    page: int = Query(0),
    size: int = Query(20),
    sort: str | None = Query(None, description="기본 createdAt,desc"),
):
    statuses = [s.strip().upper() for s in status.split(",") if s.strip()] if status else None
    try:
        import asyncio
        category, tag = await _resolve_filters(user_id, category_id, tag_id)
        images, total = await asyncio.to_thread(
            search.list_images, user_id, page, size, category, tag,
            statuses, favorite, date_from, date_to, sort,
        )
    except search.InvalidSortError:
        raise
    except Exception as e:
        logger.exception("이미지 목록 조회 실패")
        return responses.failure("INTERNAL_ERROR", "이미지 목록을 조회하지 못했습니다.",
                                 str(e)[:300], http_status=500)

    items = [_to_list_item(img).model_dump(by_alias=True) for img in images]
    return responses.success(responses.page_data(items, page, size, total))


# ── 6-2 이미지 상세 ───────────────────────────────────────────────────────
@app.get("/api/v1/images/{image_id}", dependencies=[Depends(require_internal_token)],
         response_model=ApiResponse[ImageDetail])
async def app_image_detail(image_id: int):
    try:
        import asyncio
        found = await asyncio.to_thread(search.get_image, image_id)
    except Exception as e:
        logger.exception("이미지 상세 조회 실패")
        return responses.failure("INTERNAL_ERROR", "이미지를 조회하지 못했습니다.",
                                 str(e)[:300], http_status=500)

    if found is None:
        return responses.failure("IMAGE_NOT_FOUND", "이미지를 찾을 수 없습니다.",
                                 f"imageId: {image_id}", http_status=404)

    # 명세 6-2: 태그·필드의 출처는 전부 AGENT 다(6-3 사용자 수정이 범위 밖).
    field_sources = {
        name: "AGENT"
        for name in ("title", "summary", "tags", "categories")
        if found.get({"tags": "tags", "categories": "category"}.get(name, name))
    }
    detail = ImageDetail(
        image_id=found["image_id"],
        file_name=found.get("file_name"),
        content_hash=found.get("content_hash"),
        status=found.get("status") or "COMPLETED",
        favorite=found.get("favorite", False),
        title=found.get("title", ""),
        summary=found.get("summary", ""),
        ocr=OcrInput(raw_text=found.get("raw_text", ""),
                     lang=found.get("ocr_lang"),
                     confidence=found.get("ocr_confidence")),
        tags=_tag_refs(found.get("tags") or [], source="AGENT"),
        categories=_category_refs(found.get("category"),
                                  found.get("category_confidence")),
        field_sources=field_sources,
        key_information=found.get("key_information") or [],
        image_url=_image_url(found["image_id"], found.get("s3_key")),
        created_at=found.get("created_at"),
        uploaded_at=found.get("uploaded_at"),
        updated_at=found.get("created_at"),
        last_viewed_at=found.get("last_viewed_at"),
    )

    # 명세 8-1: lastViewedAt 은 6-2 상세 조회 성공 시에만 갱신한다.
    # 응답에는 이번 조회 이전 값을 담아 내려보낸다(방금 본 시각을 돌려주면 무의미하다).
    await asyncio.to_thread(search.touch_last_viewed, found["image_id"], _now_iso())
    return responses.success(detail.model_dump(by_alias=True))


# ── 6-6 썸네일 조회 ───────────────────────────────────────────────────────
@app.get("/api/v1/images/{image_id}/thumbnail",
         dependencies=[Depends(require_internal_token)],
         response_model=ApiResponse[ThumbnailResponse])
async def app_image_thumbnail_meta(image_id: int):
    """명세 6-6 Response data: `{thumbnailUrl, title, tags}`.

    `thumbnailUrl` 은 아래 `/raw` 경로를 가리킨다. 만료가 없어 앱이 캐시하기 좋고,
    스토리지를 외부에 공개하지 않아도 된다.
    """
    import asyncio

    found = await asyncio.to_thread(search.get_image, image_id)
    if found is None:
        return responses.failure("IMAGE_NOT_FOUND", "이미지를 찾을 수 없습니다.",
                                 f"imageId: {image_id}", http_status=404)
    if not found.get("s3_key"):
        return responses.failure("THUMBNAIL_NOT_FOUND", "썸네일이 없습니다.",
                                 f"imageId: {image_id}", http_status=404)
    return responses.success({
        "thumbnailUrl": _thumbnail_url(image_id, found.get("s3_key")),
        "title": found.get("title", ""),
        # 명세 6-6 의 tags 는 이름 배열이다(6-1·6-2 의 객체 배열과 다르다).
        "tags": found.get("tags") or [],
    })


@app.get("/api/v1/images/{image_id}/thumbnail/raw",
         dependencies=[Depends(require_internal_token)],
         response_class=Response,
         responses={200: {"content": {"image/jpeg": {}},
                          "description": "썸네일 JPEG 바이너리"}})
async def app_image_thumbnail(image_id: int):
    """썸네일 바이너리(명세 6-6 의 thumbnailUrl 이 가리키는 실제 이미지).

    명세 6-6 본문은 `{thumbnailUrl, title, tags}` JSON 이라 그 형식은 상위 경로가
    담당하고, 이미지 자체는 이 경로가 준다. thumbnailUrl 을 스토리지 presigned GET
    으로 주지 않는 이유: presigned 는 만료(1시간)가 있어 앱의 이미지 캐시가 매번
    깨지고, 스토리지를 외부에 공개해야 한다. 이 경로는 만료가 없다.

    분석할 때 원본과 같은 key 로 썸네일 버킷에 올려 두므로 보통은 그것을 그대로 읽는다.
    없으면(분석 전 이미지, 저장 실패) 원본에서 즉석 생성해 내려주고 그때 버킷에도 채운다.

    presigned URL 을 저장했다가 돌려주면 만료로 깨지므로 주소는 이 경로로 고정한다.
    이 응답만 공통 envelope 를 쓰지 않는다.
    """
    import asyncio

    found = await asyncio.to_thread(search.get_image, image_id)
    if found is None:
        return responses.failure("IMAGE_NOT_FOUND", "이미지를 찾을 수 없습니다.",
                                 f"imageId: {image_id}", http_status=404)

    image_ref = found.get("s3_key")
    if not image_ref:
        return responses.failure("THUMBNAIL_NOT_FOUND", "썸네일을 만들 원본이 없습니다.",
                                 f"imageId: {image_id}", http_status=404)

    thumb: bytes | None = None
    if get_settings().s3_thumbnail_bucket:
        try:
            thumb = await asyncio.to_thread(storage.fetch_thumbnail, image_ref)
        except Exception as e:
            logger.info("저장된 썸네일 없음 imageId=%s: %s — 즉석 생성", image_id, e)

    if thumb is None:
        try:
            thumb = await asyncio.to_thread(storage.store_thumbnail, image_ref)
        except Exception as e:
            logger.warning("썸네일 생성 실패 imageId=%s: %s", image_id, e)
            return responses.failure("THUMBNAIL_NOT_FOUND", "썸네일을 가져오지 못했습니다.",
                                     str(e)[:200], http_status=404)

    return Response(content=thumb, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


# ── 원본 이미지 조회 (명세 6-8 의 바이너리 버전) ──────────────────────────
@app.get("/api/v1/images/{image_id}/source",
         dependencies=[Depends(require_internal_token)],
         response_class=Response,
         responses={200: {"content": {"image/*": {}},
                          "description": "원본 이미지 바이너리"}})
async def app_image_source(image_id: int):
    """원본 이미지를 그대로 돌려준다.

    썸네일(`/thumbnail/raw`)은 정사각으로 잘려 있어 스크린샷 내용이 다 안 보인다.
    상세 화면처럼 전체를 봐야 하는 곳은 이 경로를 쓴다.

    원본은 수 MB 라 목록에서 여러 장을 한꺼번에 부르면 느려진다. 목록·격자는
    썸네일을, 상세는 이 경로를 쓰는 것을 전제로 한다.
    """
    import asyncio

    found = await asyncio.to_thread(search.get_image, image_id)
    if found is None:
        return responses.failure("IMAGE_NOT_FOUND", "이미지를 찾을 수 없습니다.",
                                 f"imageId: {image_id}", http_status=404)
    s3_key = found.get("s3_key")
    if not s3_key:
        return responses.failure("SOURCE_IMAGE_NOT_FOUND", "원본 이미지가 없습니다.",
                                 f"imageId: {image_id}", http_status=404)
    try:
        data = await asyncio.to_thread(storage.fetch_image, s3_key)
    except Exception as e:
        logger.warning("원본 조회 실패 imageId=%s: %s", image_id, e)
        return responses.failure("SOURCE_IMAGE_NOT_FOUND", "원본을 가져오지 못했습니다.",
                                 str(e)[:200], http_status=404)

    # 확장자로 미디어 타입을 정한다. 모르면 브라우저·이미지 로더가 알아서 판별하도록 둔다.
    ext = s3_key.rsplit(".", 1)[-1].lower() if "." in s3_key else ""
    media = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
             "webp": "image/webp", "heic": "image/heic",
             "heif": "image/heif"}.get(ext, "application/octet-stream")
    return Response(content=data, media_type=media,
                    headers={"Cache-Control": "public, max-age=86400"})


# ── 7-1 태그 목록 ─────────────────────────────────────────────────────────
@app.get("/api/v1/tags", dependencies=[Depends(require_internal_token)],
         response_model=ApiResponse[PageData[TagItem]])
async def app_tag_list(
    user_id: CurrentUserId,
    q: str | None = Query(None, description="태그명 부분 일치(자동완성용)"),
    page: int = Query(0),
    size: int = Query(20),
    sort: str | None = Query(None, description="usageCount,desc(기본)/name,asc/createdAt,desc"),
):
    # 명세 7-1 정렬. 집계 결과가 이미 메모리에 있어 여기서 정렬한다.
    # createdAt 은 태그에 생성시각이 없어(이름 집계라) usageCount 순으로 대체한다.
    tag_sorts = {"usageCount,desc", "name,asc", "createdAt,desc"}
    sort_value = (sort or "usageCount,desc").strip()
    if sort_value not in tag_sorts:
        raise search.InvalidSortError(f"지원하지 않는 정렬입니다: {sort_value}")

    try:
        import asyncio
        items = await asyncio.to_thread(search.aggregate_tags, user_id, 500)
    except Exception as e:
        logger.exception("태그 조회 실패")
        return responses.failure("INTERNAL_ERROR", "태그를 조회하지 못했습니다.",
                                 str(e)[:300], http_status=500)

    if q:
        needle = q.strip().lower()
        items = [i for i in items if needle in i["name"].lower()]

    if sort_value == "name,asc":
        items.sort(key=lambda i: i["name"])
    else:
        items.sort(key=lambda i: (-i["count"], i["name"]))

    total = len(items)
    window = items[max(0, page) * size: max(0, page) * size + size]
    tags = [
        TagItem(tag_id=search.stable_id(i["name"]), name=i["name"],
                usage_count=i["count"]).model_dump(by_alias=True)
        for i in window
    ]
    return responses.success(responses.page_data(tags, page, size, total))


# ── 7-2 카테고리 목록 ─────────────────────────────────────────────────────
@app.get("/api/v1/categories", dependencies=[Depends(require_internal_token)],
         response_model=ApiResponse[CategoryListData])
async def app_category_list(
    user_id: CurrentUserId,
    sort: str | None = Query(
        None,
        description="name,asc(기본)/updatedAt,desc(최신 업로드순)/imageCount,desc(사진 많은 순)",
    ),
):
    """카테고리 목록(명세 7-2).

    **페이지네이션은 프론트 요청으로 뺐다**(명세와 다른 부분, 팀 합의).
    카테고리는 기본 17종에 AGENT 가 분석 중 새로 만든 것이 더해지는 정도라 수가 적다.
    사람이 직접 만드는 경로는 없다. 그래서 항상 전체를 한 번에 돌려주고,
    `page`·`size` 를 보내도 무시한다.
    """
    # 명세 7-2 정렬. 카테고리 화면의 정렬 드롭다운이 이 세 값을 쓴다.
    category_sorts = {"name,asc", "updatedAt,desc", "imageCount,desc"}
    sort_value = (sort or "name,asc").strip()
    if sort_value not in category_sorts:
        raise search.InvalidSortError(f"지원하지 않는 정렬입니다: {sort_value}")

    try:
        import asyncio
        items = await asyncio.to_thread(search.aggregate_categories, user_id, 500)
    except Exception as e:
        logger.exception("카테고리 조회 실패")
        return responses.failure("INTERNAL_ERROR", "카테고리를 조회하지 못했습니다.",
                                 str(e)[:300], http_status=500)

    if sort_value == "updatedAt,desc":
        items.sort(key=lambda i: i.get("last_updated_at") or "", reverse=True)
    elif sort_value == "imageCount,desc":
        items.sort(key=lambda i: (-i["count"], i["name"]))
    else:
        items.sort(key=lambda i: i["name"])

    cards = [
        CategoryCard(
            category_id=search.stable_id(i["name"]),
            name=i["name"],
            # 이 카테고리에 가장 최근 분류된 사진의 썸네일을 그대로 가리킨다.
            # 카테고리 전용 이미지를 따로 만들지 않는다(사진마다 썸네일 1장).
            thumbnail_url=(_thumbnail_url(i["thumbnail_image_id"],
                                         i.get("thumbnail_s3_key"))
                           if i.get("thumbnail_image_id") else None),
            image_count=i["count"],
            # 이 카테고리 안에서 각 태그가 몇 장에 붙어 있는지까지 함께 내려준다.
            tags=[TagCount(tag_id=search.stable_id(t["name"]),
                           name=t["name"], image_count=t["count"])
                  for t in i.get("tags") or []],
            updated_at=i.get("last_updated_at"),
        ).model_dump(by_alias=True)
        for i in items
    ]
    return responses.success({"list": cards})


# ── 7-3 홈 대시보드 요약 ─────────────────────────────────────────────────
# 진행률은 실제 퍼센트를 잴 수 없어 단계로 환산한다. 단계 수가 정해져 있어
# 사용자에게 보여줄 진행 막대로는 충분하고, 명세의 0~100 정수 계약도 지킨다.
_STAGE_PROGRESS = {"LLM": 25, "IMAGE_ANALYSIS": 50, "AGENT": 75, "INDEXING": 90}


@app.get("/api/v1/home", dependencies=[Depends(require_internal_token)],
         response_model=ApiResponse[HomeResponse])
async def app_home(user_id: CurrentUserId):
    """홈 화면에 필요한 것을 한 번에 모아 준다(명세 7-3).

    모바일 초기 로딩에서 5-1·7-2·6-1 을 각각 부르지 않아도 되게 하는 집계 API 다.
    """
    import asyncio

    jobs = job_store.list_by_user(user_id)
    queued = [j for j in jobs if j["status"] == "QUEUED"]
    processing = [j for j in jobs if j["status"] == "PROCESSING"]
    failed = [j for j in jobs if j["status"] == "FAILED"]

    active: ActiveAnalysis | None = None
    if processing:
        # 명세: "가장 먼저 시작된 진행 중 작업"
        first = min(processing, key=lambda j: j.get("created_at") or "")
        found = await asyncio.to_thread(search.get_image, first["image_id"])
        active = ActiveAnalysis(
            job_id=first["job_id"], image_id=first["image_id"],
            file_name=(found or {}).get("file_name"),
            title=(found or {}).get("title", ""),
            thumbnail_url=_thumbnail_url(first["image_id"],
                                         (found or {}).get("s3_key")),
            stage=first["stage"], status=first["status"],
            progress=_STAGE_PROGRESS.get(first["stage"], 0),
        )

    try:
        categories = await asyncio.to_thread(search.aggregate_categories, user_id, 500)
        # 명세: updatedAt,desc 로 최대 8개
        categories.sort(key=lambda c: c.get("last_updated_at") or "", reverse=True)
        # 명세: 분석 완료 시각 내림차순으로 최대 4개
        images, _ = await asyncio.to_thread(
            search.list_images, user_id, 0, 4, None, None,
            ["COMPLETED", "EMPTY"], None, None, None, "createdAt,desc",
        )
    except Exception as e:
        logger.exception("홈 집계 실패")
        return responses.failure("INTERNAL_ERROR", "홈 정보를 조회하지 못했습니다.",
                                 str(e)[:300], http_status=500)

    home = HomeResponse(
        home_date=_now_iso()[:10],
        analysis_status=HomeAnalysisStatus(
            has_active_jobs=bool(queued or processing),
            queued_count=len(queued),
            processing_count=len(processing),
            failed_count=len(failed),
            active_analysis=active,
        ),
        categories=[
            HomeCategory(
                category_id=search.stable_id(c["name"]),
                name=c["name"],
                image_count=c["count"],
                # 명세 7-3: 대표 태그 최대 3개 (7-2 의 4개와 다르다)
                tags=_tag_refs([t["name"] for t in (c.get("tags") or [])[:3]]),
                updated_at=c.get("last_updated_at"),
            )
            for c in categories[:8]
        ],
        recent_images=[
            HomeRecentImage(
                image_id=img["image_id"],
                title=img.get("title", ""),
                thumbnail_url=_thumbnail_url(img["image_id"], img.get("s3_key")),
                tags=_tag_refs(img.get("tags") or []),
                favorite=img.get("favorite", False),
                analyzed_at=img.get("created_at"),
            )
            for img in images
        ],
    )
    return responses.success(home.model_dump(by_alias=True))


# ── 8-1 통합 검색 ─────────────────────────────────────────────────────────
@app.get("/api/v1/search", dependencies=[Depends(require_internal_token)],
         response_model=ApiResponse[PageData[SearchResultItem]])
async def app_search(
    user_id: CurrentUserId,
    q: str = Query(...),
    scope: str = Query("ALL", description="ALL(기본)/OCR/TAG/STRUCTURED"),
    category_id: int | None = Query(None, alias="categoryId"),
    tag_id: int | None = Query(None, alias="tagId"),
    page: int = Query(0),
    size: int = Query(20),
    sort: str | None = Query(
        None,
        description="relevance(기본)/imageId,asc/lastViewedAt,desc/uploadedAt,desc/createdAt,desc",
    ),
):
    if not q.strip():
        return responses.failure("INVALID_PARAMETER", "요청 값이 올바르지 않습니다.",
                                 [{"field": "q", "message": "필수 값입니다."}])
    if scope.upper() not in ("ALL", "OCR", "TAG", "STRUCTURED"):
        return responses.failure("INVALID_PARAMETER", "요청 값이 올바르지 않습니다.",
                                 [{"field": "scope",
                                   "message": "ALL/OCR/TAG/STRUCTURED 중 하나여야 합니다."}])
    try:
        import asyncio
        category, tag = await _resolve_filters(user_id, category_id, tag_id)
        hits, total = await asyncio.to_thread(
            search.keyword_search, user_id, q, category, size, page, tag,
            scope, sort,
        )
    except search.InvalidSortError:
        raise
    except Exception as e:
        logger.exception("검색 실패")
        return responses.failure("INTERNAL_ERROR", "검색에 실패했습니다.",
                                 str(e)[:300], http_status=500)

    items = [
        SearchResultItem(
            image_id=h["image_id"],
            title=h.get("title", ""),
            summary=h.get("summary", ""),
            thumbnail_url=_thumbnail_url(h["image_id"], h.get("s3_key")),
            score=h.get("score", 0.0),
            tags=_tag_refs(h.get("tags") or []),
            last_viewed_at=h.get("last_viewed_at"),
            uploaded_at=h.get("uploaded_at"),
            created_at=h.get("created_at"),
        ).model_dump(by_alias=True)
        for h in hits
    ]
    return responses.success(responses.page_data(items, page, size, total))

