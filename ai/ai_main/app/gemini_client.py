"""Gemini 호출 래퍼.

SDK 의존성을 이 파일에만 가둔다. google-generativeai(지원 종료 예고)에서
google-genai 로 이전했다(2026-08-03). 이전 이유는 폐기 대비만이 아니라
**thinking 제어**다 — thinking 토큰은 출력 단가로 과금되는데(2.5-flash 기본
ON, 3.5-flash 기본 medium) 구 SDK 는 thinkingConfig 를 넘길 수 없어서
분석 1건마다 보이지 않는 출력 토큰을 내고 있었다. 신 SDK 는 REST(httpx)
기반이라 경로 접두사가 있는 GMS 프록시 문제(gRPC 불가)도 자연 해소된다.
"""

import io
import json
import logging
import random
import time
from typing import Any, Callable

from .config import get_settings

logger = logging.getLogger(__name__)


class GeminiError(RuntimeError):
    pass


def _is_rate_limit(exc: Exception) -> bool:
    """429(rate limit / quota) 성격의 예외인지 판별한다.

    SDK·전송 계층에 따라 예외 타입이 제각각이라, 상태코드·클래스명·메시지를
    폭넓게 본다. (google.genai.errors.ClientError 는 .code 에 HTTP 상태를 담는다)
    """
    if getattr(exc, "status_code", None) == 429 or getattr(exc, "code", None) == 429:
        return True
    if type(exc).__name__ in ("ResourceExhausted", "TooManyRequests"):
        return True
    msg = str(exc).lower()
    return any(
        token in msg
        for token in ("429", "resource_exhausted", "rate limit", "rate-limit", "quota exceeded")
    )


def _call_with_retry(label: str, fn: Callable[[], Any]) -> Any:
    """Gemini 호출을 429에 한해 지수 백오프로 재시도한다.

    429가 아닌 예외(잘못된 요청·인증 오류 등)는 재시도 없이 즉시 올린다.
    동기 함수라 asyncio.to_thread 안에서 그대로 호출된다.
    """
    settings = get_settings()
    attempts = max(1, settings.gemini_max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt >= attempts or not _is_rate_limit(exc):
                raise
            delay = min(
                settings.gemini_backoff_base * (2 ** (attempt - 1)),
                settings.gemini_backoff_max,
            )
            delay += random.uniform(0, settings.gemini_backoff_base)  # 지터로 동시 재시도 분산
            logger.warning(
                "Gemini rate limit(%s) 재시도 %s/%s, %.1fs 대기: %s",
                label, attempt, attempts, delay, str(exc)[:200],
            )
            time.sleep(delay)


def _sdk():
    """google-genai 모듈을 지연 import 한다. MOCK_AI 경로는 SDK 없이도 돌아야 한다."""
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:  # pragma: no cover
        raise GeminiError("google-genai 가 설치되지 않았습니다.") from e
    return genai, types


# Client 는 내부에 httpx 커넥션 풀을 들고 있어 호출마다 만들면 낭비다.
# 설정은 기동 시 고정(get_settings lru_cache)이라 싱글턴으로 충분하다.
_client_instance: Any = None


def _client():
    global _client_instance
    if _client_instance is None:
        genai, types = _sdk()
        settings = get_settings()
        _client_instance = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(
                # GMS 프록시 경유 — SDK 가 이 값 뒤에 /v1beta/... 를 붙인다.
                base_url=settings.gemini_base_url,
                # ⚠️ 신 SDK 의 timeout 은 **밀리초**다(구 SDK 는 초).
                # 무제한이면 응답 없는 요청 하나가 asyncio.to_thread 스레드를
                # 영구 점유하고, max_concurrent_stages(4)만큼 쌓이면 파이프라인이 멈춘다.
                timeout=int(settings.gemini_timeout * 1000),
            ),
        )
    return _client_instance


def _thinking_config(model_name: str, types: Any) -> Any:
    """모델 세대에 맞는 thinking 설정을 만든다. None 이면 미전송(모델 기본값).

    thinking 토큰은 출력 단가로 과금된다 — 이 함수가 비용 절감의 본체다.
    세대별 파라미터가 다르다: 2.5 는 thinking_budget(정수, 0=끔), 3.x 는
    thinking_level(minimal/low/medium/high). 한 요청에 둘을 같이 보내면 400
    이므로 모델명을 보고 하나만 고른다.
    """
    settings = get_settings()
    if model_name.startswith("gemini-3"):
        if settings.gemini_thinking_level:
            return types.ThinkingConfig(thinking_level=settings.gemini_thinking_level)
        return None
    if settings.gemini_thinking_budget >= 0:
        return types.ThinkingConfig(thinking_budget=settings.gemini_thinking_budget)
    return None


