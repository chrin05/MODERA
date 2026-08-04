"""OpenSearch 키워드 검색 (FastAPI 전담).

- 색인: AGENT 단계가 끝나면 요약·태그·카테고리·OCR 원문을 색인한다.
  검색용 시맨틱 벡터(로컬 bge-m3)도 knn_vector 필드에 함께 색인한다(F2).
- 검색: BM25(multi_match) 키워드 검색. 한글은 nori analyzer 로 형태소 분석한다.
  하이브리드(BM25+kNN)는 F3 에서 이 벡터를 이용해 얹는다.

SDK(opensearch-py) 의존성을 이 파일에만 가둔다.
"""

import logging
import time
import zlib
from functools import lru_cache
from typing import Any

from .config import get_settings
from . import embedder
from .category import normalize_name

logger = logging.getLogger(__name__)

# 한글 형태소 분석기. nori 플러그인이 설치돼 있어야 한다(도커 이미지에 포함).
# 이 이름의 analyzer 를 title·summary·tags·raw_text 가 쓴다. nori 가 없는
# OpenSearch 로 바꾸면 인덱스 생성 자체가 실패한다.
_KOREAN_ANALYZER = "korean"

# 인덱스 존재 확인 캐시. 0 이면 아직 확인 전.
_index_checked_at = 0.0
_INDEX_CHECK_TTL = 60.0


class SearchError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _client():
    try:
        from opensearchpy import OpenSearch
    except ImportError as e:  # pragma: no cover
        raise SearchError("opensearch-py 가 설치되지 않았습니다.") from e

    settings = get_settings()
    http_auth = (
        (settings.opensearch_user, settings.opensearch_password)
        if settings.opensearch_user
        else None
    )
    return OpenSearch(
        hosts=[{"host": settings.opensearch_host, "port": settings.opensearch_port}],
        http_auth=http_auth,
        use_ssl=settings.opensearch_use_ssl,
        verify_certs=False,      # 내부망/자체서명 인증서 허용
        ssl_show_warn=False,
    )


def _index_body() -> dict[str, Any]:
    settings = get_settings()
    return {
        "settings": {
            "index": {
                "number_of_shards": 1,
                "number_of_replicas": 0,
                # 검색용 kNN(로컬 bge-m3 벡터) 활성화 — F2.
                "knn": True,
            },
            "analysis": {
                "analyzer": {
                    # nori_tokenizer 로 복합명사까지 분해해 한글 키워드 검색 품질을 높인다.
                    _KOREAN_ANALYZER: {
                        "type": "custom",
                        "tokenizer": "nori_tokenizer",
                        "filter": ["lowercase"],
                    }
                }
            },
        },
        "mappings": {
            "properties": {
                "image_id": {"type": "long"},
                "user_id": {"type": "long"},          # 사용자별 검색 격리용
                "title": {"type": "text", "analyzer": _KOREAN_ANALYZER},
                "summary": {"type": "text", "analyzer": _KOREAN_ANALYZER},
                # 검색은 형태소 분석된 text 로, 태그 목록 집계는 keyword 서브필드로 한다.
                "tags": {
                    "type": "text",
                    "analyzer": _KOREAN_ANALYZER,
                    "fields": {"keyword": {"type": "keyword"}},
                },
                "category_name": {"type": "keyword"},  # 정확 일치 필터
                "raw_text": {"type": "text", "analyzer": _KOREAN_ANALYZER},
                # 검색용 시맨틱 벡터(로컬 bge-m3). 카테고리용 gemini-768 과 별개 필드다.
                # 자연어/하이브리드 검색(F3)에서 kNN 으로 조회한다. 벡터가 없는 문서
                # (빈 OCR 등)는 kNN 에 안 걸리고 BM25 로만 검색된다.
                "embedding": {
                    "type": "knn_vector",
                    "dimension": settings.search_embedding_dim,
                    "method": {
                        "name": "hnsw",
                        "space_type": "cosinesimil",
                        "engine": "lucene",
                    },
                },
                "created_at": {"type": "date"},
                # 명세 4-1 등록 시점에 채워지는 필드.
                "file_name": {"type": "keyword"},
                "content_hash": {"type": "keyword"},   # 중복 판정 키(SHA-256)
                "file_size": {"type": "long"},
                "ocr_lang": {"type": "keyword", "index": False},
                "ocr_confidence": {"type": "float", "index": False},
                # 명세 6-1 필터·8-1 정렬용 필드.
                "status": {"type": "keyword"},        # 명세 1.4 status enum
                "favorite": {"type": "boolean"},
                "uploaded_at": {"type": "date"},      # 8-1 uploadedAt,desc
                "last_viewed_at": {"type": "date"},   # 8-1 lastViewedAt,desc (6-2 조회 시 갱신)
                # 아래는 화면에 돌려주기만 하고 검색 대상은 아니라 색인하지 않는다.
                "s3_key": {"type": "keyword", "index": False},
                "key_information": {"type": "keyword", "index": False},
                "category_confidence": {"type": "float", "index": False},
                # 융합 호출이 이미지와 대조해 교정한 화면 텍스트(6-2 '추출된 텍스트').
                # 검색은 raw_text 로만 한다 — 교정본을 검색 대상에 넣으면 SEARCH_FIELDS
                # 계약과 점수 분포가 바뀌므로 저장 전용이다. 기존 인덱스에는 이 필드가
                # 없지만 dynamic mapping 이 첫 쓰기에 붙여 주고, 질의하지 않으므로
                # 매핑 종류가 무엇이든 무해하다(재색인 불필요).
                "refined_text": {"type": "text", "index": False},
            }
        },
    }


