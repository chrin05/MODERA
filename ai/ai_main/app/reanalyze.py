"""카테고리 재분석 — 기능 전부가 이 파일에 있다.

    ① 사용자가 어떤 이미지의 카테고리 결과를 거부한다
    ② POST /internal/v1/categories/reanalyze {imageId, excludedCategoryIds: [3]}
    ③ 거부된 카테고리를 후보에서 빼고 다시 판정한다 → 새 카테고리 1개
    ④ 또 거부하면 누적해서 보낸다 {excludedCategoryIds: [3, 5]} — 최대 5회(MAX_EXCLUDED)

**거부 목록은 서버가 들고 있지 않다.** `doc_selection.py` 와 같은 이유로 매 호출에
누적 배열을 받는다 — 서버 세션으로 만들면 재기동·다중 인스턴스에서 날아간다.
덕분에 "최대 5회" 제한도 배열 길이 하나로 끝난다(pydantic max_length).

재분석은 **카테고리만** 바꾼다. 제목·요약·태그·OCR 은 그대로 둔다(사용자가 거부한
것은 분류 결과 하나다). 원본 이미지도 다시 읽지 않는다 — 색인에 남은 분석 결과를
재료로 쓰므로 비전 호출 0회, LLM 1회 + 임베딩 1회로 끝난다.

판정 코어는 `category.resolve_category`, 후보 수집은 `stages.build_candidates` 를
그대로 쓴다. 임계값·오염 가드도 분석 경로와 같은 설정을 본다 — 복사하면 한쪽만
튜닝돼서 "재분석하면 기준이 달라지는" 사고가 난다.

main.py 는 `include_router` 한 줄만 닿는다. 되돌리려면 이 파일과 그 한 줄만 지운다.
"""

import asyncio
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import Field

from . import category_store, gemini_client, llm, search, spring_client, stages
from .category import normalize_name, resolve_category
from .config import get_settings
from .deps import _error
from .schemas import CamelModel, CategoryCandidate

logger = logging.getLogger(__name__)

router = APIRouter()

# 재분석 최대 반복 횟수. 누적 배열 길이가 곧 시도 횟수라 이 상한이 그대로 회수 제한이다.
MAX_EXCLUDED = 5


class ReanalyzeRequest(CamelModel):
    image_id: int = Field(gt=0)
    # 지금까지 사용자가 거부한 카테고리 전부(누적). 6번째 시도는 여기서 400 으로 끊긴다.
    excluded_category_ids: list[int] = Field(min_length=1, max_length=MAX_EXCLUDED)


class ReanalyzeResponse(CamelModel):
    image_id: int
    # 기존 카테고리로 판정되면 그 id(Spring 이 준 값), 신규면 이름 해시(search.stable_id).
    category_id: int
    category_name: str


def _excluded(candidate: CategoryCandidate, ids: set[int]) -> bool:
    """거부 대상 후보인지. Spring 의 categoryId 와 이 서버의 이름 해시를 둘 다 본다 —
    호출자가 어느 쪽 id 를 들고 있는지(Spring 경유/앱 직결)가 갈린다."""
    return candidate.category_id in ids or search.stable_id(candidate.name) in ids


def _rejected(name: str, excluded_keys: set[str]) -> bool:
    """거부된 이름인지. 표기 변형(쇼핑↔쇼핑몰)도 같은 것으로 본다 —
    `resolve_category` 의 name-variant 규칙과 같은 기준이라, 거부한 카테고리가
    이름만 살짝 바뀐 신규로 되돌아오는 경로를 막는다."""
    key = normalize_name(name)
    if not key:
        return True
    return any(
        key == ex or (len(key) >= 2 and len(ex) >= 2 and (key in ex or ex in key))
        for ex in excluded_keys
    )