def _log_usage(model_name: str, response: Any) -> None:
    """토큰 사용량 계측. '예상보다 많이 먹는다'를 감이 아니라 숫자로 확인하기 위한 로그.

    thoughts 가 thinking 토큰이며 **출력 단가로 과금**된다 — thinking 제어가
    실제로 먹혔는지는 이 값이 0/None 인지로 판정한다.

    cached 는 implicit caching 이 잡아 준 프리픽스 토큰이며 **입력 단가의 25%**만
    청구된다. 이 값이 프롬프트 재배치(config.prompt_static_prefix)의 유일한
    판정 근거다:
      - 항상 None → GMS 프록시가 cachedContentTokenCount 를 안 흘려주거나
        implicit caching 을 안 태운다. 재배치해도 이득이 없다.
      - 0 또는 1,024 미만에서 흔들림 → 가변 데이터가 프리픽스를 끊고 있다
        (2.5 계열 최소 1,024토큰 미달이면 캐시가 아예 안 붙는다). 재배치 이득이 크다.
      - prompt 의 대부분을 차지하며 안정적 → 이미 먹고 있다. 남은 이득은 작다.

    계측 실패가 본 호출을 죽여서는 안 되므로 전부 best-effort 다.
    """
    try:
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return
        prompt_tokens = getattr(usage, "prompt_token_count", None)
        cached = getattr(usage, "cached_content_token_count", None)
        # 비율까지 찍어 둔다 — 로그를 눈으로 훑을 때 "붙었나 안 붙었나"를
        # 두 숫자 나눠 보지 않고 바로 읽기 위해서다.
        ratio = (f" ({cached / prompt_tokens:.0%})"
                 if cached and prompt_tokens else "")
        logger.info(
            "Gemini usage model=%s prompt=%s cached=%s%s thoughts=%s candidates=%s total=%s",
            model_name,
            prompt_tokens,
            cached,
            ratio,
            getattr(usage, "thoughts_token_count", None),
            getattr(usage, "candidates_token_count", None),
            getattr(usage, "total_token_count", None),
        )
    except Exception:  # noqa: BLE001 — 계측은 부가 기능
        pass


def parse_json_response(text: str) -> dict[str, Any]:
    """모델 응답에서 JSON 을 추출한다. 코드펜스·잡문이 섞여도 복구를 시도한다.

    generate_json 이 response_mime_type=application/json 을 요구하므로 보통은
    바로 json.loads 로 끝나지만, 프록시 경유 변형·모델 일탈 대비 방어선으로 유지한다.
    """
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        cleaned = parts[1] if len(parts) > 1 else cleaned
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise GeminiError(f"모델 응답을 JSON 으로 해석하지 못했습니다: {cleaned[:200]!r}")


def _mock_json(parts: list[Any]) -> dict[str, Any]:
    """MOCK_AI=true 일 때 쓰는 가짜 응답.

    프롬프트가 요구하는 JSON 형태를 보고 어느 호출인지 구분한다. 각 프롬프트가
    출력 스키마를 문자열로 박아 두고 있어(`'{"informative": ...}'`) 그 키를 찾으면 된다.
    """
    prompt = " ".join(p for p in parts if isinstance(p, str))
    if '"informative"' in prompt:
        return {"informative": True, "confidence": 0.9, "reason": "MOCK 정보성 판정"}
    if '"detected_texts"' in prompt:
        return {
            "description": "MOCK 이미지 분석 결과입니다.",
            "detected_texts": ["MOCK 텍스트", "32,000원", "2026-07-28"],
            "objects": ["텍스트", "버튼"],
        }
    if '"key_information"' in prompt:
        return {
            "title": "MOCK 제목",
            "summary": "MOCK 요약입니다.",
            "tags": ["mock", "테스트"],
            "categories": ["쇼핑"],
            "key_information": ["항목: MOCK 값"],
            "analysis_confidence": 0.9,
        }
    if '"category"' in prompt:  # 카테고리 재분석(reanalyze._pick_category)
        return {"category": "게임", "reason": "MOCK 재분석 판정"}
    if '"price_min"' in prompt:
        return {"keywords": ["mock"], "price_min": None, "price_max": None,
                "brand": None, "category_hints": [], "date_from": None,
                "date_to": None, "expires_before": None, "confidence": 0.9}
    return {}


