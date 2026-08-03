"""LLM 프로바이더 토글 자체 점검. 네트워크·SDK 없이 돈다.

검증 대상은 전부 조용히 틀어질 수 있는 것들이다 — LLM_PROVIDER 가 실제로 다른
구현으로 디스패치되는지, 프로바이더별 모델 이름공간이 서로 새지 않는지(gemini
모델명이 Anthropic 으로 가면 404), Anthropic content 블록 변환에서 이미지가
텍스트 앞으로 정렬되는지, 이미지 축소가 면적 예산과 한 변 상한을 지키는지.

    python scripts/llm_check.py
"""

import importlib
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# setdefault 가 아니라 덮어쓴다 — 셸에 .env.example 을 source 해 두면 빈 문자열이
# 이미 export 되어 있고(예: CLAUDE_API_KEY=), setdefault 는 그걸 "설정됨"으로 보아
# _required 가 거부한다. 점검은 외부 env 와 무관하게 돌아야 한다.
os.environ["INTERNAL_TOKEN"] = "check"
os.environ["GEMINI_API_KEY"] = "check"
os.environ["CLAUDE_API_KEY"] = "check"


def _settings(**env):
    """주어진 env 로 config 를 다시 읽는다(get_settings 는 lru_cache 라 재로드 필요)."""
    for key in ("LLM_PROVIDER", "LLM_MODEL_NAME", "VISION_MODEL_NAME",
                "CLAUDE_LLM_MODEL_NAME", "CLAUDE_VISION_MODEL_NAME",
                "CLAUDE_INFORMATIVE_MODEL_NAME"):
        os.environ.pop(key, None)
    os.environ.update(env)
    config = importlib.reload(importlib.import_module("app.config"))
    return config.get_settings()


def check_provider_dispatch():
    """LLM_PROVIDER 가 실제로 다른 구현 모듈로 가는지."""
    import app.llm as llm

    _settings(LLM_PROVIDER="gemini")
    importlib.reload(llm)
    assert llm.provider() == "gemini"
    assert llm._impl().__name__ == "app.gemini_client", llm._impl().__name__

    _settings(LLM_PROVIDER="claude")
    importlib.reload(llm)
    assert llm.provider() == "claude"
    assert llm._impl().__name__ == "app.llm.claude", llm._impl().__name__

    # 잘못된 값은 기동 시점에 죽어야 한다(런타임에 조용히 gemini 로 새지 않게).
    try:
        _settings(LLM_PROVIDER="openai")
    except RuntimeError:
        pass
    else:
        raise AssertionError("LLM_PROVIDER 오타를 잡지 못했다")
    print("ok  provider dispatch")


def check_model_namespaces():
    """프로바이더별 모델 이름공간이 서로 새지 않는지.

    팀 .env 에 LLM_MODEL_NAME=gemini-2.5-flash-lite 가 박혀 있는 상태에서
    LLM_PROVIDER=claude 로만 바꿔도 gemini 모델명이 Anthropic 으로 가면 안 된다.
    """
    s = _settings(LLM_PROVIDER="gemini")
    assert s.llm_model_name.startswith("gemini-"), s.llm_model_name
    assert s.embedding_model_name.startswith("gemini-"), s.embedding_model_name

    s = _settings(LLM_PROVIDER="claude", LLM_MODEL_NAME="gemini-2.5-flash-lite",
                  VISION_MODEL_NAME="gemini-2.5-flash-lite")
    for role in ("llm_model_name", "vision_model_name", "informative_model_name",
                 "document_model_name", "query_parse_model_name"):
        name = getattr(s, role)
        assert name.startswith("claude-"), f"{role} 에 gemini 이름이 샜다: {name}"
    # 4-6 계열 고정(529 회피). sonnet-5 가 기본으로 돌아오면 실패한다.
    assert s.llm_model_name == "claude-sonnet-4-6", s.llm_model_name
    assert s.vision_model_name == "claude-haiku-4-5", s.vision_model_name
    # 임베딩은 프로바이더와 무관하게 Gemini 유지.
    assert s.embedding_model_name.startswith("gemini-"), s.embedding_model_name

    # 개별 재정의는 CLAUDE_ 접두사로만 먹는다.
    s = _settings(LLM_PROVIDER="claude", CLAUDE_INFORMATIVE_MODEL_NAME="claude-haiku-4-5")
    assert s.informative_model_name == "claude-haiku-4-5"
    print("ok  model namespaces")