def _create_index_with_template(index: str, body: dict[str, Any]) -> None:
    """인덱스 템플릿을 먼저 걸고 인덱스를 만든다.

    auto_create_index 경쟁 대비: 우리가 create 하기 직전에 다른 요청의 문서
    쓰기가 인덱스를 먼저 만들어 버려도, 정확히 이 이름만 잡는 템플릿 덕에
    dynamic mapping 대신 올바른 매핑이 적용된다(category_name 이 text 로 잡혀
    집계가 400 나는 사고의 재발 방지). 경쟁에서 지면 create 는
    already_exists 로 터지는데, 이미 원하는 매핑이므로 무시한다.
    """
    client = _client()
    client.indices.put_index_template(
        name=f"{index}-mapping",
        body={"index_patterns": [index], "template": body, "priority": 100},
    )
    try:
        client.indices.create(index=index, body=body)
    except Exception as e:
        if "resource_already_exists" not in str(e):
            raise


def ensure_index(force: bool = False) -> None:
    """인덱스가 없으면 **올바른 매핑으로** 만든다(멱등).

    왜 매번 확인하지 않고, 왜 캐시를 영원히 두지도 않는가:

    OpenSearch 는 `action.auto_create_index` 가 기본 true 라, 없는 인덱스에 문서를
    쓰면 알아서 만들어 준다. 문제는 그때 만들어지는 매핑에는 **nori analyzer 가 없다**는
    것이다. 한글 검색이 조용히 망가지고, 에러는 아무 데도 안 뜬다.

    이 상황은 OpenSearch 컨테이너를 볼륨까지 지우고 다시 올렸는데 FastAPI 는 계속
    떠 있을 때 실제로 생긴다. 캐시를 영구히 두면 ensure_index 가 그냥 통과해 버려서
    다음 색인이 잘못된 매핑을 만든다.

    그래서 캐시에 유효기간을 둔다. exists 는 로컬 HEAD 요청이라 싸고,
    최악의 경우에도 TTL 안에 스스로 복구된다. 생성 시에는 같은 이름의 인덱스
    템플릿도 함께 등록해 auto-create 경쟁까지 막는다(_create_index_with_template).
    """
    global _index_checked_at
    now = time.monotonic()
    if not force and now - _index_checked_at < _INDEX_CHECK_TTL:
        return
    client = _client()
    index = get_settings().opensearch_index
    if not client.indices.exists(index=index):
        _create_index_with_template(index, _index_body())
        logger.warning("OpenSearch 인덱스 생성: %s (nori 매핑 적용)", index)
    else:
        # 존재만 보면 못 잡는 사고가 있다: 인덱스를 밖에서 지운 직후 TTL 안에
        # 색인 요청이 들어오면 OpenSearch 가 **자동 생성**해 버린다. 그러면 이
        # 함수는 "있음"으로 통과하고, embedding 은 knn_vector 가 아닌 float 로,
        # nori 도 없이 굳는다 — 시맨틱 검색이 조용히 죽는다(2026-07-29·07-31 2회 발생).
        # 매핑은 못 고치므로(재색인 필요) 비명이라도 지른다.
        props = (client.indices.get_mapping(index=index)[index]["mappings"]
                 .get("properties", {}))
        if props.get("embedding", {}).get("type") != "knn_vector":
            logger.error(
                "OpenSearch 인덱스 '%s' 매핑 손상: embedding=%s (knn_vector 여야 함). "
                "자동 생성된 인덱스다 — 시맨틱 검색이 동작하지 않는다. "
                "인덱스를 삭제하고 재색인해야 한다.",
                index, props.get("embedding", {}).get("type"),
            )
    _index_checked_at = now


def migrate_image_index_mapping() -> None:
    """auto-create 로 잘못 만들어진 이미지 인덱스를 재색인으로 교정한다(멱등).

    category_name 이 dynamic mapping 으로 text 가 되면 terms 집계가
    400(fielddata) 을 던지고(카테고리 목록 API), nori 가 없어 한글 검색·term
    필터도 조용히 나빠진다. _source 는 남아 있으므로 old→tmp→재생성→되채움
    재색인만으로 복구된다(LLM 재분석 불필요).

    기동 시(lifespan, 요청 받기 전) 부른다 — 단일 워커라 재색인 중 새 쓰기와
    경쟁하지 않는다. 실패해도 기동은 막지 않는다: 원본이 남는 방향으로만
    중단하고, 다음 재기동(=재배포)에서 다시 시도한다.
    """
    try:
        client = _client()
        index = get_settings().opensearch_index
        if not client.indices.exists(index=index):
            return  # ensure_index 가 올바른 매핑으로 새로 만든다
        props = client.indices.get_mapping(index=index)[index]["mappings"].get(
            "properties", {})
        if (props.get("category_name") or {}).get("type") == "keyword":
            return
        tmp = f"{index}_migrate_tmp"
        client.indices.refresh(index=index)
        total = client.count(index=index)["count"]
        logger.warning("이미지 인덱스 매핑 교정 시작: category_name=text, %s건", total)

        if client.indices.exists(index=tmp):
            client.indices.delete(index=tmp)
        client.indices.create(index=tmp, body=_index_body())
        # ponytail: 동기 재색인 — 수만 건까지 기동 지연 수초. 그 이상 커지면
        # wait_for_completion=False + tasks API 폴링으로 바꾼다.
        client.reindex(body={"source": {"index": index}, "dest": {"index": tmp}},
                       wait_for_completion=True, request_timeout=600)
        client.indices.refresh(index=tmp)
        moved = client.count(index=tmp)["count"]
        if moved != total:
            logger.error("교정 중단: tmp 재색인 %s/%s건 — 원본 유지", moved, total)
            return

        client.indices.delete(index=index)
        ensure_index(force=True)  # 템플릿 등록 + 올바른 매핑으로 재생성
        client.reindex(body={"source": {"index": tmp}, "dest": {"index": index}},
                       wait_for_completion=True, request_timeout=600)
        client.indices.refresh(index=index)
        back = client.count(index=index)["count"]
        if back != total:
            logger.error("되채움 %s/%s건 — tmp(%s) 보존", back, total, tmp)
            return
        client.indices.delete(index=tmp)
        logger.warning("이미지 인덱스 매핑 교정 완료: %s건, keyword·nori 적용", total)
    except Exception:
        logger.exception("이미지 인덱스 매핑 교정 실패 — 다음 기동에서 재시도")