def _mock_vector(text: str, dim: int) -> list[float]:
    """텍스트 해시로 만드는 결정적 단위 벡터. 같은 입력이면 항상 같은 값이 나온다."""
    import hashlib
    import math

    digest = hashlib.sha256(text.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "big")
    # 선형 합동 생성기. random 모듈의 전역 상태를 건드리지 않으려고 직접 돌린다.
    values: list[float] = []
    state = seed or 1
    for _ in range(dim):
        state = (state * 6364136223846793005 + 1442695040888963407) % (2**64)
        values.append((state / 2**64) * 2.0 - 1.0)
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def generate_json(model_name: str, parts: list[Any]) -> dict[str, Any]:
    """텍스트(또는 텍스트+이미지) 프롬프트를 보내고 JSON 응답을 받는다."""
    if get_settings().mock_ai:
        logger.info("MOCK_AI — generate_json 가짜 응답 (model=%s)", model_name)
        return _mock_json(parts)
    _, types = _sdk()
    config = types.GenerateContentConfig(
        # JSON 모드 — 코드펜스·설명문이 사라져 파싱 실패와 잡토큰이 준다.
        response_mime_type="application/json",
        thinking_config=_thinking_config(model_name, types),
    )
    client = _client()
    response = _call_with_retry(
        f"generate_content:{model_name}",
        lambda: client.models.generate_content(
            model=model_name, contents=parts, config=config,
        ),
    )
    _log_usage(model_name, response)
    return parse_json_response(response.text)


# GMS 프록시는 요청 본문이 약 90KB 를 넘으면 본문이 유실된 채 업스트림에 전달돼
# 400("contents is not specified")이 난다 — 2026-07-31 실측: 67KB 통과, 90KB+ 실패.
# 실서버 스크린샷은 원본이 수 MB 라 비전 호출이 전부 이 400 으로 죽고, 파이프라인은
# "OCR 만으로 진행" 폴백 덕에 조용히 눈 없이 돌아가고 있었다(실사진 111장 중 98장 실측).
# base64 팽창(4/3)과 텍스트 몫을 감안해 이미지 자체를 ~40KB 이하로 맞춘다.
# 융합 호출은 본문을 이미지와 OCR 텍스트가 나눠 쓰는데, 한글은 JSON 이스케이프로
# 글자당 6바이트라 텍스트 밀집 스크린샷은 OCR 만 수십 KB 다(60KB 예산에서 4장
# 400 실측 → 40KB 로 하향, 텍스트 쪽은 stages 가 3,000자로 제한). 분류용 시각
# 신호(레이아웃·색·로고·서식)는 이 해상도로 충분하고, 글자 정보는 기기 OCR 이
# 원본 해상도에서 따로 들어온다.
_IMAGE_BYTE_BUDGET = 40_000


def image_part(image_bytes: bytes):
    """S3 에서 받은 바이트를 비전 입력으로 변환한다. GMS 본문 한도 안으로 축소한다."""
    try:
        from PIL import Image, ImageOps
    except ImportError as e:  # pragma: no cover
        raise GeminiError("pillow 가 설치되지 않았습니다.") from e
    im = Image.open(io.BytesIO(image_bytes))
    if len(image_bytes) <= _IMAGE_BYTE_BUDGET:
        # 이미 한도 안 — 원본 바이트를 그대로 보낸다(재인코딩 팽창 방지).
        mime = Image.MIME.get(im.format or "", "image/png")
        return _to_part(image_bytes, mime)
    im = ImageOps.exif_transpose(im).convert("RGB")
    # 너비 기준 축소는 세로로 긴 캡처(대화 전체 저장·전체 페이지)에서 실패한다 —
    # 360px 폭이어도 높이가 수천 px 면 수백 KB 다(실측: 카톡 장문 캡처 181KB 통과
    # → GMS 400). 품질을 먼저 낮추고, 그래도 크면 면적을 절반씩 줄여 **예산 이하를
    # 보장**한다. 극단적으로 긴 이미지는 흐릿해지지만, 그런 화면은 어차피 한 뷰로
    # 읽을 수 없고 텍스트는 기기 OCR 이 원문으로 따로 들어온다.
    quality = 80
    while True:
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality)
        if buf.tell() <= _IMAGE_BYTE_BUDGET:
            break
        if quality > 50:
            quality -= 15
        elif im.width * im.height > 40_000:
            im = im.resize((max(1, int(im.width * 0.7)),
                            max(1, int(im.height * 0.7))))
        else:
            break  # 4만 픽셀 미만 q50 이 예산을 넘는 경우는 없지만, 무한루프 안전핀
    return _to_part(buf.getvalue(), "image/jpeg")


