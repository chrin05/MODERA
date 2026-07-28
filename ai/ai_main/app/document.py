"""이미 분석이 끝난 이미지들을 묶어 문서를 만든다.

분석 파이프라인과는 **완전히 분리된 기능**이다. 새 단계를 추가하지 않는다.

재료는 **Spring 이 요청 본문에 실어 보낸다**(10-1 과 같은 방식). AI 는 자기
색인이나 저장소를 조회하지 않는다. Spring 이 데이터 보관 주체이므로 AI 가
따로 들고 있는 사본을 읽으면 두 곳이 어긋날 수 있고, 조회 왕복·타임아웃·
소유자 검증이 전부 딸려온다. 받은 것만 쓰면 그 문제가 통째로 사라지고
OpenSearch 가 죽어 있어도 문서화는 동작한다.

    1) prepare_sources   : 받은 이미지 목록을 정리한다 (중복·빈 항목 제거)
    2) generate_document : Gemini 로 문서용 구조(제목·요약·섹션)를 만든다
    3) render_html       : 그 구조를 모바일용 HTML 로 찍는다 (파이썬이 한다)

3번을 모델에 맡기지 않는 이유: 서식이 매번 흔들리고 코드펜스·잡문이 섞이며,
태그를 직접 뱉게 하면 이스케이프가 무너져 스크립트 주입 경로가 된다. 구조만
JSON 으로 받고 렌더링을 결정적으로 처리하면 **이미지 종류와 무관하게 레이아웃과
서식이 항상 같고**, 렌더러는 네트워크 없이 테스트할 수 있다.
"""

import logging
from datetime import datetime, timezone
from html import escape
from typing import Any

from . import gemini_client
from .config import get_settings
from .schemas import DocumentImage

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


# ponytail: main·stages·responses·jobs 에도 같은 함수가 있다(4번째 사본).
#           손대는 김에 정리하려면 공용 헬퍼로 빼고 5개 파일에서 import 할 것.
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


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
            f"제목: {s['title']}\n"
            f"요약: {s['summary']}\n"
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
        "이것들을 종합해 하나의 읽을 수 있는 문서로 재구성하라.\n\n"
        "[규칙]\n"
        + title_rule
        + "- 내용이 비슷한 이미지는 한 섹션으로 묶고, 성격이 다르면 섹션을 나눈다.\n"
        "- 각 섹션에는 근거가 된 이미지 번호를 imageIds 에 정확히 담는다.\n"
        "- 가격·날짜·장소 같은 사실은 원문 그대로 쓴다. 확인되지 않은 값은 넣지 마라(추측 금지).\n"
        "- bullets 는 표처럼 나열할 항목에만 쓰고, 서술이 자연스러우면 body 만 채운다.\n"
        "- 서식 기호(#, -, *)나 HTML 태그를 값 안에 넣지 마라. 문서 조립은 서버가 한다.\n"
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
    """Gemini 로 문서 구조를 만든다. 반환값은 그대로 render_html 에 넣는다."""
    settings = get_settings()
    prompt = build_prompt(sources, title, instruction, language)
    parsed = gemini_client.generate_json(settings.llm_model_name, [prompt])

    valid_ids = {s["image_id"] for s in sources}
    sections = []
    for raw in parsed.get("sections") or []:
        if not isinstance(raw, dict):
            continue
        sections.append({
            "heading": _oneline(raw.get("heading", "")),
            "body": str(raw.get("body", "") or ""),
            "bullets": [_oneline(b) for b in (raw.get("bullets") or []) if str(b).strip()],
            # 모델이 없는 번호를 지어내면 출처 표기가 거짓이 된다. 실제 소스만 남긴다.
            "image_ids": [i for i in (raw.get("imageIds") or []) if i in valid_ids],
        })

    return {
        # 요청이 제목을 지정했으면 그 값으로 고정한다. 프롬프트로 부탁만 하면
        # 모델이 다른 제목을 내놨을 때 그게 채택돼 계약이 깨진다.
        "title": _oneline(title or parsed.get("title") or "문서"),
        "summary": str(parsed.get("summary", "") or ""),
        "sections": sections,
    }


def _oneline(value: Any) -> str:
    """제목·항목에 줄바꿈이 섞이면 한 줄로 렌더되는 요소가 어긋난다."""
    return " ".join(str(value or "").split())


