"""LLM 프로바이더 디스패처.

환경변수 `LLM_PROVIDER` 하나로 Gemini ↔ Claude 를 바꿔 끼운다. 호출부는 이
모듈만 보고, 어느 프로바이더가 붙어 있는지 모른다.

    LLM_PROVIDER=gemini   # 기본값, 기존 동작 그대로
    LLM_PROVIDER=claude   # auth2api(로컬 OAuth 프록시) 경유 Anthropic

**임베딩은 이 모듈에 없다.** Anthropic 에는 임베딩 엔드포인트 자체가 없어서
(auth2api 경유 `POST /v1/embeddings` → 404, 2026-08-04 실측) 카테고리 벡터는
프로바이더와 무관하게 계속 `gemini_client.embed` 를 직접 쓴다. 그래서
`GEMINI_API_KEY` 는 Claude 모드에서도 여전히 필수다.

디스패치는 import 시점이 아니라 호출 시점에 한다. 덕분에 (1) 쓰지 않는
프로바이더의 SDK 는 import 조차 되지 않고, (2) MOCK_AI 경로가 두 SDK 모두
없이도 돈다. 모듈 조회는 `sys.modules` 딕셔너리 히트라 비용이 없다.
"""

from typing import Any

from ..config import get_settings
from ..gemini_client import GeminiError, parse_json_response  # noqa: F401 (재수출)


def _impl():
    """활성 프로바이더 구현 모듈을 돌려준다."""
    if get_settings().llm_provider == "claude":
        from . import claude
        return claude
    from .. import gemini_client
    return gemini_client


def provider() -> str:
    """현재 프로바이더 이름. 로깅·헬스체크 표기용."""
    return get_settings().llm_provider


def generate_json(model_name: str, parts: list[Any]) -> dict[str, Any]:
    """텍스트(또는 텍스트+이미지) 프롬프트를 보내고 JSON 응답을 받는다.

    `model_name` 은 호출부가 config 에서 꺼내 넘긴다. config 가 프로바이더별로
    이미 해석해 두므로 여기서 모델명을 다시 매핑하지 않는다.
    """
    return _impl().generate_json(model_name, parts)


def image_part(image_bytes: bytes) -> Any:
    """이미지 바이트를 활성 프로바이더의 프롬프트 파트로 만든다.

    반환 타입은 프로바이더마다 다르다(Gemini: SDK Part, Claude: dict). 호출부는
    이 값을 `generate_json` 의 parts 에 그대로 넣기만 하고 들여다보지 않는다.
    """
    return _impl().image_part(image_bytes)