def _to_part(data: bytes, mime: str):
    """바이트를 SDK Part 로 감싼다. MOCK 모드는 SDK 없이 돌아야 하므로 dict 로 대신한다
    (_mock_json 은 문자열 파트만 보므로 내용은 쓰이지 않는다)."""
    if get_settings().mock_ai:
        return {"mime_type": mime, "data": data}
    _, types = _sdk()
    return types.Part.from_bytes(data=data, mime_type=mime)


# batchEmbedContents 의 요청당 상한. 구 SDK 는 초과분을 알아서 쪼갰지만
# 신 SDK 는 그대로 보내므로 여기서 나눈다.
_EMBED_BATCH_LIMIT = 100


def embed(texts: list[str], purpose: str = "DOCUMENT") -> tuple[str, list[list[float]]]:
    """텍스트 배치를 임베딩한다. (모델명, 벡터 목록)을 돌려준다."""
    settings = get_settings()
    if settings.mock_ai:
        logger.info("MOCK_AI — embed 가짜 벡터 %s건 (dim=%s)",
                    len(texts), settings.embedding_dim)
        return settings.embedding_model_name, [
            _mock_vector(t, settings.embedding_dim) for t in texts
        ]
    if not texts:
        return settings.embedding_model_name, []
    _, types = _sdk()
    client = _client()
    task_type = "RETRIEVAL_QUERY" if purpose == "QUERY" else "RETRIEVAL_DOCUMENT"
    config = types.EmbedContentConfig(
        task_type=task_type,
        # 차원을 명시해 모델 기본값(3072)이 아닌 합의된 값으로 받는다.
        output_dimensionality=settings.embedding_dim,
    )
    vectors: list[list[float]] = []
    for start in range(0, len(texts), _EMBED_BATCH_LIMIT):
        chunk = texts[start:start + _EMBED_BATCH_LIMIT]
        response = _call_with_retry(
            "embed_content",
            lambda c=chunk: client.models.embed_content(
                model=settings.embedding_model_name, contents=c, config=config,
            ),
        )
        got = [list(e.values) for e in response.embeddings]
        if len(got) != len(chunk):
            # GMS 프록시는 batchEmbedContents 를 지원하지 않아 몇 개를 보내든
            # 첫 항목의 임베딩 1개만 돌려준다(2026-08-03 실측). 구글 직결에서는
            # 배치가 그대로 동작하므로, 개수가 어긋난 경우에만 단건으로 나눠 다시 받는다.
            logger.warning("배치 임베딩 응답 %s/%s — 단건 호출로 폴백", len(got), len(chunk))
            got = []
            for text in chunk:
                response = _call_with_retry(
                    "embed_content",
                    lambda t=text: client.models.embed_content(
                        model=settings.embedding_model_name, contents=t, config=config,
                    ),
                )
                got.extend(list(e.values) for e in response.embeddings)
        vectors.extend(got)
    # 호출자는 입력 순서와 벡터 순서가 같다고 보고 zip 한다
    # (stages.seed_default_category_vectors, 10-2 /internal/v1/embed 의 index).
    # 개수가 어긋나면 이름과 벡터가 밀려 붙으므로 여기서 끊는다.
    if len(vectors) != len(texts):
        raise GeminiError(
            f"임베딩 개수가 입력과 다릅니다: 요청 {len(texts)}, 응답 {len(vectors)}"
        )
    # 차원이 어긋난 벡터가 저장·색인까지 흘러가면 조용히 깨진다. 여기서 끊는다.
    for vector in vectors:
        if len(vector) != settings.embedding_dim:
            raise GeminiError(
                f"임베딩 차원이 설정과 다릅니다: 기대 {settings.embedding_dim}, "
                f"실제 {len(vector)} (모델={settings.embedding_model_name})"
            )
    return settings.embedding_model_name, vectors
