"""Claude(Anthropic) 호출 래퍼.

anthropic SDK 의존성을 이 파일에만 가둔다. `gemini_client` 와 같은 표면
(`generate_json` / `image_part`)을 제공하므로 호출부는 `llm` 디스패처만 본다.

전송은 auth2api(로컬 OAuth 프록시, 기본 `http://127.0.0.1:8317`) 경유가 기본이다.
Anthropic 직결도 같은 코드로 돈다 — `CLAUDE_BASE_URL` 을 비우면 SDK 기본 호스트로
간다.

임베딩은 여기 없다 — Anthropic 에는 임베딩 엔드포인트가 없다. `llm/__init__.py`
모듈 docstring 참고.
"""

import base64
import io
import logging
from typing import Any

from ..config import get_settings
# MOCK 응답 판별과 코드펜스 복구 파서는 프로바이더와 무관한 순수 함수다.
# 중복 구현하지 않고 그대로 재사용한다.
from ..gemini_client import _mock_json, parse_json_response

logger = logging.getLogger(__name__)


class ClaudeError(RuntimeError):
    pass


def _sdk():
    """anthropic 모듈을 지연 import 한다. MOCK_AI 경로는 SDK 없이도 돌아야 한다."""
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise ClaudeError("anthropic 이 설치되지 않았습니다.") from e
    return anthropic


# Client 는 내부에 httpx 커넥션 풀을 들고 있어 호출마다 만들면 낭비다.
# 설정은 기동 시 고정(get_settings lru_cache)이라 싱글턴으로 충분하다.
_client_instance: Any = None


def _client():
    """SDK 클라이언트 싱글턴.

    재시도는 SDK 에 맡긴다 — 408/409/429/5xx(529 overloaded 와 auth2api 의 503
    포함)를 지수 백오프로 자동 재시도하므로 `gemini_client._call_with_retry` 같은
    수동 루프가 필요 없다. 상한만 `CLAUDE_MAX_RETRIES` 로 올려 둔다.

    ⚠️ 529 는 업스트림 용량 부족이라 코드로 '배제'할 수는 없고 재시도로만 흡수된다.
    실측(2026-08-04) 기준 sonnet-5 는 3연속 529/503, sonnet-4-6·haiku-4-5 는
    같은 조건에서 1차 성공이었다 — 그래서 기본 모델을 4-6 계열로 잡았다.
    """
    global _client_instance
    if _client_instance is None:
        anthropic = _sdk()
        settings = get_settings()
        kwargs: dict[str, Any] = {
            "api_key": settings.claude_api_key,
            # ⚠️ anthropic SDK 의 timeout 은 **초**다(google-genai 신 SDK 는 밀리초).
            "timeout": settings.claude_timeout,
            "max_retries": settings.claude_max_retries,
        }
        if settings.claude_base_url:
            kwargs["base_url"] = settings.claude_base_url
        _client_instance = anthropic.Anthropic(**kwargs)
    return _client_instance


# 각 프롬프트가 이미 "반드시 아래 JSON만 출력. 마크다운·설명 금지."를 담고 있지만,
# system 으로 한 번 더 못박으면 서두("아래는 분석 결과입니다") 혼입이 줄어든다.
# parse_json_response 의 코드펜스 복구는 그래도 방어선으로 남겨 둔다.
_SYSTEM = (
    "You output a single raw JSON object and nothing else. "
    "No prose, no explanation, no markdown, no code fences."
)


def generate_json(model_name: str, parts: list[Any]) -> dict[str, Any]:
    """텍스트(또는 텍스트+이미지) 프롬프트를 보내고 JSON 응답을 받는다."""
    settings = get_settings()
    if settings.mock_ai:
        logger.info("MOCK_AI — generate_json 가짜 응답 (model=%s)", model_name)
        return _mock_json(parts)

    response = _client().messages.create(
        model=model_name,
        max_tokens=settings.claude_max_tokens,
        system=_SYSTEM,
        messages=[{"role": "user", "content": _content(parts)}],
        # thinking·effort 를 보내지 않는다. sonnet-4-6 과 haiku-4-5 모두 미전송이
        # '사고 끔'이고, 분류·추출 파이프라인에는 그게 맞다(사고 토큰은 출력 단가로
        # 과금된다). 품질이 필요한 경로가 생기면 그때 모델 세대별로 분기할 것 —
        # 4.6 은 thinking={"type":"adaptive"} + output_config.effort 이고,
        # haiku-4-5 는 budget_tokens 방식이며 effort 를 보내면 400 이다.
    )
    _log_usage(model_name, response)

    if response.stop_reason == "refusal":
        raise ClaudeError(
            "Claude 가 안전상 응답을 거부했습니다 "
            f"(model={model_name}, details={getattr(response, 'stop_details', None)})"
        )
    if response.stop_reason == "max_tokens":
        # 잘린 JSON 은 파서가 어차피 죽는다. 원인을 로그에 남겨 둔다.
        logger.warning(
            "Claude 응답이 max_tokens(%s)에서 잘렸습니다 — CLAUDE_MAX_TOKENS 를 올리세요 "
            "(model=%s)", settings.claude_max_tokens, model_name,
        )
    return parse_json_response(_first_text(response))