def _pick_category(text: str, candidate_names: list[str], excluded_names: list[str]) -> str:
    """거부 목록을 뺀 상태로 카테고리 하나를 다시 제안받는다.

    분석 경로(`stages.run_agent_generation`)의 카테고리 규칙만 떼어 온 프롬프트다.
    제목·태그·주요정보는 이미 있는 것을 쓰므로 다시 만들지 않는다.
    """
    prompt = (
        "아래는 스크린샷 1장의 기존 분석 결과다. 이 이미지에 맞는 카테고리 하나를 다시 정하라.\n"
        f"기존 카테고리 후보: {candidate_names}. 내용과 무리 없이 맞는 후보가 있으면 그것을 골라라. "
        "어느 후보도 맞지 않으면 새 카테고리 이름을 제안하라(2~6자). "
        "새 이름은 여러 이미지가 공유할 수 있는 주제 단위여야 하고, "
        "브랜드명·상호명·장소명·제품명은 카테고리로 만들지 마라.\n"
        f"**사용자가 이미 거부한 이름이다: {excluded_names}. 고르지도, 제안하지도 마라 — "
        "축약·변형(예: '쇼핑' 거부 → '쇼핑몰')도 금지다.**\n"
        f"'{stages.EMPTY_CATEGORY}'는 고르지 마라 — 맞는 후보가 없으면 반드시 새 이름을 제안하라.\n"
        "반드시 아래 JSON만 출력. 마크다운·설명 금지.\n"
        '{"category":"...","reason":"간단한 근거"}\n\n'
        f"분석 결과:\n{text}"
    )
    parsed = llm.generate_json(get_settings().llm_model_name, [prompt])
    return str(parsed.get("category") or "").strip()


def _source_text(found: dict) -> str:
    """프롬프트 재료. 색인에 남은 분석 결과 + OCR 원문(길이 제한)."""
    parts = [found.get("title") or "", found.get("summary") or ""]
    parts += [str(k) for k in (found.get("key_information") or [])]
    parts.append((found.get("raw_text") or "")[:2000])
    return "\n".join(p for p in parts if p.strip()).strip()


# 토큰 검사는 main.py 가 include_router 에서 걸어 준다(내부 API 공통 규칙).
@router.post("/internal/v1/categories/reanalyze",
             response_model=ReanalyzeResponse,
             responses={400: {"description": "INVALID_REQUEST / NO_ANALYSIS_SOURCE"},
                        404: {"description": "IMAGE_NOT_FOUND"},
                        502: {"description": "REANALYZE_FAILED"}})