def index_document(
    image_id: int,
    user_id: int,
    title: str,
    summary: str,
    tags: list[str],
    category_name: str | None,
    raw_text: str,
    created_at: str,
    s3_key: str | None = None,
    key_information: list[str] | None = None,
    status: str = "COMPLETED",
    category_confidence: float | None = None,
    uploaded_at: str | None = None,
    refined_text: str | None = None,
) -> None:
    """AGENT 결과를 검색 인덱스에 저장한다. image_id 를 문서 id 로 써서 재색인은 덮어쓴다."""
    ensure_index()
    settings = get_settings()
    doc = {
        "image_id": image_id,
        "user_id": user_id,
        "title": title,
        "summary": summary,
        "tags": tags,
        "category_name": category_name,
        "raw_text": raw_text,
        "created_at": created_at,
        "s3_key": s3_key,
        "key_information": key_information or [],
        "status": status,
        "category_confidence": category_confidence,
    }
    if uploaded_at:
        doc["uploaded_at"] = uploaded_at
    if refined_text:
        # 빈 값이면 키를 넣지 않는다 — 재분석이 교정본 없이 다시 색인할 때
        # 이미 있는 교정본을 null 로 덮지 않게 한다(_merge_doc 은 부분 병합).
        doc["refined_text"] = refined_text
    # 검색용 시맨틱 벡터(로컬 bge-m3). best-effort — 실패해도 색인·BM25 는 계속 동작.
    # 빈 OCR 이미지는 title·summary 가 비어 벡터를 만들지 않는다(kNN 대상에서 자연히 제외).
    search_text = " ".join(p for p in (title, summary) if p).strip()
    vector = embedder.embed(search_text)
    if vector:
        doc["embedding"] = vector
    # 4-1 등록 때 만들어 둔 문서(fileName·contentHash·uploadedAt 등)를 덮어쓰지 않도록
    # 통째로 index 하지 않고 부분 병합한다. 문서가 없으면 새로 만든다.
    _merge_doc(image_id, doc)


def _merge_doc(image_id: int, doc: dict[str, Any]) -> None:
    """부분 갱신(없으면 생성). 색인의 다른 필드를 보존한다."""
    _client().update(
        index=get_settings().opensearch_index,
        id=str(image_id),
        body={"doc": doc, "doc_as_upsert": True},
    )


def set_category(image_id: int, category_name: str,
                 category_confidence: float | None = None) -> None:
    """카테고리만 갈아 끼운다(재분석). 제목·요약·태그·OCR 은 건드리지 않는다."""
    _merge_doc(image_id, {"category_name": category_name,
                          "category_confidence": category_confidence})


def create_pending_document(
    image_id: int,
    user_id: int,
    s3_key: str,
    file_name: str,
    content_hash: str,
    file_size: int,
    created_at: str,
    raw_text: str = "",
    ocr_lang: str | None = None,
    ocr_confidence: float | None = None,
) -> None:
    """4-1 등록 시점의 문서를 만든다. 아직 분석 전이라 status 는 QUEUED 다.

    등록 즉시 색인해 두면 업로드·분석 중인 이미지도 6-1 목록에 status 와 함께 나타난다
    (명세 6-1 의 status 필터가 QUEUED·PROCESSING 을 거를 수 있어야 한다).
    """
    ensure_index()
    _merge_doc(image_id, {
        "image_id": image_id,
        "user_id": user_id,
        "s3_key": s3_key,
        "file_name": file_name,
        "content_hash": content_hash,
        "file_size": file_size,
        "raw_text": raw_text,
        "ocr_lang": ocr_lang,
        "ocr_confidence": ocr_confidence,
        "created_at": created_at,
        "status": "QUEUED",
        # 즐겨찾기(6-5)는 아직 AI 범위 밖이라 항상 false. 필드를 미리 두면
        # 6-1 favorite 필터가 계약대로 동작하고, 6-5 가 붙어도 매핑을 안 바꿔도 된다.
        "favorite": False,
        "title": "",
        "summary": "",
        "tags": [],
    })


def mark_uploaded(image_id: int, uploaded_at: str) -> None:
    """4-2 업로드 완료 통지 시각을 기록한다."""
    _merge_doc(image_id, {"uploaded_at": uploaded_at})


def set_status(image_id: int, status: str) -> None:
    _merge_doc(image_id, {"status": status})


def find_by_status(status: str, limit: int = 500) -> list[dict[str, Any]]:
    """해당 status 의 문서를 사용자 구분 없이 모은다. 기동 시 끊긴 작업 복구용.

    사용자별 조회가 아니라 전 사용자를 훑는 유일한 경로다. 재기동 시점에는
    "누가 요청했는가" 가 없고 "무엇이 끊겼는가" 만 있어서 격리 축이 성립하지 않는다.
    요청 경로에서는 쓰지 말 것 — user_id 필터가 없다.

    s3_key 는 index:false 지만 _source 에는 남아 있어 그대로 읽어올 수 있다.
    """
    ensure_index()
    resp = _client().search(
        index=get_settings().opensearch_index,
        body={
            "size": limit,
            "query": {"bool": {"filter": [{"term": {"status": status}}]}},
            "sort": [{"image_id": {"order": "asc"}}],
            "_source": ["image_id", "user_id", "s3_key", "raw_text"],
        },
    )
    return [h["_source"] for h in resp["hits"]["hits"]]