def _first_text(response: Any) -> str:
    """응답에서 첫 text 블록을 뽑는다(thinking 등 다른 블록 타입은 건너뛴다)."""
    for block in response.content or []:
        if getattr(block, "type", None) == "text":
            return block.text
    raise ClaudeError(
        f"Claude 응답에 text 블록이 없습니다 (stop_reason={response.stop_reason})"
    )


def _content(parts: list[Any]) -> list[dict[str, Any]]:
    """gemini_client 와 같은 parts 리스트를 Anthropic content 블록으로 바꾼다.

    이미지를 텍스트 앞으로 옮긴다 — Anthropic 은 이미지가 먼저 오는 편이 정확도가
    좋다고 안내한다. 호출부(`stages._agent_call`)는 긴 프롬프트 뒤에 이미지를
    append 하므로 정렬은 여기서 한다.
    """
    images: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, str):
            if part:
                texts.append({"type": "text", "text": part})
        elif isinstance(part, dict) and part.get("type") == "image":
            images.append(part)
        else:
            raise ClaudeError(
                f"지원하지 않는 프롬프트 파트 타입: {type(part).__name__}"
            )
    if not images and not texts:
        raise ClaudeError("빈 프롬프트는 보낼 수 없습니다.")
    return images + texts


# Anthropic 제약: 이미지당 5MB, 한 변 8000px. 상한에 붙이지 않고 여유를 둔다.
_IMAGE_MAX_BYTES = 4_500_000
_MAX_DIM = 8000
# 재인코딩 없이 그대로 보낼 수 있는 포맷.
_PASSTHROUGH_MIME = ("image/png", "image/jpeg", "image/gif", "image/webp")


def image_part(image_bytes: bytes) -> dict[str, Any]:
    """S3 에서 받은 바이트를 Anthropic image 블록으로 만든다.

    GMS 프록시의 본문 ~90KB 한도(`gemini_client._IMAGE_BYTE_BUDGET` = 40KB)가
    여기서는 없다. 실서버 스크린샷을 40KB q50 으로 뭉개던 열화가 사라지는 것이
    이 전환의 실질 이득이다.

    대신 모델의 입력 해상도 계층에 맞춰 줄인다. sonnet-4-6·haiku-4-5 는 긴 변
    1568px 계층이라 그보다 큰 이미지는 업스트림에서 어차피 축소되고 토큰만 더
    먹는다. 고해상도(2576px) 계층은 opus-4-7 이상과 sonnet-5 부터이므로, 모델을
    그쪽으로 올릴 때 `CLAUDE_IMAGE_LONG_EDGE` 도 함께 올린다.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError as e:  # pragma: no cover
        raise ClaudeError("pillow 가 설치되지 않았습니다.") from e

    long_edge = get_settings().claude_image_long_edge
    im = Image.open(io.BytesIO(image_bytes))
    width, height = im.size
    if (width * height <= long_edge * long_edge
            and max(width, height) <= _MAX_DIM
            and len(image_bytes) <= _IMAGE_MAX_BYTES):
        mime = Image.MIME.get(im.format or "", "")
        if mime in _PASSTHROUGH_MIME:
            # 이미 한도 안 + 지원 포맷 — 재인코딩 팽창 없이 원본 그대로 보낸다.
            return _block(image_bytes, mime)

    im = _fit(ImageOps.exif_transpose(im).convert("RGB"), long_edge)
    quality = 85
    while True:
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality)
        if buf.tell() <= _IMAGE_MAX_BYTES or quality <= 40:
            break
        quality -= 15
    return _block(buf.getvalue(), "image/jpeg")


def _fit(im: Any, long_edge: int) -> Any:
    """긴 변이 아니라 **총 픽셀**을 `long_edge²` 로 맞춘다(비율 유지).

    긴 변만 자르면 세로로 아주 긴 캡처(카톡 장문·전체 페이지 저장)에서 폭이
    수십 px 로 붕괴해 분류에 쓰는 시각 신호까지 사라진다. 면적 기준이면 같은
    토큰 예산에서 폭을 지킬 수 있다. 한 변 8000px 상한은 별도로 자른다.
    """
    width, height = im.size
    scale = min(
        1.0,
        ((long_edge * long_edge) / (width * height)) ** 0.5,
        _MAX_DIM / max(width, height),
    )
    if scale >= 1.0:
        return im
    return im.resize((max(1, int(width * scale)), max(1, int(height * scale))))


def _block(data: bytes, mime: str) -> dict[str, Any]:
    """base64 image 블록.

    SDK 타입이 아니라 평범한 dict 다 — 덕분에 MOCK_AI 경로가 anthropic 설치 없이
    돌고, `_mock_json` 이 문자열 파트만 보는 규약도 그대로 유지된다.
    """
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime,
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }


def _log_usage(model_name: str, response: Any) -> None:
    """토큰 사용량 계측. `gemini_client._log_usage` 와 같은 목적이다.

    계측 실패가 본 호출을 죽여서는 안 되므로 전부 best-effort 다.
    """
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        logger.info(
            "Claude usage model=%s input=%s output=%s cache_read=%s cache_write=%s",
            model_name,
            getattr(usage, "input_tokens", None),
            getattr(usage, "output_tokens", None),
            getattr(usage, "cache_read_input_tokens", None),
            getattr(usage, "cache_creation_input_tokens", None),
        )
    except Exception:  # noqa: BLE001 — 계측은 부가 기능
        pass
