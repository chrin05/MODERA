"""이미 분석이 끝난 이미지들을 묶어 문서를 만든다.

분석 파이프라인과는 **완전히 분리된 기능**이다. 새 단계를 추가하지 않는다.

재료는 **Spring 이 요청 본문에 실어 보낸다**(10-1 과 같은 방식). AI 는 자기
색인이나 저장소를 조회하지 않는다. Spring 이 데이터 보관 주체이므로 AI 가
따로 들고 있는 사본을 읽으면 두 곳이 어긋날 수 있고, 조회 왕복·타임아웃·
소유자 검증이 전부 딸려온다. 받은 것만 쓰면 그 문제가 통째로 사라지고
OpenSearch 가 죽어 있어도 문서화는 동작한다.

    1) prepare_sources   : 받은 이미지 목록을 정리한다 (중복·빈 항목 제거)
    2) generate_document : Gemini 로 문서용 구조(제목·요약·섹션)를 만든다
    3) render_markdown   : 그 구조를 마크다운으로 찍는다 (파이썬이 한다)

3번을 모델에 맡기지 않는 이유: 마크다운 형식이 매번 흔들리고 코드펜스·잡문이
섞인다. 구조만 JSON 으로 받고 렌더링은 결정적으로 처리하면 출력이 항상 같은
모양이 되고, 렌더러는 네트워크 없이 테스트할 수 있다.
"""

import logging
import re
from typing import Any

from . import llm
from .config import get_settings
from .schemas import DocumentImage
from .timeutil import now_iso

logger = logging.getLogger(__name__)

# 한 번에 묶을 수 있는 이미지 수. 프롬프트가 커지면 지연·비용이 같이 늘고
# 모델이 뒤쪽 이미지를 흘린다. 넘으면 요청 단계에서 400 으로 끊는다.
# ponytail: 고정 상한. 30장을 넘겨야 하면 배치로 나눠 생성 후 병합할 것.
MAX_IMAGES = 30
# 이미지당 프롬프트에 넣을 OCR 상한(자). 스크린샷 OCR 은 대부분 이 안에 들어오고,
# 긴 것은 앞부분에 핵심이 몰려 있다.
# ponytail: 앞에서 자르는 단순 절단. 긴 문서 스크린샷은 뒷부분이 통째로 날아간다.
#           문제가 되면 문단 단위 추출이나 사전 요약으로 올릴 것.
OCR_CHARS = 1500


class NoSourceError(RuntimeError):
    """문서를 만들 수 있는 이미지가 하나도 없다."""