def check_content_blocks():
    """parts → Anthropic content 블록 변환. 이미지가 텍스트 앞으로 와야 한다."""
    _settings(LLM_PROVIDER="claude")
    from app.llm import claude
    importlib.reload(claude)

    img = {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                       "data": "eA=="}}
    # 호출부(stages)는 [프롬프트, 이미지] 순으로 넘긴다 → 뒤집혀 나와야 한다.
    blocks = claude._content(["prompt", img])
    assert [b["type"] for b in blocks] == ["image", "text"], blocks
    assert blocks[1]["text"] == "prompt"

    # 텍스트만.
    assert claude._content(["only text"]) == [{"type": "text", "text": "only text"}]
    # 빈 문자열은 버린다(Anthropic 은 빈 text 블록을 400 으로 거부한다).
    assert claude._content(["", "x"]) == [{"type": "text", "text": "x"}]

    for bad in ([], [""], [b"raw bytes"], [{"mime_type": "image/png", "data": b""}]):
        try:
            claude._content(bad)
        except claude.ClaudeError:
            pass
        else:
            raise AssertionError(f"잘못된 파트를 통과시켰다: {bad!r}")
    print("ok  content blocks")


def check_image_fit():
    """이미지 축소가 면적 예산과 한 변 상한을 지키는지.

    긴 변만 자르면 세로로 긴 캡처에서 폭이 붕괴한다 — 면적 기준이라 폭이 남아야 한다.
    """
    from PIL import Image

    _settings(LLM_PROVIDER="claude")
    from app.llm import claude
    importlib.reload(claude)

    budget = 1568 * 1568
    # 한도 안 → 손대지 않는다.
    small = Image.new("RGB", (900, 500))
    assert claude._fit(small, 1568).size == (900, 500)

    # 큰 이미지 → 면적 예산 안으로.
    big = claude._fit(Image.new("RGB", (4000, 3000)), 1568)
    assert big.size[0] * big.size[1] <= budget, big.size

    # 세로로 아주 긴 캡처(카톡 장문). 긴 변만 잘랐다면 폭이 84px 로 붕괴한다.
    tall = claude._fit(Image.new("RGB", (1080, 20000)), 1568)
    assert tall.size[0] * tall.size[1] <= budget, tall.size
    assert max(tall.size) <= claude._MAX_DIM, tall.size
    assert tall.size[0] >= 300, f"폭이 붕괴했다: {tall.size}"

    # 한 변 상한: 면적은 여유롭지만 한쪽이 8000px 을 넘는 경우.
    wide = claude._fit(Image.new("RGB", (30000, 40)), 1568)
    assert max(wide.size) <= claude._MAX_DIM, wide.size

    # image_part: 한도 안 PNG 는 재인코딩 없이 원본 그대로(팽창 방지).
    buf = io.BytesIO(); Image.new("RGB", (900, 500), (250, 248, 242)).save(buf, "PNG")
    part = claude.image_part(buf.getvalue())
    assert part["source"]["media_type"] == "image/png", part["source"]["media_type"]
    # 한도 밖은 JPEG 로 축소.
    buf = io.BytesIO(); Image.new("RGB", (4000, 3000)).save(buf, "PNG")
    part = claude.image_part(buf.getvalue())
    assert part["source"]["media_type"] == "image/jpeg", part["source"]["media_type"]
    assert part["type"] == "image" and part["source"]["type"] == "base64"
    print("ok  image fit")


def check_mock_path():
    """MOCK_AI 가 claude 프로바이더에서도 SDK 없이 도는지(로컬 배선 점검용)."""
    os.environ["MOCK_AI"] = "true"
    try:
        _settings(LLM_PROVIDER="claude")
        import app.llm as llm
        importlib.reload(llm)
        from app.llm import claude
        importlib.reload(claude)

        parsed = llm.generate_json("claude-haiku-4-5", [
            '반드시 아래 JSON만 출력.\n{"informative": true, "confidence": 0.0}'])
        assert parsed.get("informative") is True, parsed

        # 이미지 파트도 SDK 없이 만들어져야 한다(평범한 dict).
        from PIL import Image
        buf = io.BytesIO(); Image.new("RGB", (100, 100)).save(buf, "PNG")
        assert llm.image_part(buf.getvalue())["type"] == "image"
    finally:
        os.environ.pop("MOCK_AI", None)
    print("ok  mock path")


if __name__ == "__main__":
    check_provider_dispatch()
    check_model_namespaces()
    check_content_blocks()
    check_image_fit()
    check_mock_path()
    print("\n전부 통과")