# ── 3) HTML 렌더링 ────────────────────────────────────────────────────────
# 모바일 WebView 전용 스타일. **내용과 무관하게 항상 동일하다** — 카테고리·장수·
# 섹션 수에 따라 달라지는 분기가 없다. 쇼핑 문서든 여행 문서든 같은 서식으로 나온다.
#
# 외부 폰트·CSS·스크립트를 쓰지 않는다. WebView 가 오프라인이거나 CSP 가 걸려 있어도
# 그대로 렌더된다.
_STYLE = """\
:root{
color-scheme:light dark;
--bg:#fff;--surface:#f7f8fa;--line:#e4e7ec;--line-soft:#edeff3;
--text:#16181d;--muted:#6b7280;--accent:#2f6fed;--accent-soft:#eaf0fe;
--radius:14px}
@media(prefers-color-scheme:dark){:root{
--bg:#0f1115;--surface:#171a21;--line:#272b34;--line-soft:#1f232b;
--text:#e7e9ee;--muted:#98a0ac;--accent:#7ba5ff;--accent-soft:#1b2436}}
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;
padding:clamp(22px,6vw,40px) clamp(16px,4.5vw,24px) 56px;
padding-left:max(clamp(16px,4.5vw,24px),env(safe-area-inset-left));
padding-right:max(clamp(16px,4.5vw,24px),env(safe-area-inset-right));
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
"Apple SD Gothic Neo","Noto Sans KR","Malgun Gothic",sans-serif;
font-size:16px;line-height:1.75;color:var(--text);background:var(--bg);
word-break:keep-all;overflow-wrap:anywhere}
.doc{max-width:680px;margin:0 auto}
.doc-head{margin-bottom:clamp(20px,5vw,28px)}
.doc-head h1{margin:0;font-weight:700;letter-spacing:-.02em;line-height:1.3;
font-size:clamp(1.45rem,5.5vw,1.9rem)}
.doc-summary{margin:12px 0 0;color:var(--muted);font-size:.95rem;line-height:1.7}
.sec{margin-top:clamp(14px,3.5vw,20px);padding:clamp(16px,4.5vw,22px);
background:var(--surface);border:1px solid var(--line-soft);border-radius:var(--radius)}
.sec h2{margin:0 0 12px;font-size:1.05rem;font-weight:700;line-height:1.45;
letter-spacing:-.01em;display:flex;align-items:center;gap:9px}
.sec h2::before{content:"";flex:none;width:3px;height:1.05em;
border-radius:2px;background:var(--accent)}
.sec-body{margin:0}
.sec-body+.sec-list{margin-top:12px}
.sec-list{margin:0;padding:0;list-style:none}
.sec-list li{position:relative;margin-top:9px;padding-left:17px}
.sec-list li:first-child{margin-top:0}
.sec-list li::before{content:"";position:absolute;left:3px;top:.74em;
width:5px;height:5px;border-radius:50%;background:var(--accent);opacity:.75}
.sec-src{margin:16px 0 0;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.sec-src-label{font-size:.75rem;color:var(--muted)}
.ref{font-size:.75rem;line-height:1.7;padding:1px 9px;border-radius:999px;
background:var(--accent-soft);color:var(--accent);font-variant-numeric:tabular-nums}
.doc-src{margin-top:clamp(30px,7vw,44px)}
.doc-src h2{margin:0 0 10px;font-size:.75rem;font-weight:600;letter-spacing:.08em;
text-transform:uppercase;color:var(--muted)}
.src-list{list-style:none;margin:0;padding:0;border-top:1px solid var(--line)}
.src-item{display:flex;gap:12px;align-items:flex-start;
padding:12px 2px;border-bottom:1px solid var(--line-soft)}
.src-item:last-child{border-bottom:0}
.src-id{flex:none;min-width:40px;padding:1px 7px;border-radius:7px;text-align:center;
background:var(--surface);border:1px solid var(--line-soft);
font-size:.72rem;line-height:1.7;color:var(--muted);font-variant-numeric:tabular-nums}
.src-body{flex:1;min-width:0}
.src-title{display:block;font-size:.9rem;line-height:1.55}
.src-meta{display:block;margin-top:1px;font-size:.76rem;color:var(--muted)}"""