async def reanalyze_category(request: ReanalyzeRequest):
    """거부된 카테고리를 배제하고 다시 판정한다. 카테고리 외의 분석 결과는 그대로다."""
    settings = get_settings()
    found = await asyncio.to_thread(search.get_image, request.image_id)
    if found is None:
        return _error("IMAGE_NOT_FOUND", f"imageId: {request.image_id}", http_status=404)

    # 요약을 임베딩한다 — 카테고리 centroid 가 요약 임베딩의 평균이라(분석 경로와
    # 같은 벡터 공간) 다른 텍스트를 넣으면 유사도 기준이 조용히 어긋난다.
    summary = (found.get("summary") or found.get("title") or "").strip()
    if not summary:
        return _error(
            "NO_ANALYSIS_SOURCE",
            "재분석할 분석 결과가 없습니다(분석 전이거나 EMPTY).", http_status=400)

    user_id = found.get("user_id") or 0
    excluded_ids = set(request.excluded_category_ids)
    knowledge = await spring_client.fetch_knowledge_candidates(user_id)
    candidates = await asyncio.to_thread(
        stages.build_candidates, user_id, knowledge.categories
    )

    kept = [c for c in candidates if not _excluded(c, excluded_ids)]
    excluded_names = [c.name for c in candidates if _excluded(c, excluded_ids)]
    # 지금 붙어 있는 카테고리는 무조건 뺀다. 사용자가 거부한 바로 그것인데, id 체계가
    # 어긋나면(Spring DB id vs 이름 해시) 위 필터에 안 걸려 같은 카테고리를 그대로
    # 돌려주는 무의미한 응답이 된다.
    current = (found.get("category") or "").strip()
    if current:
        kept = [c for c in kept if normalize_name(c.name) != normalize_name(current)]
        if current not in excluded_names:
            excluded_names.append(current)
    if not excluded_names:
        logger.warning("재분석 excludedCategoryIds %s 가 어떤 카테고리에도 안 붙었다 imageId=%s",
                       sorted(excluded_ids), request.image_id)
    excluded_keys = {normalize_name(n) for n in excluded_names}

    try:
        proposed = await asyncio.to_thread(
            _pick_category, _source_text(found), [c.name for c in kept], excluded_names
        )
        if not proposed:
            raise ValueError("모델이 카테고리를 주지 않았습니다.")

        # 이름 중복 관문용 이름 임베딩을 요약과 같은 배치에 실어 보낸다(추가 호출 0회).
        proposed_key = normalize_name(proposed)
        name_matched = any(normalize_name(c.name) == proposed_key for c in kept)
        name_slots = [] if (name_matched or not kept) else [proposed] + [c.name for c in kept]
        _, vectors = await asyncio.to_thread(
            gemini_client.embed, [summary] + name_slots, "DOCUMENT"
        )
    except Exception as e:
        logger.exception("카테고리 재분석 실패 imageId=%s", request.image_id)
        return _error("REANALYZE_FAILED", str(e)[:500], http_status=502)

    name_vectors = {normalize_name(n): v for n, v in zip(name_slots, vectors[1:])}
    resolution = resolve_category(
        vectors[0], proposed, kept, settings.category_name_dup_threshold,
        name_vectors=name_vectors,
        proposed_name_vector=name_vectors.get(proposed_key),
    )

    name, category_id = resolution.name, resolution.category_id
    confidence = round(resolution.similarity, 3)
    absorbed = resolution.matched_by in ("name", "name-variant", "name-dup")
    if _rejected(name, excluded_keys):
        # 모델이 거부된 이름(또는 그 변형)을 그대로 제안했고 후보 흡수도 안 된 경우.
        # 이름이 가장 가까운 남은 후보로 떨어진다.
        # ponytail: 재프롬프트 없이 1회 폴백. 이 경고가 자주 뜨면 프롬프트를 손볼 것.
        fallback = next((n for _, n in resolution.ranking
                         if not _rejected(n, excluded_keys)), None)
        if fallback is None:
            return _error(
                "REANALYZE_FAILED", "배제 후 남은 카테고리 후보가 없습니다.", http_status=502)
        logger.warning("재분석이 거부된 이름 '%s' 을 재제안 → 폴백 '%s' imageId=%s",
                       name, fallback, request.image_id)
        name = fallback
        category_id = next((c.category_id for c in kept if c.name == fallback), None)
        confidence, absorbed = 0.0, True  # 폴백은 근거가 약하다 → centroid 누적 금지

    # 분석 경로와 같은 오염 가드. 이름으로 흡수된 판정인데 감사 코사인이 낮으면
    # centroid 에 누적하지 않는다(수동 교정 경로가 없는 제품이라 오염이 남는다).
    #
    # ponytail: 이전 카테고리 centroid 에서 이 이미지 몫을 빼지는 않는다(누적 upsert 라
    # 감산 경로가 없다). 재분석 비율이 높아지면 카테고리별 재계산 배치를 붙일 것.
    guard = settings.category_guard_min_cosine
    if guard > 0 and absorbed and confidence < guard:
        logger.warning("재분석 오염 가드: '%s' 감사 코사인 %.3f < %.2f — centroid 누적 생략",
                       name, confidence, guard)
    else:
        try:
            await asyncio.to_thread(category_store.upsert_category_vector, user_id, name, vectors[0])
        except Exception as e:
            logger.warning("카테고리 벡터 갱신 실패 '%s': %s", name, e)

    # 색인의 카테고리만 갈아 끼운다. 실패해도 판정 결과는 돌려준다(분석 경로와 같은
    # best-effort). Spring 이 자기 DB 를 최종 보관하고, 여기 값은 조회 사본이다.
    try:
        await asyncio.to_thread(search.set_category, request.image_id, name, confidence)
        await asyncio.to_thread(search.refresh_index)
    except Exception as e:
        logger.warning("재분석 색인 갱신 실패 imageId=%s: %s", request.image_id, e)

    logger.info("카테고리 재분석 imageId=%s 배제=%s 제안=%s → %s (기준=%s, 신규=%s)",
                request.image_id, excluded_names, proposed, name,
                resolution.matched_by, resolution.created)
    body = ReanalyzeResponse(
        image_id=request.image_id,
        category_id=category_id or search.stable_id(name),
        category_name=name,
    )
    return JSONResponse(status_code=200, content=body.model_dump(by_alias=True))