def save_ocr(
    image_id: int, raw_text: str, lang: str | None, confidence: float | None
) -> None:
    """4-3 OCR 제출 결과를 저장한다."""
    _merge_doc(image_id, {
        "raw_text": raw_text, "ocr_lang": lang, "ocr_confidence": confidence,
    })


def refresh_index() -> None:
    """방금 쓴 문서를 즉시 검색 가능하게 만든다.

    OpenSearch 는 기본적으로 1초 주기로만 색인을 갱신한다(near-real-time). 4-1 등록
    직후에 목록을 조회하거나 다음 요청에서 중복 판정을 하면 방금 넣은 문서가 안 잡힌다.
    건당이 아니라 **배치 1회**만 호출하므로 비용은 크지 않다.
    """
    try:
        _client().indices.refresh(index=get_settings().opensearch_index)
    except Exception as e:
        logger.warning("인덱스 refresh 실패: %s", e)


def find_by_content_hash(user_id: int, content_hash: str) -> int | None:
    """같은 사용자가 이미 올린 같은 내용의 이미지를 찾는다(명세 4-1 중복 판정).

    같은 사진을 다시 올려도 Gemini 를 또 돌리지 않게 해준다.

    **업로드가 끝난 것만** 중복으로 본다(uploaded_at 존재). 등록만 되고 업로드에
    실패한 문서까지 중복으로 잡으면 그 사진은 영원히 재시도할 수 없다 —
    4-1 이 duplicated 로 분류해 uploadUrl 을 주지 않기 때문이다.
    """
    ensure_index()
    resp = _client().search(
        index=get_settings().opensearch_index,
        body={
            "size": 1,
            "query": {"bool": {"filter": [
                {"term": {"user_id": user_id}},
                {"term": {"content_hash": content_hash}},
                {"exists": {"field": "uploaded_at"}},
            ]}},
            "_source": ["image_id"],
        },
    )
    hits = resp["hits"]["hits"]
    return hits[0]["_source"]["image_id"] if hits else None


def touch_last_viewed(image_id: int, viewed_at: str) -> None:
    """6-2 상세 조회 성공 시 lastViewedAt 을 갱신한다(명세 8-1 정렬 규칙).

    명세: "lastViewedAt 은 인증 사용자가 6-2 이미지 상세 조회에 성공했을 때 갱신한다.
    검색 결과 노출과 썸네일 조회는 조회 이력으로 기록하지 않는다."
    실패해도 조회 자체를 막지 않는다(정렬 힌트일 뿐이다).
    """
    try:
        _client().update(
            index=get_settings().opensearch_index,
            id=str(image_id),
            body={"doc": {"last_viewed_at": viewed_at}},
        )
    except Exception as e:
        logger.warning("lastViewedAt 갱신 실패 imageId=%s: %s", image_id, e)


# ── 정렬 파싱 (명세 8-1 정렬 규칙) ────────────────────────────────────────
class InvalidSortError(ValueError):
    """지원하지 않는 정렬 필드·방향. 명세 11 의 INVALID_SORT(400) 로 매핑된다."""


# 명세의 camelCase 정렬값 → 색인 필드명
_SORT_FIELDS = {
    "createdAt": "created_at",
    "imageId": "image_id",
    "uploadedAt": "uploaded_at",
    "lastViewedAt": "last_viewed_at",
}

# 최종 동률 해소 기준. 명세: "모든 정렬의 최종 동률 해소 기준은 imageId,desc 로 고정해
# 페이지 이동 중 결과 순서가 흔들리지 않게 한다."
_TIE_BREAKER = {"image_id": {"order": "desc"}}


def parse_sort(sort: str | None, allowed: set[str], default: str) -> list[dict[str, Any]]:
    """`createdAt,desc` 형태를 OpenSearch sort 절로 바꾼다.

    allowed 에 없는 값은 InvalidSortError 를 올린다(명세: INVALID_SORT 400).
    `relevance` 는 BM25 점수 정렬이라 호출 측에서 별도로 처리한다.
    """
    value = (sort or default).strip()
    if value not in allowed:
        raise InvalidSortError(f"지원하지 않는 정렬입니다: {value}")
    if value == "relevance":
        return [{"_score": {"order": "desc"}}, _TIE_BREAKER]

    field_name, _, direction = value.partition(",")
    direction = direction or "desc"
    if direction not in ("asc", "desc") or field_name not in _SORT_FIELDS:
        raise InvalidSortError(f"지원하지 않는 정렬입니다: {value}")

    indexed = _SORT_FIELDS[field_name]
    clause: dict[str, Any] = {"order": direction}
    if indexed in ("last_viewed_at", "uploaded_at"):
        # 명세: "조회 이력이 없는 이미지는 마지막에 배치"
        clause["missing"] = "_last"
    order: list[dict[str, Any]] = [{indexed: clause}]
    if indexed != "image_id":
        order.append(_TIE_BREAKER)
    return order


# 명세 8-1 scope: 검색 대상 필드를 좁힌다.
_SCOPE_FIELDS = {
    "ALL": ["title^2", "summary", "tags", "raw_text"],
    "OCR": ["raw_text"],
    "TAG": ["tags"],
    # 구조화 데이터는 MVP 범위 밖이라 색인된 필드가 없다. 빈 결과가 정상이다.
    "STRUCTURED": [],
}

SEARCH_SORTS = {
    "relevance", "imageId,asc", "lastViewedAt,desc",
    "uploadedAt,desc", "createdAt,desc",
}