# ── 1) 소스 정리 ──────────────────────────────────────────────────────────
def prepare_sources(
    images: list[DocumentImage],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(사용할 이미지, 건너뛴 이미지) 를 돌려준다.

    조회하지 않는다. 어떤 이미지가 이 사용자 것이고 분석이 끝났는지는 Spring 이
    질의 단계에서 이미 걸렀다. 여기서는 중복과 '내용이 없는 항목'만 정리한다.
    """
    sources: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[int] = set()

    for image in images:
        if image.image_id in seen:
            continue
        seen.add(image.image_id)

        # 분석 결과와 마찬가지로 정제본이 있으면 그쪽을 쓴다(stages 와 같은 규칙).
        ocr = (image.ocr.refined_text or image.ocr.raw_text or "").strip()
        if not (image.title or image.summary or image.key_information or ocr):
            # 전부 비어 있으면 프롬프트에 빈 블록만 들어가고 모델이 지어내기 시작한다.
            skipped.append({"image_id": image.image_id, "reason": "NO_CONTENT"})
            continue

        sources.append({
            "image_id": image.image_id,
            "title": image.title,
            "summary": image.summary,
            "tags": image.tags,
            "category": image.category,
            "key_information": image.key_information,
            "ocr": ocr[:OCR_CHARS],
            "created_at": image.created_at,
        })

    return sources, skipped


# ── 2) 문서용 데이터 생성 ─────────────────────────────────────────────────
def build_prompt(
    sources: list[dict[str, Any]],
    title: str | None,
    instruction: str | None,
    language: str | None,
) -> str:
    blocks = []
    for s in sources:
        blocks.append(
            f"[이미지 #{s['image_id']}]\n"
            f"제목(참고용 메타데이터): {s['title']}\n"
            f"요약(참고용 메타데이터): {s['summary']}\n"
            f"카테고리: {s['category']}\n"
            f"태그: {', '.join(s['tags'])}\n"
            f"주요정보: {' / '.join(s['key_information'])}\n"
            f"촬영시각: {s['created_at']}\n"
            f"OCR 원문:\n{s['ocr']}"
        )

    title_rule = (
        f"문서 제목은 '{title}' 로 한다.\n" if title
        else "내용을 대표하는 문서 제목을 직접 정한다.\n"
    )
    instruction_rule = f"추가 요청: {instruction}\n" if instruction else ""
    language_rule = f"출력 언어는 {language} 로 한다.\n" if language else ""

    return (
        "아래는 사용자가 저장한 스크린샷들의 분석 결과와 OCR 원문이다. "
        "이것들을 종합해 사람이 처음부터 쓴 것처럼 읽히는 하나의 글로 다시 써라.\n\n"
        "[규칙]\n"
        + title_rule
        + "- 각 이미지의 '제목'·'요약'은 우리 분석기가 붙인 참고용 메타데이터다. 그 문장을"
        " 그대로 옮기거나 '제목:', '요약:' 같은 라벨을 출력하지 마라. 본문은 OCR 원문과"
        " 주요정보를 근거로 새 문장으로 쓴다.\n"
        "- 이미지 하나가 섹션 하나가 되면 안 된다. 이미지 경계는 결과물에 드러나지 않아야"
        " 한다. '이미지', '스크린샷', 이미지 번호를 본문에 쓰지 마라.\n"
        "- 소재끼리 직접적 연관이 없어 보여도 각각을 따로 나열하지 말고, 공통 축(목적·시기·"
        "주제·행동)을 찾아 적은 수의 섹션으로 통합한다. 한 섹션 안에서는 여러 소재의 내용을"
        " 한 흐름의 문장으로 이어 쓰고, 서로 어떻게 이어지는지 한 문장으로 연결해 준다.\n"
        "- 정말 어디에도 묶이지 않는 소재만 마지막 '그 외' 섹션에 함께 정리한다.\n"
        "- 각 섹션에는 근거가 된 이미지 번호를 imageIds 에 정확히 담는다(본문에는 쓰지 마라).\n"
        "- 가격·날짜·장소 같은 사실은 원문 그대로 쓴다. 확인되지 않은 값은 넣지 마라(추측 금지).\n"
        "- bullets 는 표처럼 나열할 항목에만 쓰고, 서술이 자연스러우면 body 만 채운다.\n"
        "- 마크다운 기호(#, -, *)를 값 안에 넣지 마라. 문서 조립은 서버가 한다.\n"
        + instruction_rule
        + language_rule
        + "\n반드시 아래 JSON만 출력. 마크다운·설명 금지.\n"
        '{"title":"...","summary":"...","sections":[{"heading":"...","body":"...",'
        '"bullets":["..."],"imageIds":[1,2]}]}\n\n'
        + "\n\n".join(blocks)
    )


def generate_document(
    sources: list[dict[str, Any]],
    title: str | None = None,
    instruction: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """LLM 으로 문서 구조를 만든다. 반환값은 그대로 render_markdown 에 넣는다."""
    settings = get_settings()
    prompt = build_prompt(sources, title, instruction, language)
    parsed = llm.generate_json(settings.document_model_name, [prompt])

    valid_ids = {s["image_id"] for s in sources}
    sections = []
    for raw in parsed.get("sections") or []:
        if not isinstance(raw, dict):
            continue
        sections.append({
            "heading": _oneline(raw.get("heading", "")),
            "body": _prose(raw.get("body", "")),
            "bullets": [_oneline(b) for b in (raw.get("bullets") or []) if str(b).strip()],
            # 모델이 없는 번호를 지어내면 출처 표기가 거짓이 된다. 실제 소스만 남긴다.
            "image_ids": [i for i in (raw.get("imageIds") or []) if i in valid_ids],
        })

    return {
        # 요청이 제목을 지정했으면 그 값으로 고정한다. 프롬프트로 부탁만 하면
        # 모델이 다른 제목을 내놨을 때 그게 채택돼 계약이 깨진다.
        "title": _oneline(title or parsed.get("title") or "문서"),
        "summary": _prose(parsed.get("summary", "")),
        "sections": sections,
    }


def _oneline(value: Any) -> str:
    """제목·항목에 줄바꿈이 섞이면 마크다운 구조가 깨진다."""
    return " ".join(str(value or "").split())


# 프롬프트로 "마크다운 기호를 넣지 마라" 라고 부탁하지만 모델은 종종 어긴다.
# body·summary 는 여러 줄 서술이라 _oneline 으로 뭉갤 수 없어 줄 앞 구조 기호만 막는다.
# `#` 이 들어오면 섹션이 문서 최상위 제목으로 승격되고, 코드펜스가 열리면 그 뒤의
# 출처 표까지 통째로 코드 블록에 먹힌다 — 렌더 결과가 항상 같은 모양이라는 계약이 깨진다.
_LEAD_MARKER = re.compile(r"^(\s*)(#{1,6}\s|>\s|\|)")
_FENCE = re.compile(r"^\s*(?:```|~~~)")


def _prose(value: Any) -> str:
    """여러 줄 서술. 줄 앞 마크다운 구조 기호를 중화한다."""
    lines = []
    for line in str(value or "").splitlines():
        if _FENCE.match(line):
            # 펜스는 escape 로 무력화되지 않는다(백틱 하나만 먹는다). 줄째로 버린다.
            continue
        lines.append(_LEAD_MARKER.sub(r"\1\\\2", line))
    return "\n".join(lines).strip()


# ── 3) 마크다운 렌더링 ────────────────────────────────────────────────────
def render_markdown(document: dict[str, Any]) -> str:
    """문서 구조 → 마크다운 문자열.

    이미지 id 는 렌더에 넣지 않는다. 사용자에게 내부 id 는 의미가 없고, 출처 표기가
    본문 흐름을 끊는다. 근거 매핑은 응답 JSON 의 sections[].imageIds 로만 나간다.
    """
    lines: list[str] = [f"# {document['title']}", ""]

    if document.get("summary"):
        lines += [document["summary"], ""]

    for section in document.get("sections") or []:
        lines += [f"## {section['heading']}", ""]
        if section.get("body"):
            lines += [section["body"], ""]
        if section.get("bullets"):
            lines += [f"- {b}" for b in section["bullets"]]
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ── 진입점 ────────────────────────────────────────────────────────────────
def generate(
    user_id: int,
    images: list[DocumentImage],
    title: str | None = None,
    instruction: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """1~3 을 이어 실행한다. 동기 함수라 asyncio.to_thread 로 호출한다."""
    sources, skipped = prepare_sources(images)
    if not sources:
        raise NoSourceError(
            f"문서로 만들 내용이 있는 이미지가 없습니다. (요청 {len(images)}건)"
        )

    document = generate_document(sources, title, instruction, language)
    markdown = render_markdown(document)
    logger.info(
        "문서 생성 userId=%s 사용=%s 건너뜀=%s 섹션=%s 길이=%s",
        user_id, len(sources), len(skipped),
        len(document["sections"]), len(markdown),
    )

    return {
        **document,
        "markdown": markdown,
        "source_image_ids": [s["image_id"] for s in sources],
        "skipped": skipped,
        "model_version": get_settings().document_model_name,
        "generated_at": now_iso(),
    }