def _esc(value: Any) -> str:
    """HTML 이스케이프.

    모델 출력과 OCR 원문이 그대로 들어온다. 스크린샷에 `<script>` 가 찍혀 있으면
    OCR 이 그 문자열을 읽어 오고, 이스케이프하지 않으면 WebView 에서 실행된다.
    """
    return escape(str(value if value is not None else ""), quote=True)


def _date(value: Any) -> str:
    """ISO-8601 을 날짜까지만 줄인다. 좁은 화면에서 전체 타임스탬프는 줄을 넘긴다."""
    text = _oneline(value)
    return text[:10] if len(text) >= 10 and text[4] == "-" else text


def render_html(document: dict[str, Any], sources: list[dict[str, Any]]) -> str:
    """문서 구조 + 소스 목록 → 모바일용 단일 HTML 문서.

    외부 리소스가 없는 self-contained 문서라 WebView 에 그대로 load 하면 된다.
    빈 값(요약 없음·본문 없음 등)은 요소를 생략한다. 남기면 빈 여백만 생긴다.
    """
    out: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="ko">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1,'
        'viewport-fit=cover">',
        # 스크립트 실행 자체를 차단한다. 모든 값을 이스케이프하고 있지만, 그게
        # 한 군데라도 새면 OCR 로 흘러든 문자열이 WebView 에서 실행된다.
        # default-src 'none' 이면 script·fetch·iframe·폰트·이미지가 전부 막히고,
        # 인라인 <style> 하나만 허용한다. 이 문서는 그 외에 아무것도 쓰지 않는다.
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\';'
        " style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">",
        f"<title>{_esc(document['title'])}</title>",
        f"<style>{_STYLE}</style>",
        "</head>",
        "<body>",
        '<article class="doc">',
        '<header class="doc-head">',
        f"<h1>{_esc(document['title'])}</h1>",
    ]
    if document.get("summary"):
        out.append(f'<p class="doc-summary">{_esc(document["summary"])}</p>')
    out.append("</header>")

    for section in document.get("sections") or []:
        out += ['<section class="sec">', f"<h2>{_esc(section['heading'])}</h2>"]
        if section.get("body"):
            out.append(f'<p class="sec-body">{_esc(section["body"])}</p>')
        if section.get("bullets"):
            out.append('<ul class="sec-list">')
            out += [f"<li>{_esc(b)}</li>" for b in section["bullets"]]
            out.append("</ul>")
        if section.get("image_ids"):
            refs = "".join(f'<span class="ref">#{int(i)}</span>'
                           for i in section["image_ids"])
            out.append('<p class="sec-src">'
                       f'<span class="sec-src-label">출처</span>{refs}</p>')
        out.append("</section>")

    # 어떤 스크린샷에서 나온 문서인지 남긴다. 사용자가 원본을 다시 찾을 수 있어야 한다.
    # 표가 아니라 세로 목록이다 — 좁은 화면에서 4열 표는 가로 스크롤이 생긴다.
    if sources:
        out += ['<footer class="doc-src">', "<h2>출처</h2>", '<ul class="src-list">']
        for s in sources:
            meta = " · ".join(p for p in (_esc(s.get("category")),
                                          _date(s.get("created_at"))) if p)
            out += [
                '<li class="src-item">',
                f'<span class="src-id">#{int(s["image_id"])}</span>',
                '<span class="src-body">',
                f'<span class="src-title">{_esc(s.get("title")) or "제목 없음"}</span>',
                f'<span class="src-meta">{meta}</span>' if meta else "",
                "</span>",
                "</li>",
            ]
        out += ["</ul>", "</footer>"]

    out += ["</article>", "</body>", "</html>"]
    # 빈 문자열은 조건부로 안 붙인 요소 자리다. 걸러야 빈 줄이 생기지 않는다.
    return "\n".join(line for line in out if line) + "\n"


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
    html = render_html(document, sources)
    logger.info(
        "문서 생성 userId=%s 사용=%s 건너뜀=%s 섹션=%s 길이=%s",
        user_id, len(sources), len(skipped),
        len(document["sections"]), len(html),
    )

    return {
        **document,
        "html": html,
        "source_image_ids": [s["image_id"] for s in sources],
        "skipped": skipped,
        "model_version": get_settings().llm_model_name,
        "generated_at": _now_iso(),
    }
