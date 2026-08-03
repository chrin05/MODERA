"""Spring 연동용 내부 API (`/internal/v1/*`).

명세 10 장. 앱 API 와 달리 공통 envelope 를 쓰지 않고
`{"error": CODE, "message": ..., "detail": {}}` 형식으로 에러를 낸다(`deps._error`).

라우트 본문은 main.py 에 있던 것을 그대로 옮겼다 — `@app.` 이 `@router.` 로
바뀐 것뿐이다. 인증은 라우트마다 걸린 `require_internal_token` 이 그대로 담당한다.
"""

import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import JSONResponse
from pydantic import Field

from . import document, gemini_client, llm, search
from .config import get_settings
from .deps import _error, require_internal_token
from .jobs import job_registry
from .related import ImageSearchCompletedEvent, ImageSearchPayload
from .schemas import (
    AnalyzeAccepted,
    AnalyzeRequest,
    CamelModel,
    DocumentRequest,
    DocumentResponse,
    EmbedRequest,
    EmbedResponse,
    EmbeddingItem,
    ParsedConditions,
    QueryParseRequest,
    QueryParseResponse,
    SearchHit,
    SearchRequest,
    SearchResponse,
)
from .stages import execute_stage

logger = logging.getLogger(__name__)

router = APIRouter()


# ── 10-1 단계별 분석 실행 요청 ────────────────────────────────────────────
_REQUIRED_INPUT = {
    "LLM": ("ocr",),
    "IMAGE_ANALYSIS": ("image",),
    "AGENT": ("ocr", "image_analysis"),
    # FULL 은 원본 이미지만 있으면 된다. ocr 은 있으면 쓰고, 없으면
    # 이미지 분석에서 읽어낸 텍스트로 대신한다(stages._run_full_pipeline 참고).
    "FULL": ("image",),
}


@router.post("/internal/v1/analyze", dependencies=[Depends(require_internal_token)])
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
@router.post("/internal/v1/embed", dependencies=[Depends(require_internal_token)])
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
@router.post("/internal/v1/query/parse", dependencies=[Depends(require_internal_token)])
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
            llm.generate_json, settings.query_parse_model_name, [prompt]
        )
        conditions = ParsedConditions.model_validate(
            {k: v for k, v in parsed.items() if k != "confidence"}
        )
        response = QueryParseResponse(
            model_version=settings.query_parse_model_name,
            parsed_conditions=conditions,
            confidence=float(parsed.get("confidence", 0.0)),
        )
    except Exception as e:
        # 완전 실패도 에러 코드로 올리지 않고 parsedConditions=null 로 degrade
        logger.warning("조건 변환 실패: %s", e)
        response = QueryParseResponse(
            model_version=settings.query_parse_model_name,
            parsed_conditions=None,
            confidence=0.0,
        )
    return JSONResponse(status_code=200, content=response.model_dump(by_alias=True))


# ── 검색 (Spring 이 호출하는 내부 검색 — 하이브리드/cascade) ─────────────
# 앱↔AI 직결 종료 후 사용자 검색은 전부 Spring 을 거쳐 여기로 온다. 그래서 기본
# 모드는 cascade(자연어/시맨틱 검색이 살아 있는 경로)다. Spring 은 응답의 imageId
# 순서를 유지한 채 자기 DB(조회 사본)를 조인해 앱 응답을 조립한다.
@router.post("/internal/v1/search", dependencies=[Depends(require_internal_token)])
async def keyword_search(request: SearchRequest):
    if not request.query or not request.query.strip():
        return _error("INVALID_REQUEST", "query 가 비어 있습니다.", http_status=400)
    if request.scope.upper() not in ("ALL", "OCR", "TAG", "STRUCTURED"):
        return _error("INVALID_REQUEST",
                      "scope 는 ALL/OCR/TAG/STRUCTURED 중 하나여야 합니다.", http_status=400)
    if request.mode not in ("auto", "bm25"):
        return _error("INVALID_REQUEST", "mode 는 auto/bm25 중 하나여야 합니다.",
                      http_status=400)
    try:
        import asyncio
        hits, total = await asyncio.to_thread(
            search.keyword_search,
            request.user_id, request.query, request.category, request.size,
            request.page, request.tag, request.scope, request.sort,
            # auto → hybrid=None(cascade: BM25 먼저, 빈 결과면 시맨틱 승격).
            # bm25 → hybrid=False(강제 BM25 — 정확 total·BM25 점수 스케일 보장).
            hybrid=(False if request.mode == "bm25" else None),
        )
    except search.InvalidSortError as e:
        # 내부 API 는 앱 envelope 가 아니라 raw 에러 형식을 쓴다.
        return _error("INVALID_SORT", str(e), http_status=400)
    except Exception as e:
        logger.exception("검색 실패")
        return _error("SEARCH_FAILED", str(e)[:500], http_status=502)

    response = SearchResponse(total=total, page=request.page, size=request.size,
                              hits=[SearchHit(**h) for h in hits])
    return JSONResponse(status_code=200, content=response.model_dump(by_alias=True))