def keyword_search(
    user_id: int,
    query: str,
    category: str | None,
    size: int,
    page: int = 0,
    tag: str | None = None,
    scope: str = "ALL",
    sort: str | None = None,
    hybrid: bool | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """검색 진입점. 항상 user_id 로 격리하고, category·tag 가 있으면 추가 필터.

    relevance·scope=ALL 자동 검색(hybrid=None)은 **cascade(F4)** 로 처리한다: BM25 를
    먼저 돌려 결과가 있으면 그대로 쓰고(임베딩 안 함), 빈 결과일 때만 하이브리드(BM25+kNN,
    F3)로 승격한다. "쿠팡" 같은 키워드는 정확 BM25 로, "반려견" 처럼 어휘가 안 겹치는
    시맨틱 질의는 승격돼 의미로 잡힌다 — 질의를 '자연어인지' 분류하지 않고 검색 결과로 판단한다.

    `hybrid=False` 는 강제 BM25(내부 엔드포인트가 정확 total·BM25 점수 스케일을 유지),
    `hybrid=True` 는 (relevance·scope=ALL 조건 하에서) 프로브 없이 바로 하이브리드를 켠다
    — OCR/TAG scope 나 필드 정렬이면 hybrid 값과 무관하게 BM25 로 간다. 하이브리드가 어떤
    이유로든 깨지면 BM25 로 폴백해 검색 자체는 살린다(best-effort).
    """
    ensure_index()
    settings = get_settings()

    fields = _SCOPE_FIELDS.get(scope.upper(), _SCOPE_FIELDS["ALL"])
    if not fields:
        # STRUCTURED: 검색할 필드가 없다. 질의를 날리지 않고 빈 결과를 돌려준다.
        return [], 0

    # 하이브리드는 relevance·scope=ALL 에서만 의미가 있다(임베딩이 title+summary 기반).
    # OCR/TAG scope, 필드 정렬, hybrid=False, 킬스위치 off 는 전부 기존 BM25 를 유지한다.
    sort_value = (sort or "relevance").strip()
    hybrid_eligible = (
        hybrid is not False
        and settings.search_hybrid_enabled
        and scope.upper() == "ALL"
        and sort_value == "relevance"
    )
    if not hybrid_eligible:
        return _bm25_search(
            settings, user_id, query, fields, category, tag, size, page, sort
        )

    # hybrid=True: 프로브 없이 강제 하이브리드.
    if hybrid is True:
        return _run_hybrid_or_bm25(
            settings, user_id, query, fields, category, tag, size, page, sort
        )

    # hybrid=None: cascade(F4). BM25 를 먼저 돌려 결과가 있으면 임베딩 없이 그대로 쓰고,
    # 빈 결과일 때만 하이브리드로 승격한다. '자연어인지' 분류하지 않고 검색 결과로 판단한다.
    # 승격 지연은 BM25 프로브 1회(~수 ms)뿐이고, 지연을 지배하는 임베딩은 어차피 공통이다.
    bm25_hits, bm25_total = _bm25_search(
        settings, user_id, query, fields, category, tag, size, page, sort
    )
    # F4b: "잡았다"가 곧 "맞게 잡았다"는 아니다. 최고점이 문턱 이상일 때만 BM25 로 확정하고,
    # 미만이면(오탐 흔적) 결과가 있어도 하이브리드로 보강한다. 문턱 0.0 = 기존 동작.
    top_score = bm25_hits[0]["score"] if bm25_hits else 0.0
    if bm25_total > 0 and top_score >= settings.search_cascade_min_top_score:
        return bm25_hits, bm25_total   # 키워드로 충분 — 임베딩 생략
    # BM25 빈 결과(또는 약한 결과) → 시맨틱으로 승격. 승격 불가 시 BM25 결과를 그대로 쓴다.
    return _run_hybrid_or_bm25(
        settings, user_id, query, fields, category, tag, size, page, sort,
        bm25_fallback=(bm25_hits, bm25_total),
    )


def _run_hybrid_or_bm25(
    settings: Any,
    user_id: int,
    query: str,
    fields: list[str],
    category: str | None,
    tag: str | None,
    size: int,
    page: int,
    sort: str | None,
    bm25_fallback: tuple[list[dict[str, Any]], int] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """질의를 임베딩해 하이브리드를 시도하고, 임베딩 불가·하이브리드 실패 시 BM25 로 폴백한다.

    `bm25_fallback` 이 주어지면(cascade 가 이미 BM25 를 돌려본 경우) 폴백에서 BM25 를
    다시 돌리지 않고 그 결과를 그대로 쓴다. hybrid=True(프로브 없음)일 때는 None 이라
    폴백 시 BM25 를 새로 돌린다.
    """
    qvec = embedder.embed(query)
    if qvec is not None:
        try:
            return _hybrid_search(
                settings, user_id, query, qvec, category, tag, size, page
            )
        except Exception:
            logger.exception("하이브리드 검색 실패 → BM25 폴백")
    if bm25_fallback is not None:
        return bm25_fallback
    return _bm25_search(
        settings, user_id, query, fields, category, tag, size, page, sort
    )


def _search_filters(
    user_id: int, category: str | None, tag: str | None
) -> list[dict[str, Any]]:
    """사용자 격리(+선택 필터). BM25·kNN 두 가지가 똑같이 써서 격리 동치성을 보장한다."""
    filters: list[dict[str, Any]] = [{"term": {"user_id": user_id}}]
    if category:
        filters.append({"term": {"category_name": category}})
    if tag:
        filters.append({"term": {"tags.keyword": tag}})
    return filters


def _hit_dict(source: dict[str, Any], score: float) -> dict[str, Any]:
    """검색 hit 의 공통 형태. BM25·하이브리드 두 경로가 같은 키집합을 돌려줘야
    호출부(SearchHit·SearchResultItem)의 계약이 깨지지 않는다."""
    return {
        "image_id": source.get("image_id"),
        "title": source.get("title", ""),
        "summary": source.get("summary", ""),
        "tags": source.get("tags", []),
        "category": source.get("category_name"),
        "s3_key": source.get("s3_key"),
        "created_at": source.get("created_at"),
        "uploaded_at": source.get("uploaded_at"),
        "last_viewed_at": source.get("last_viewed_at"),
        "score": score,
    }


def _bm25_search(
    settings: Any,
    user_id: int,
    query: str,
    fields: list[str],
    category: str | None,
    tag: str | None,
    size: int,
    page: int,
    sort: str | None,
) -> tuple[list[dict[str, Any]], int]:
    """기존 BM25 경로(F1). 하이브리드 조건이 아니거나 임베딩이 없을 때 쓴다.

    서버측 from/size 페이징이라 total 이 정확하고, _score 스케일도 그대로다.
    scope=OCR/TAG, 필드 정렬, 내부 엔드포인트, 모델 미로드가 모두 여기로 온다.
    """
    bool_query: dict[str, Any] = {
        "must": [{
            "multi_match": {
                "query": query,
                "fields": fields,
                # 기본 OR 는 토큰 하나만 걸려도 매칭돼 "원"·"만" 같은 흔한 형태소로
                # 과다매칭이 난다. 질의 토큰의 일정 비율 이상을 요구해 이를 막는다.
                "minimum_should_match": settings.search_min_should_match,
            },
        }],
        "filter": _search_filters(user_id, category, tag),
    }

    resp = _client().search(
        index=settings.opensearch_index,
        body={
            "from": max(0, page) * size,
            "size": size,
            "query": {"bool": bool_query},
            "sort": parse_sort(sort, SEARCH_SORTS, "relevance"),
        },
    )

    # relevance 가 아닌 정렬에서는 _score 가 null 로 온다.
    hits = [
        _hit_dict(h["_source"], h.get("_score") or 0.0)
        for h in resp["hits"]["hits"]
    ]
    return hits, resp["hits"]["total"]["value"]


# 하이브리드 후보풀에서 돌려받을 _source 필드. embedding 을 명시적으로 포함해,
# 나중에 매핑에 _source excludes 가 생겨도 클라이언트측 코사인 재계산이 견고하게 한다.
_POOL_SOURCE = [
    "image_id", "title", "summary", "tags", "category_name", "s3_key",
    "created_at", "uploaded_at", "last_viewed_at", "embedding",
]


def _bm25_pool_body(
    settings: Any, query: str, filters: list[dict[str, Any]], n: int
) -> dict[str, Any]:
    """하이브리드용 BM25 후보풀. 요청 size 가 아니라 고정 풀 크기 N 을 받아
    page 와 무관하게 같은 후보집합을 만든다(페이지 간 total 안정)."""
    return {
        "from": 0,
        "size": n,
        "query": {"bool": {
            "must": [{
                "multi_match": {
                    "query": query,
                    "fields": _SCOPE_FIELDS["ALL"],
                    "minimum_should_match": settings.search_min_should_match,
                },
            }],
            "filter": filters,
        }},
        # 동점 문서의 rank 를 결정적으로 만들기 위한 2차 정렬.
        "sort": [{"_score": {"order": "desc"}}, {"image_id": {"order": "desc"}}],
        "_source": {"includes": _POOL_SOURCE},
    }


def _knn_body(
    qvec: list[float], filters: list[dict[str, Any]], n: int
) -> dict[str, Any]:
    """lucene kNN 후보풀. filter 를 knn 필드객체 안(vector·k 와 형제)에 두어
    OpenSearch 2.4+ efficient pre-filtering 으로 사용자 격리를 건다. 미지원 빌드면
    unknown field 에러가 나(조용한 무시가 아님) 상위에서 BM25-only 로 degrade 한다."""
    return {
        "size": n,
        "query": {"knn": {"embedding": {
            "vector": qvec,
            "k": n,
            "filter": {"bool": {"filter": filters}},
        }}},
        "_source": {"includes": _POOL_SOURCE},
    }


def _hybrid_search(
    settings: Any,
    user_id: int,
    query: str,
    qvec: list[float],
    category: str | None,
    tag: str | None,
    size: int,
    page: int,
) -> tuple[list[dict[str, Any]], int]:
    """BM25 + lucene kNN 을 msearch 1왕복으로 받아, 융합 전에 raw 신호로 임계값 컷을
    적용하고 RRF 로 순위결합한다(F3).

    - 컷: 임베딩이 있는 후보는 재계산 코사인(단위벡터 내적)이 임계값 이상이어야 살아남는다.
      어휘로만 걸린 무관 문서를 걸러 "억지 매칭"을 막는다. 살아남는 후보가 없으면 결과 없음.
      임베딩 없는 레거시 문서는 BM25 최소 점수로만 거른다(기본 off → 하위호환).
    - 폴백: kNN 만 실패하면 이미 받은 BM25 결과로 BM25-only 하이브리드를 계속한다(재실행 없음).
    - total: 융합 생존자 수. 후보풀 N 이 page 무관 고정이라 페이지 간 total 이 안정적이다.
    """
    n = max(settings.search_hybrid_pool_size, size)
    filters = _search_filters(user_id, category, tag)
    body = [
        {"index": settings.opensearch_index}, _bm25_pool_body(settings, query, filters, n),
        {"index": settings.opensearch_index}, _knn_body(qvec, filters, n),
    ]
    responses = _client().msearch(body=body)["responses"]

    # ── msearch 부분 실패는 예외가 아니라 responses[i].error 로 온다. 명시 검사한다. ──
    bm25_resp = responses[0]
    if bm25_resp.get("error"):
        # BM25 자체가 실패하면 하이브리드를 포기하고 상위에서 레거시 BM25 로 폴백한다.
        raise SearchError(f"BM25 후보풀 조회 실패: {bm25_resp['error']}")
    bm25_hits = bm25_resp["hits"]["hits"]

    knn_resp = responses[1]
    if knn_resp.get("error") or "hits" not in knn_resp:
        # kNN 만 실패(구버전 lucene filter 미지원 등): 재실행 없이 BM25-only 로 진행한다.
        logger.warning(
            "kNN 후보풀 조회 실패 → BM25-only 하이브리드: %s", knn_resp.get("error")
        )
        knn_hits = []
    else:
        knn_hits = knn_resp["hits"]["hits"]

    # ── union (image_id 로 dedup). 각 가지의 raw 점수를 문서에 기록한다. ──
    docs: dict[int, dict[str, Any]] = {}
    bm25_ids: list[int] = []
    knn_ids: list[int] = []
    for h in bm25_hits:
        src = h["_source"]
        iid = src.get("image_id")
        if iid is None:
            continue
        bm25_ids.append(iid)
        docs.setdefault(iid, {"src": src})["bm25"] = h.get("_score") or 0.0
    for h in knn_hits:
        src = h["_source"]
        iid = src.get("image_id")
        if iid is None:
            continue
        knn_ids.append(iid)
        docs.setdefault(iid, {"src": src})
    bm25_set = set(bm25_ids)
    knn_set = set(knn_ids)

    # ── 임계값 컷(융합 前, raw 신호). bge-m3 는 단위벡터라 cos = dot(qvec, emb). ──
    qn = len(qvec)
    survivors: list[int] = []
    for iid, d in docs.items():
        emb = d["src"].get("embedding")
        if emb and len(emb) == qn:
            cos = sum(a * b for a, b in zip(qvec, emb))
            d["cos"] = cos
            if cos >= settings.search_knn_min_cosine:
                survivors.append(iid)
        else:
            # 임베딩 없는 레거시 문서: 시맨틱 게이트를 면제하고 어휘 점수로만 거른다.
            d["cos"] = None
            if d.get("bm25", 0.0) >= settings.search_bm25_min_score:
                survivors.append(iid)
    if not survivors:
        return [], 0  # 억지 매칭 차단 → 결과 없음

    # ── 생존자 내에서 각 가지 rank 를 결정적으로 매긴다(점수 desc, image_id desc). ──
    bm25_rank = {
        iid: r for r, iid in enumerate(sorted(
            [i for i in survivors if i in bm25_set],
            key=lambda i: (-docs[i]["bm25"], -i),
        ))
    }
    knn_rank = {
        iid: r for r, iid in enumerate(sorted(
            [i for i in survivors if i in knn_set and docs[i].get("cos") is not None],
            key=lambda i: (-docs[i]["cos"], -i),
        ))
    }

    # ── RRF 순위결합. 두 리스트 rank 의 1/(k+rank+1) 합. ──
    k = settings.search_rrf_k
    fused = sorted(
        (
            (
                iid,
                (1.0 / (k + bm25_rank[iid] + 1) if iid in bm25_rank else 0.0)
                + (1.0 / (k + knn_rank[iid] + 1) if iid in knn_rank else 0.0),
            )
            for iid in survivors
        ),
        key=lambda t: (-t[1], -t[0]),  # rrf desc, image_id desc
    )

    total = len(fused)  # page 무관 고정풀 위의 생존자 수 → 페이지 간 안정
    start = max(0, page) * size
    window = fused[start:start + size]  # 순수 클라이언트 슬라이싱
    hits = [_hit_dict(docs[iid]["src"], round(score, 6)) for iid, score in window]
    return hits, total


def _to_image(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "image_id": source.get("image_id"),
        "title": source.get("title", ""),
        "summary": source.get("summary", ""),
        "tags": source.get("tags", []),
        "category": source.get("category_name"),
        "s3_key": source.get("s3_key"),
        "created_at": source.get("created_at"),
        # 이 필드들이 없는 문서는 이번 매핑 변경 전에 색인된 것이다.
        "status": source.get("status", "COMPLETED"),
        "favorite": source.get("favorite", False),
        "uploaded_at": source.get("uploaded_at"),
        "last_viewed_at": source.get("last_viewed_at"),
        "category_confidence": source.get("category_confidence"),
        "file_name": source.get("file_name"),
        "content_hash": source.get("content_hash"),
    }


LIST_SORTS = {
    "createdAt,desc", "createdAt,asc", "imageId,asc", "imageId,desc",
    "uploadedAt,desc", "lastViewedAt,desc",
}


def list_images(
    user_id: int,
    page: int,
    size: int,
    category: str | None = None,
    tag: str | None = None,
    statuses: list[str] | None = None,
    favorite: bool | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """홈 화면용 목록(명세 6-1). 기본 정렬은 createdAt,desc."""
    ensure_index()
    settings = get_settings()

    filters: list[dict[str, Any]] = [{"term": {"user_id": user_id}}]
    if category:
        filters.append({"term": {"category_name": category}})
    if tag:
        # 집계·필터는 분석되지 않은 keyword 서브필드를 쓴다.
        filters.append({"term": {"tags.keyword": tag}})
    if statuses:
        filters.append({"terms": {"status": statuses}})
    if favorite is not None:
        filters.append({"term": {"favorite": favorite}})
    if date_from or date_to:
        rng: dict[str, str] = {}
        if date_from:
            rng["gte"] = date_from
        if date_to:
            rng["lte"] = date_to
        filters.append({"range": {"created_at": rng}})

    resp = _client().search(
        index=settings.opensearch_index,
        body={
            "from": max(0, page) * size,
            "size": size,
            "query": {"bool": {"filter": filters}},
            "sort": parse_sort(sort, LIST_SORTS, "createdAt,desc"),
        },
    )
    images = [_to_image(h["_source"]) for h in resp["hits"]["hits"]]
    return images, resp["hits"]["total"]["value"]


def get_image(image_id: int) -> dict[str, Any] | None:
    """이미지 상세. 문서 id 가 image_id 라 바로 조회한다."""
    ensure_index()
    settings = get_settings()
    try:
        resp = _client().get(index=settings.opensearch_index, id=str(image_id))
    except Exception:
        return None
    if not resp.get("found"):
        return None
    source = resp["_source"]
    detail = _to_image(source)
    detail["user_id"] = source.get("user_id")
    detail["key_information"] = source.get("key_information", [])
    detail["raw_text"] = source.get("raw_text", "")
    detail["refined_text"] = source.get("refined_text")
    detail["ocr_lang"] = source.get("ocr_lang")
    detail["ocr_confidence"] = source.get("ocr_confidence")
    detail["file_size"] = source.get("file_size")
    return detail


def _aggregate(user_id: int, field: str, limit: int) -> list[dict[str, Any]]:
    """사용자 문서를 대상으로 term 집계를 돌려 (이름, 개수) 목록을 만든다."""
    ensure_index()
    settings = get_settings()
    resp = _client().search(
        index=settings.opensearch_index,
        body={
            "size": 0,
            "query": {"bool": {"filter": [{"term": {"user_id": user_id}}]}},
            "aggs": {"grouped": {"terms": {"field": field, "size": limit}}},
        },
    )
    buckets = resp.get("aggregations", {}).get("grouped", {}).get("buckets", [])
    return [{"name": b["key"], "count": b["doc_count"]} for b in buckets]


def aggregate_categories(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """카테고리 목록. 카드 UI 에 필요한 썸네일·대표 태그까지 한 번에 모아 준다.

    카테고리별로 최신 이미지 1장(top_hits)과 상위 태그(terms)를 하위 집계로 붙인다.
    정렬(이름순·최신순·개수순)은 앱이 이 데이터로 직접 처리할 수 있게 필드를 다 담는다.
    """
    ensure_index()
    settings = get_settings()
    resp = _client().search(
        index=settings.opensearch_index,
        body={
            "size": 0,
            "query": {"bool": {"filter": [{"term": {"user_id": user_id}}]}},
            "aggs": {
                "grouped": {
                    "terms": {"field": "category_name", "size": limit},
                    "aggs": {
                        "recent": {
                            "top_hits": {
                                "size": 1,
                                "sort": [{"created_at": {"order": "desc"}}],
                                "_source": {
                                    "includes": ["image_id", "s3_key", "created_at"]
                                },
                            }
                        },
                        # 카테고리명과 겹치는 태그를 걸러내고 4개를 채우기 위해 넉넉히 받는다.
                        "top_tags": {"terms": {"field": "tags.keyword", "size": 8}},
                    },
                }
            },
        },
    )

    categories: list[dict[str, Any]] = []
    for bucket in resp.get("aggregations", {}).get("grouped", {}).get("buckets", []):
        name = bucket["key"]
        hits = bucket.get("recent", {}).get("hits", {}).get("hits", [])
        recent = hits[0]["_source"] if hits else {}

        tags = [
            {"name": t["key"], "count": t["doc_count"]}
            for t in bucket.get("top_tags", {}).get("buckets", [])
            # 카테고리명과 같은 태그는 카드에서 중복이라 뺀다.
            if t["key"] != name
        ][:4]

        categories.append({
            "name": name,
            "count": bucket["doc_count"],
            "tags": tags,
            "thumbnail_image_id": recent.get("image_id"),
            "thumbnail_s3_key": recent.get("s3_key"),
            "last_updated_at": recent.get("created_at"),
        })
    return categories


def aggregate_tags(user_id: int, limit: int = 30) -> list[dict[str, Any]]:
    return _aggregate(user_id, "tags.keyword", limit)


# ── 카테고리·태그 ID ──────────────────────────────────────────────────────
# 이 서비스는 이름만 색인하고 별도 ID 테이블을 두지 않는다. 앱 명세가 숫자 ID 를
# 요구하므로 이름에서 결정적으로 파생시킨다. 해시라서 서버를 재시작해도 값이
# 유지되고, 별도 저장소가 필요 없다. Spring 복귀 시에는 DB 의 실제 ID 로 바뀐다.


def max_image_id() -> int:
    """색인된 문서 중 가장 큰 image_id. 문서가 없으면 0.

    image_id 는 곧 문서 id 라, 서버가 재시작한 뒤에도 이 값 다음부터 채번해야
    기존 문서를 덮어쓰지 않는다.
    """
    ensure_index()
    settings = get_settings()
    resp = _client().search(
        index=settings.opensearch_index,
        body={"size": 0, "aggs": {"max_id": {"max": {"field": "image_id"}}}},
    )
    value = resp.get("aggregations", {}).get("max_id", {}).get("value")
    return int(value) if value else 0


def stable_id(name: str) -> int:
    """이름 → 31비트 양의 정수 ID. 같은 이름이면 항상 같은 값이 나온다.

    정규화한 이름을 해싱한다. 카테고리의 동일성 판정은 이미 normalize_name 기준이라
    ("쇼핑" 과 "쇼핑 " 은 같은 centroid 문서 `{userId}:{정규화이름}` 에 누적된다)
    원문을 해싱하면 같은 카테고리가 두 개의 id 를 갖는다. Spring 은 이 id 로
    카테고리를 식별하므로 저장소의 동일성 기준과 어긋나면 안 된다.
    """
    return zlib.crc32(normalize_name(name).encode("utf-8")) & 0x7FFFFFFF


def to_tag_refs(names: list[str]) -> list[dict[str, Any]]:
    return [{"tag_id": stable_id(n), "name": n} for n in names or []]


def resolve_name_by_id(user_id: int, field: str, target_id: int) -> str | None:
    """ID 로 필터가 들어왔을 때, 집계 결과를 훑어 원래 이름을 되찾는다.

    해시는 되돌릴 수 없으므로 후보 이름들을 모아 같은 해시를 찾는 방식이다.
    카테고리·태그 개수가 많지 않아 비용은 크지 않다.
    """
    for item in _aggregate(user_id, field, 500):
        if stable_id(item["name"]) == target_id:
            return item["name"]
    return None