# ── 5-5 자연어 기반 AI 이미지 검색 ────────────────────────────────────────
class SemanticSearchRequest(CamelModel):
    # 0 이하 차단 이유는 related.RelatedSearchRequest 와 같다(0 = 전역 시드 소유자).
    user_id: int = Field(gt=0)
    query: str
    # analysis-worker 가 Redis 이벤트에서 그대로 실어 보낸다. 없으면 서버가 만든다.
    correlation_id: str | None = None
    page: int = Field(default=0, ge=0)
    size: int = Field(default=20, ge=1, le=100)


@router.post("/internal/v1/images/search/semantic",
             dependencies=[Depends(require_internal_token)],
             response_model=ImageSearchCompletedEvent,
             responses={400: {"description": "INVALID_REQUEST"},
                        502: {"description": "SEARCH_FAILED"}})
async def semantic_search(request: SemanticSearchRequest):
    """명세 5-5 의 AI 구간. analysis-worker 가 IMAGE_SEMANTIC_SEARCH_REQUESTED
    이벤트를 받아 이걸 부르고, 응답(IMAGE_SEARCH_COMPLETED 봉투)을 결과
    스트림에 그대로 싣는다 — 연관 이미지·문서화 후보와 같은 봉투다.

    검색은 /internal/v1/search 의 기본 모드와 같은 cascade(F4): BM25 먼저,
    빈 결과면 시맨틱(하이브리드) 승격. "문장 또는 단어" 모두 이 경로 하나로
    처리한다 — 질의를 '자연어인지' 분류하지 않는다(keyword_search docstring).
    """
    correlation_id = request.correlation_id or str(uuid.uuid4())
    query = request.query.strip()
    if not query:
        return _error("INVALID_REQUEST", "query 가 비어 있습니다.", http_status=400)
    try:
        import asyncio
        hits, total = await asyncio.to_thread(
            search.keyword_search, request.user_id, query,
            None, request.size, request.page,
        )
    except Exception as e:
        logger.exception("자연어 검색 실패 userId=%s", request.user_id)
        return _error("SEARCH_FAILED", str(e)[:500], http_status=502)

    event = ImageSearchCompletedEvent(payload=ImageSearchPayload(
        correlation_id=correlation_id, total=total,
        page=request.page, size=request.size,
        hits=[SearchHit(**h) for h in hits],
    ))
    return JSONResponse(status_code=200, content=event.model_dump(by_alias=True))


# ── 문서화 (분석 완료 이미지 → 마크다운) ─────────────────────────────────
@router.post("/internal/v1/documents", dependencies=[Depends(require_internal_token)],
          response_model=DocumentResponse,
          responses={400: {"description": "INVALID_REQUEST / NO_DOCUMENT_SOURCE"},
                     502: {"description": "DOCUMENT_GENERATION_FAILED"}})
async def create_document(request: DocumentRequest):
    """분석이 끝난 이미지들을 묶어 하나의 마크다운 문서로 만든다.

    분석 파이프라인과 무관한 별개 기능이다. 새로 분석하지 않는다.

    재료(`images`)는 **Spring 이 요청 본문에 실어 보낸다.** 10-1 과 같은 방식이라
    AI 는 아무것도 조회하지 않는다. 어떤 이미지가 이 사용자 것이고 분석이
    끝났는지는 Spring 이 질의 단계에서 이미 거른 상태여야 한다.

    응답의 `markdown` 이 최종 산출물이다. Spring 은 이 문자열을 그대로 저장하거나
    앱에 내려보내면 된다.
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
