"""단계별 분석 실행 (명세 10-1) 과 결과 콜백 (명세 10-4).

프로토타입의 통짜 run_pipeline 을 stage 단위로 분리했다.
한 요청은 LLM / IMAGE_ANALYSIS / AGENT 중 하나만 실행하고, 다음 단계로
넘길지는 Spring 이 결정한다.
"""

import asyncio
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from . import category_store, gemini_client, llm, search, spring_client, storage
from .category import CategoryResolution, normalize_name, resolve_category
from .config import get_settings
from .jobs import job_registry, job_store
from .timeutil import now_iso
from .schemas import (
    AnalyzeRequest,
    CallbackError,
    CallbackRequest,
    CategoryCandidate,
    KnowledgeCandidates,
)

logger = logging.getLogger(__name__)

# ── 카테고리 정책 — 단일 출처 ──────────────────────────────────────────────
# 목록·정의·tie-break·예시문을 전부 이 구조 하나에 모은다. 프롬프트 문장은
# _category_prompt_section() 이 여기서 렌더링하므로, 개편 회의 결과 반영 =
# 이 dict 수정이 전부다. 예전에는 이름 상수와 예시 "문장"이 프롬프트에 흩어져
# 있어서(예: 게임 예문이 NEW_NAME_EXAMPLES[0]의 의미에 몰래 결합) 목록을 바꾸면
# 문장이 어긋나는 사고가 났다. few_shot 을 (장면, 답) 구조로 두면 문장과 이름이
# 항상 함께 움직이고, _check_category_config() 가 이름 멤버십을 기동 시 검증한다.
#
# categories : {이름: 포함 조건 or None}. 조건이 있으면 프롬프트에 "이름(조건)"으로
#              보여 러프한 이름의 과다 흡수를 줄인다(팀 합의 방식). None 이면 이름만.
# tie_break  : 다주제 이미지의 우선 규칙. exceptions 는 "단, ..." 문장 목록.
# empty      : 비정보성(EMPTY) 전용 — 정보성 이미지는 여기로 보내지 않는다.
# new_name_examples : 신규 이름의 입도 예시. 목록에 없는 이름만.
# few_shot   : (장면, {"existing": 이름} | {"new": 이름}) 쌍. existing 은 목록에
#              있어야 하고 new 는 없어야 한다 — 렌더링과 검증이 함께 강제한다.
# ⚠️ 기본 목록 승격 문턱(오너 규칙, 2026-07-31) — 셋 다 만족할 때만 추가한다:
#   1) 실측에서 반복 오분류가 확인될 것(일회성 아쉬움 금지 — '정보' 16장도 거절했다)
#   2) 기존 카테고리의 "정의 조정"으로 해결 불가할 것(목록 추가는 최후 수단)
#   3) 형태가 아니라 내용 기준일 것
# 나머지 롱테일은 AI 신규 생성이 흡수한다 — 그게 이 제품의 차별점이며, 기본
# 목록은 "모두에게 공통인 최소 골격"으로 유지한다(17→13 으로 줄인 방향을 지킬 것).
CATEGORY_POLICY: dict[str, Any] = {
    "categories": {
        # 13종 v2 (2026-07-31 확정, 오너 단독 권한) — 실사진 111장 오너 라벨 근거:
        # 쿠폰(라벨 7장 전부 음식·쇼핑으로 오분류), 문서(6장 학습 블랙홀),
        # 게임(오너가 여가 대신 게임 5회 입력 + AI 조차 번들 무시하고 생성).
        # 학습 정의에서 "업무 자료" 제거(안내문·공지 블랙홀 21건 실측),
        # 쇼핑 정의를 실물 상품으로 좁힘(택배 알림 흡수 차단 — AI 가 택배를
        # 신규 생성하는 것이 오너 의도). 구 17종은 git 히스토리 참조.
        "쇼핑": "실물 상품의 정보·주문·구매 화면",
        "음식": "맛집·메뉴·레시피·배달",
        "여행": "숙소·항공·기차 등 교통·여행지",
        "일정": "모임·행사·마감·접수 등 시각이 정해진 약속",
        "금융": "계좌·송금·주식·투자·보험",
        "학습": "강의·자격증·시험·수업 자료",
        "게임": "게임 화면·아이템·공략",
        "쿠폰": "기프티콘·금액권·상품권·교환권",
        "문서": "동의서·확인서·증명서·서약서",
        "건강": "병원·처방·운동",
        "여가": "영상·음악·공연·취미",
        # SNS 는 형태가 아니라 '콘텐츠 결'로 정의한다 — 짤·밈·공감툰은 여가(활동)와
        # 결이 다르다는 오너 판단(2026-07-31). 정의를 좁혀 두어 피드 캡처라도
        # 내용이 맛집·상품이면 여전히 음식·쇼핑으로 간다(형태 아닌 내용 원칙 유지).
        "SNS": "밈·짤·유머·공감툰, 유머·잡담 대화 캡처 등 가벼운 콘텐츠",
        "기타": "비정보성 이미지 전용",
    },
    "tie_break": {
        "name": "일정",
        "rule": "시간이 정해진 약속·모임·행사·마감이 중심이면",
        "exceptions": ["항공·기차·버스 승차권과 숙소 예약은 '여행'으로 보낸다"],
    },
    "empty": "기타",
    # ⚠️ 신규 이름의 "실명 예시"는 넣지 않는다(빈 목록 유지). 예시로 심은 이름은
    # 모델이 그대로 생성하는 숨은 기본 카테고리가 된다 — 실측: 예시 'MBTI' 가
    # 실제 생성됨. "AI 가 사용자 데이터에서 만든 카테고리"라는 제품 서사를
    # 지키려면 신규 이름은 100% 콘텐츠 유래여야 한다(2026-07-31 오너 결정).
    # 입도(폭) 통제는 렌더러의 서술형 규칙이 담당한다.
    "new_name_examples": [],
    # few_shot 도 같은 이유로 "기존 후보" 예시만 둔다(공개된 기본 목록이라 무해).
    # 대화 캡처의 분기(형태 아닌 내용 원칙): 약속이 담긴 대화 → 일정,
    # 유머·잡담 대화 → SNS. 대비쌍으로 경계를 가르친다.
    "few_shot": [
        ("특정 게임 아이템 화면", {"existing": "게임"}),
        ("특정 호텔의 식당 안내", {"existing": "음식"}),
        ("약속 날짜·장소를 정하는 대화 캡처", {"existing": "일정"}),
        ("친구와 웃긴 얘기를 주고받은 대화 캡처", {"existing": "SNS"}),
    ],
}

# 파생 별칭 — 시드 생성·재분석 API 등 외부 참조용. 정책이 유일한 출처다.
DEFAULT_CATEGORIES = list(CATEGORY_POLICY["categories"])
TIE_BREAK_CATEGORY = CATEGORY_POLICY["tie_break"]["name"]
EMPTY_CATEGORY = CATEGORY_POLICY["empty"]
NEW_NAME_EXAMPLES = list(CATEGORY_POLICY["new_name_examples"])


def _check_category_config() -> list[str]:
    """카테고리 정책의 내적 정합성을 확인한다. 문제 목록을 돌려준다(기동 시 경고).

    개편을 반영할 때 목록만 고치고 규칙·예시를 잊는 것이 가장 흔한 사고다.
    죽이지 않고 경고만 한다 — 분류 품질은 떨어지지만 서비스는 계속 돌아야 한다.
    """
    cats = CATEGORY_POLICY["categories"]
    problems = []
    if TIE_BREAK_CATEGORY not in cats:
        problems.append(f"tie_break '{TIE_BREAK_CATEGORY}' 가 categories 에 없다")
    if EMPTY_CATEGORY not in cats:
        problems.append(f"empty '{EMPTY_CATEGORY}' 가 categories 에 없다")
    overlap = [n for n in NEW_NAME_EXAMPLES if n in cats]
    if overlap:
        problems.append(f"new_name_examples 가 목록과 겹친다(신규 예시로 부적절): {overlap}")
    for scene, answer in CATEGORY_POLICY["few_shot"]:
        if "existing" in answer and answer["existing"] not in cats:
            problems.append(f"few_shot '{scene}' 의 기존 후보 '{answer['existing']}' 가 목록에 없다")
        if "new" in answer and answer["new"] in cats:
            problems.append(f"few_shot '{scene}' 의 신규 예시 '{answer['new']}' 가 이미 목록에 있다")
    return problems


def _category_prompt_section(candidate_names: list[str] | str) -> str:
    """카테고리 정책을 프롬프트 문단으로 렌더링한다.

    후보 이름이 정책에 정의(포함 조건)를 가지면 "이름(조건)" 으로 보여준다 —
    사용자별 신규 카테고리(정의 없음)는 이름 그대로다.
    """
    pol = CATEGORY_POLICY
    defs = pol["categories"]
    names = (candidate_names if isinstance(candidate_names, list)
             else [str(candidate_names)])
    shown = ", ".join(f"{n}({defs[n]})" if defs.get(n) else str(n) for n in names)
    tb = pol["tie_break"]
    exceptions = "".join(f"단, {e}. " for e in tb["exceptions"])
    # 신규 이름 규칙 — 실명 예시가 있으면 쓰되(현재 정책은 비움), 없으면 서술형으로
    # 입도만 가르친다. 실명을 심으면 그 이름이 그대로 생성되는 것이 실측됐다.
    if pol["new_name_examples"]:
        naming = (f"새 이름은 여러 이미지가 공유할 수 있는 주제 단위"
                  f"(예: {', '.join(pol['new_name_examples'])})여야 한다. ")
    else:
        naming = ("새 이름은 이 화면 하나가 아니라 같은 부류의 화면 여러 장이 쌓일 "
                  "폴더 이름이어야 한다 — 화면 하나에만 맞는 좁은 이름을 만들지 말고, "
                  "그 내용이 속한 분야의 이름을 골라라. ")
    examples = " ".join(
        (f"{scene} → '{a['new']}',"
         if "new" in a else f"{scene} → 기존 후보({a['existing']}),")
        for scene, a in pol["few_shot"]
    ).rstrip(",")
    example_line = f"예: {examples}. " if examples else ""
    return (
        f"기존 카테고리 후보: {shown}. 내용과 무리 없이 맞는 후보가 있으면 그것을 골라라. "
        "단, 어느 후보도 맞지 않는데 억지로 끼워 맞추지는 마라 — 그때만 새 카테고리 이름을 제안하라(2~6자). "
        + naming +
        "브랜드명·상호명·장소명·제품명은 카테고리로 만들지 마라 — 그런 구체 정보는 태그와 주요정보에 담는다. "
        "새 이름을 제안하기 전에 후보에 같은 주제가 이미 있으면 반드시 그 표기를 그대로 재사용하라(축약·변형 금지). "
        f"여러 주제에 걸치는 이미지는 우선순위를 지켜라: {tb['rule']} '{tb['name']}'을 우선한다. "
        f"{exceptions}"
        f"'{pol['empty']}'는 고르지 마라 — 어느 후보도 맞지 않으면 반드시 새 이름을 제안하라. "
        f"{example_line}"
        "categories 에는 최종 카테고리명 하나만 넣어라.\n\n"
    )


def seed_default_category_vectors() -> int:
    """기본 카테고리 이름 벡터를 전역 시드로 심는다(멱등). 심은 개수를 돌려준다.

    이미지가 0장인 카테고리는 centroid 를 만들 수 없어 이름 임베딩으로 대신하는데,
    그 값은 사용자와 무관하게 같다. 그래서 사용자별로 복제하지 않고 user_id=0
    문서(기본 목록 수만큼)로 한 번만 저장한다.

    기동마다 부르지만 이미 있는 시드는 다시 임베딩하지 않는다 — 조회 1회로 빠진
    이름만 골라 배치로 임베딩한다. 임베딩 모델·차원을 바꾸면 조회 필터가 기존
    시드를 무효로 보므로 자동으로 전량 재생성된다.

    실패해도 예외를 올리지 않는다. 시드가 없으면 콜드 스타트 판정이 이름
    완전일치로만 동작하고(품질만 떨어진다), 다음 기동에서 다시 시도한다.
    """
    for problem in _check_category_config():
        logger.warning("카테고리 설정 불일치: %s", problem)
    try:
        existing = category_store.load_category_vectors(category_store.SEED_USER_ID)
        missing = [n for n in DEFAULT_CATEGORIES if normalize_name(n) not in existing]
        if not missing:
            logger.info("카테고리 시드 %s건 이미 존재 — 임베딩 생략", len(existing))
            return 0
        _, vectors = gemini_client.embed(missing, "DOCUMENT")
        written = category_store.put_seed_category_vectors(dict(zip(missing, vectors)))
        logger.info("카테고리 시드 %s건 생성 (기존 %s건)", written, len(existing))
        return written
    except Exception as e:
        logger.warning("카테고리 시드 준비 실패: %s — 콜드 스타트는 이름 일치로 진행", e)
        return 0


@contextmanager
def _timed(timings: dict[str, float], name: str):
    """구간 소요 시간을 초 단위로 기록한다.

    "1장에 몇 초 걸리는가" 를 추정이 아니라 로그로 답하기 위한 계측이다.
    예외가 나도 그 시점까지의 시간은 남긴다.
    """
    started = time.perf_counter()
    try:
        yield
    finally:
        timings[name] = round(time.perf_counter() - started, 2)


# 동시 실행 제한용 세마포어. 실행 중인 이벤트 루프에 바인딩되도록 지연 생성한다.
#
# ⚠️ asyncio.Semaphore 는 재진입이 안 된다. 같은 태스크가 두 번 잡으면 자기 자신이
#    놓아주기를 기다리며 영원히 멈춘다. 그래서 **진입점에서만** 잡는다:
#      - execute_stage        (/internal/v1/analyze → 단계 실행, FULL 포함)
#      - run_app_analysis     (/api/v1/.../upload-complete → 앱 직결 전체 분석)
#    실제 파이프라인 본체인 _run_full_pipeline 은 절대 잡지 않는다.
_stage_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _stage_semaphore
    if _stage_semaphore is None:
        _stage_semaphore = asyncio.Semaphore(get_settings().max_concurrent_stages)
    return _stage_semaphore


# ── 1) LLM 정보성 판정 ────────────────────────────────────────────────────
def run_llm(ocr_text: str) -> tuple[str, dict[str, Any]]:
    """OCR 텍스트가 저장·검색할 가치가 있는 정보인지 판정한다."""
    if not ocr_text or not ocr_text.strip():
        # OCR 텍스트가 없으면 모델을 호출하지 않고 EMPTY 로 돌려준다.
        return "EMPTY", {"informative": False, "confidence": 0.0,
                         "reason": "OCR 텍스트가 비어 있음"}

    settings = get_settings()
    prompt = (
        "너는 스크린샷 정보 분석기다. 아래 OCR 텍스트가 개인 지식 DB에 저장·검색할 가치가 있는 "
        "'정보성' 콘텐츠인지 판단하라.\n"
        "정보성 예: 상품/가격, 일정/예약, 쿠폰/할인, 장소/맛집, 채용, 개발 지식/오류, 기사 요지 등 "
        "나중에 다시 찾아볼 실질 정보.\n"
        # 도구 화면 예시는 경량 모델용 보강(2026-07-31): 계산기 화면의 숫자를
        # '정보'로 오판해 통과시키면, AGENT 의 '기타 금지' 규칙과 결합해
        # '계산' 같은 쓰레기 카테고리가 생긴다(실측). 예시로 명시해 막는다.
        "비정보성 예: 순수 UI 요소(홈/검색/설정 같은 내비게이션 텍스트만 있는 화면), "
        "계산기·시계·배터리처럼 도구 화면에 떠 있는 일시적 숫자, 네트워크 오류·로딩 화면, "
        "빈 화면, 의미 없는 밈/장식.\n"
        "반드시 아래 JSON만 출력. 마크다운·설명 금지.\n"
        '{"informative": true, "confidence": 0.0, "reason": "간단한 근거"}\n\n'
        f"OCR 텍스트:\n{ocr_text}"
    )
    # 이진 판단이라 경량 모델 전용 스위치를 쓴다 — AGENT 모델을 올릴 때
    # 이 호출까지 따라 올라가지 않게 분리(비용 절반, config 참조).
    parsed = llm.generate_json(settings.informative_model_name, [prompt])
    return "COMPLETED", {
        "informative": bool(parsed.get("informative", False)),
        "confidence": float(parsed.get("confidence", 0.0)),
        "reason": parsed.get("reason", ""),
    }


# ── 2) 서버 이미지 분석 ───────────────────────────────────────────────────
def run_image_analysis(image_ref: str, image_bytes: bytes | None = None) -> dict[str, Any]:
    """image_ref 는 http(s) URL 또는 s3Key 다. storage 가 형태를 보고 처리한다.

    image_bytes 를 넘기면 원본을 다시 내려받지 않는다(썸네일 만들 때 읽어둔 것 재사용).
    """
    settings = get_settings()
    if image_bytes is None:
        image_bytes = storage.fetch_image(image_ref)
    prompt = (
        "이미지를 분석하라. 콘텐츠 유형을 한 문장으로 설명하고(description), "
        "눈에 보이는 주요 객체(objects)와 화면에서 읽히는 핵심 텍스트/브랜드/가격/날짜"
        "(detected_texts)를 뽑아라.\n"
        "반드시 아래 JSON만 출력. 마크다운·설명 금지.\n"
        '{"description":"...","detected_texts":["..."],"objects":["..."]}'
    )
    parsed = llm.generate_json(
        settings.vision_model_name, [prompt, llm.image_part(image_bytes)]
    )
    return {
        "description": parsed.get("description", ""),
        "detectedTexts": parsed.get("detected_texts", []),
        "objects": parsed.get("objects", []),
    }


# ── 3) AGENT (OCR + 이미지 분석 종합) ─────────────────────────────────────
def run_agent_generation(
    ocr_text: str,
    image_analysis: dict[str, Any],
    candidate_names: list[str],
    max_tags: int | None,
    language: str | None,
    existing_tags: list[str] | None = None,
    image_bytes: bytes | None = None,
) -> dict[str, Any]:
    """image_bytes 를 주면 융합 모드 — 모델이 화면 이미지를 직접 보고 정보성 판정까지
    한 호출로 수행한다(informative 키 추가). 실측(2026-07-31, 실사진 40장 파일럿):
    별도 비전 요약을 거치는 기존 경로 45% vs 이미지 직접 투입 70% — 기프티콘 바코드·
    문서 서식 같은 시각 신호가 요약 한 줄로 압축되며 증발하던 것이 원인이었다.
    """
    settings = get_settings()
    tag_rule = (
        f"[태그] 핵심 키워드 위주로 최대 {max_tags}개, 중복·과도한 일반 태그 제외. 사람 이름은 태그로 만들지 마라(본인 갤러리라 검색에 쓸 데가 없고, 손글씨·서명은 오독 위험이 크다).\n"
        if max_tags else "[태그] 중복·과도한 일반 태그 제외. 사람 이름은 태그로 만들지 마라.\n"
    )
    # 기존 태그를 기준점으로 준다. 의미가 같은 태그의 난립('맛집' vs '맛집추천')을 막고
    # 어휘를 수렴시킨다. 카테고리의 기존 후보 제시와 같은 취지. 없으면 자유 생성(콜드스타트).
    if existing_tags:
        tag_rule += (
            f"기존 태그 목록: {existing_tags}. 의미가 같은 태그가 이 목록에 있으면 "
            "새로 만들지 말고 그대로 재사용하라. 목록에 맞는 것이 없을 때만 새 태그를 만들어라.\n"
        )
    tag_rule += "\n"
    language_rule = f"출력 언어는 {language} 로 한다.\n" if language else ""
    # 캘린더용 일정 추출(schedule). 상대 날짜는 기준 시각(서버 현재 KST)으로 해석한다.
    # 스크린샷 캡처 시각이 요청에 없어 분석 시각을 기준으로 쓴다(대부분 일정은 절대 날짜라 영향 작음).
    ref_now = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    schedule_rule = (
        "[일정] 예약·티켓·행사·마감처럼 캘린더에 올릴 일정이 화면에 있으면 schedule 로 뽑아라.\n"
        "- startAt: 행사 시작 시각. endAt: 행사 종료 시각. **마감 기한('~까지')은 endAt 에 넣고 startAt 은 null 로 둬라.**\n"
        f"- ISO-8601(KST). 시각을 알면 '2026-08-03T14:30:00+09:00', 날짜만 알면 '2026-08-03'. 상대 날짜(내일·이번 주 금요일 등)는 기준 시각 {ref_now} 로 계산.\n"
        "- 연도가 없으면 기준 시각 기준 가장 가까운 미래로. 확인 안 된 값은 null(추측 금지). 일정이 전혀 없으면 둘 다 null.\n\n"
    )
    if image_bytes is not None and len(ocr_text or "") > 3000:
        # GMS 본문 한도(~90KB) 안에서 이미지와 텍스트가 몸을 나눠 쓴다. 한글은
        # JSON 이스케이프로 글자당 6바이트라, 텍스트 밀집 스크린샷은 OCR 만으로
        # 수십 KB 가 되어 이미지와 합치면 한도를 넘긴다(실측 4장 400). 초과분은
        # 잘라도 이미지가 원문을 보완하므로 품질 손실이 작다.
        ocr_text = ocr_text[:3000] + " …(길이 제한으로 이하 생략)"
    if image_bytes is not None:
        header = (
            "첨부한 화면 이미지와 OCR 텍스트를 함께 보고 개인 지식 DB용 메타데이터를 생성하라.\n\n"
            "[정보성] 먼저 저장·검색할 가치가 있는 화면인지 판정하라. "
            "순수 UI 요소(홈/검색/설정 같은 내비게이션만 있는 화면), 계산기·시계·배터리처럼 "
            "도구 화면에 떠 있는 일시적 숫자, 네트워크 오류·로딩 화면, 빈 화면, 의미 없는 "
            "밈/장식이면 informative 를 false 로 하고 나머지 필드는 비워 둬라. "
            "카메라로 찍은 일상 사진처럼 읽을 글자가 전혀 없는 이미지도 false 다 — "
            "이 서비스는 화면 속 정보를 모으는 곳이라 글자 없는 사진은 대상이 아니다. "
            "단, 화이트보드·영수증·명함처럼 글자를 찍은 사진은 정보성이다. "
            "정보성일 때만 아래를 채운다.\n\n"
            "[썸네일] thumbnail_focus 에 화면의 시각적 핵심(상품·음식 사진, 그림, 지도, "
            "포스터 등)이 있는 세로 위치를 0.0(맨 위)~1.0(맨 아래) 숫자로 담아라. "
            "가격표·버튼·설명 텍스트가 아니라 눈에 보여줄 이미지가 있는 곳이다. "
            "그런 시각 요소가 없으면 본문 내용이 시작되는 위치를 담아라.\n\n"
            # 프론트 상세화면이 "분석 근거"로 보여주는 값이다(2026-08-03 프론트 요청).
            # 그래서 요약·선별이 아니라 **OCR 원문 교정**으로 범위를 좁힌다 — 없던
            # 내용을 채우거나 UI 잡음을 지우면 원문과 대응되지 않아 근거로 못 쓴다.
            "[정제텍스트] refined_text 에 OCR 텍스트를 그대로 살리면서 깨진 글자만 "
            "바로잡아 담아라. 이미지를 보고 오인식을 교정하고(예: '동으1서' → '동의서'), "
            "줄바꿈·공백만 읽기 좋게 정돈하라. OCR 에 없는 내용을 새로 채우지 말고, "
            "있는 줄을 지우거나 요약하지도 마라 — 버튼·탭 같은 UI 문구도 OCR 에 있으면 "
            "그대로 남긴다. 손글씨·서명처럼 확신할 수 없는 글자는 추측해 바꾸지 마라. "
            "OCR 이 비어 있으면 빈 문자열.\n\n"
        )
    else:
        header = "OCR 텍스트와 이미지 분석 결과를 종합해 개인 지식 DB용 메타데이터를 생성하라.\n\n"
    prompt = (
        header
        + "[카테고리]\n"
        # C+1 확정 문구 (2026-07-30, 실사진 55장 프롬프트 A/B/C+1 실측 — 카테고리_프롬프트_실험_보고서 참조).
        # 바꾼 것: "가능한 한"(억지 유도) 제거, 신규 자격 규칙(주제 단위·브랜드 금지),
        # 기존 표기 재사용 강제(A/A' 파편화 0 실측), 다주제 tie-break, few-shot.
        # 문장 전체가 CATEGORY_POLICY 에서 렌더링된다 — 개편은 정책 dict 수정이 전부다.
        + _category_prompt_section(candidate_names)
        + tag_rule
        + "[주요정보] 사용자에게 보여줄 핵심 정보를 '항목: 값' 형태 문자열로 담아라. "
        "확인되지 않은 값은 넣지 마라(추측 금지).\n\n"
        + schedule_rule
        + language_rule
        + "반드시 아래 JSON만 출력. 마크다운·설명 금지.\n"
        + ('{"informative":true,"thumbnail_focus":0.5,"refined_text":"...",' if image_bytes is not None else "{")
        + '"title":"...","summary":"...","tags":["..."],"categories":["..."],'
        '"key_information":["..."],"analysis_confidence":0.0,'
        '"schedule":{"startAt":null,"endAt":null}}\n\n'
        f"OCR: {ocr_text}\n"
        + ("" if image_bytes is not None
           else f"이미지 분석: {image_analysis}")
    )
    parts: list[Any] = [prompt]
    if image_bytes is not None:
        parts.append(llm.image_part(image_bytes))
    return llm.generate_json(settings.llm_model_name, parts)


def _split_dt(value: str | None) -> dict[str, Any] | None:
    """ISO-8601 문자열(날짜 또는 날짜+시각)을 연·월·일 정수와 시각(HH:MM) 문자열로 분해한다.

    시각이 없는 날짜('2026-08-03')는 time 을 null 로 둔다(종일). 빈 값·파싱 불가면 None.
    타임존 표기(+09:00)는 무시하고 벽시계 시각 그대로 쓴다(계약이 KST 로컬).
    """
    s = (value or "").strip()
    if len(s) < 10:
        return None
    try:
        year, month, day = (int(x) for x in s[:10].split("-"))
    except ValueError:
        return None
    time = None
    if "T" in s:
        hm = s.split("T", 1)[1][:5]  # 'HH:MM'
        if len(hm) == 5 and hm[2] == ":" and hm[:2].isdigit() and hm[3:].isdigit():
            time = hm
    return {"year": year, "month": month, "day": day, "time": time}


def _build_schedule_data(raw: Any) -> dict[str, Any]:
    """AGENT 가 뽑은 일정(startAt·endAt ISO)을 콜백 scheduleData 계약 형태로 만든다.

    일정이 있으면 {"type":"schedule","fields":{startYear,startMonth,startDay,startTime,
    endYear,endMonth,endDay,endTime}}, 없으면 {"type": None, "fields": {}}.
    시각을 모르는 날짜는 startTime/endTime 이 null(종일).

    백엔드 계약: **시점이 하나뿐인 일정(예약·마감 등)은 end* 에 담는다.**
    start* 는 기간 일정의 시작에만 쓴다 — 단일 일정은 end만 보면 되도록 통일.
    """
    if not isinstance(raw, dict):
        return {"type": None, "fields": {}}
    start = _split_dt(raw.get("startAt"))
    end = _split_dt(raw.get("endAt"))
    if not start and not end:
        return {"type": None, "fields": {}}
    if start and not end:
        # 예약처럼 시작 시점만 뽑힌 경우 → 단일 시점 규칙에 따라 end 로 옮긴다.
        start, end = None, start
    fields: dict[str, Any] = {}
    for prefix, comp in (("start", start), ("end", end)):
        fields[prefix + "Year"] = comp["year"] if comp else None
        fields[prefix + "Month"] = comp["month"] if comp else None
        fields[prefix + "Day"] = comp["day"] if comp else None
        fields[prefix + "Time"] = comp["time"] if comp else None
    return {"type": "schedule", "fields": fields}


def build_candidates(
    user_id: int, candidates: list[CategoryCandidate]
) -> list[CategoryCandidate]:
    """판정에 쓸 카테고리 후보를 모은다. 세 소스를 합친다.

    - Spring 10-5 knowledge.categories — categoryId 를 가진 유일한 소스
    - 이 서버의 카테고리 벡터 저장소 — 사용자 centroid + 전역 시드. AGENT 가 새로
      만든 카테고리도 여기 남으므로 Spring 미연동 모드에서도, 재기동 뒤에도 유지된다
    - 둘 다 비면 DEFAULT_CATEGORIES (시드도 없는 최악의 경우. categoryId 없음)

    대표 벡터는 Spring 의 representativeVector 가 우선, 없으면 저장소 값을 채운다
    (저장소가 사용자 centroid > 전역 시드 순으로 이미 정리해 준다).
    """
    stored = category_store.load_category_vectors(user_id)
    merged = list(candidates)
    seen = {normalize_name(c.name) for c in merged}
    for key, entry in stored.items():
        if key not in seen:
            seen.add(key)
            # 사용자에게 이미 쌓인 카테고리(count>0)만 id 를 달아 준다. id 가 붙으면
            # resolve_category 가 created=False 로 판정하고(category.py), Spring 은
            # "이미 있는 카테고리" 로 받는다. 전역 시드는 이 사용자가 한 번도 쓴 적
            # 없는 기본 이름이라 신규로 남겨야 한다 — 벡터를 안 붙이는 기준과 같다.
            merged.append(CategoryCandidate(
                category_id=entry["category_id"] if entry.get("count", 0) > 0 else None,
                name=entry["name"],
            ))
    if not merged:
        merged = [CategoryCandidate(category_id=None, name=name)
                  for name in DEFAULT_CATEGORIES]
    for candidate in merged:
        if not candidate.representative_vector:
            entry = stored.get(normalize_name(candidate.name))
            # 전역 시드(count=0, 이름 임베딩)는 벡터 판정에서 제외한다 — 실측
            # (2026-07-30, 46장)에서 이름↔요약 코사인은 분리력이 노이즈 수준
            # (자기 시드 중앙값 0.52 < 남의 시드 0.54, 최적점에서도 오병합 2/3).
            # 시드의 "이름"은 위에서 후보 목록에 그대로 남으므로 이름 일치 경로는
            # 유지된다. 시드 문서 자체의 존치 여부는 팀장과 협의(저장소 설계 소관).
            if entry and entry.get("count", 0) > 0:
                candidate.representative_vector = entry["vector"]
    return merged


def _existing_tag_names(
    user_id: int, knowledge: KnowledgeCandidates, limit: int = 30
) -> list[str]:
    """태그 생성의 '기준점'. 사용자에게 이미 쌓인 태그를 모아 AGENT 가 재사용하게 한다.

    카테고리에 기존 후보를 제시하는 것과 대응된다. 두 소스를 합친다:
    - OpenSearch 태그 집계(자기 인덱스라 Spring 없이도 동작) — 사용 빈도 순
    - Spring 10-5 knowledge.tags (복귀 시)
    없으면 빈 리스트(콜드스타트 → 기존처럼 자유 생성).
    """
    names: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        n = (name or "").strip()
        key = n.lower()
        if n and key not in seen:
            seen.add(key)
            names.append(n)

    # best-effort — 집계가 실패해도 태그 생성 자체는 막지 않는다.
    try:
        for row in search.aggregate_tags(user_id, limit=limit):
            add(row.get("name", ""))
    except Exception as e:
        logger.warning("기존 태그 집계 실패(무시하고 진행): %s", e)
    for tag in knowledge.tags:
        add(tag.name)

    return names[:limit]


async def run_agent(request: AnalyzeRequest) -> dict[str, Any]:
    ocr = request.input.ocr
    image_analysis = request.input.image_analysis
    ocr_text = (ocr.refined_text or ocr.raw_text) if ocr else ""
    analysis_dict = image_analysis.model_dump(by_alias=True) if image_analysis else {}
    options = request.options
    return await run_agent_core(
        user_id=request.user_id,
        image_id=request.image_id,
        ocr_text=ocr_text,
        analysis_dict=analysis_dict,
        max_tags=options.max_tags if options else None,
        language=options.language if options else None,
    )


async def run_agent_core(
    user_id: int,
    image_id: int,
    ocr_text: str,
    analysis_dict: dict[str, Any],
    max_tags: int | None = None,
    language: str | None = None,
    image_bytes: bytes | None = None,
) -> dict[str, Any]:
    """AGENT 본체. Spring 연동 경로와 앱 직결 경로가 함께 쓴다."""
    settings = get_settings()

    # 10-5 기존 태그·카테고리 후보 조회 (실패하면 기본 후보로 진행)
    knowledge = await spring_client.fetch_knowledge_candidates(user_id)
    # 저장된 카테고리 벡터를 읽어 합친다(OpenSearch 조회라 스레드로 돌린다).
    candidates = await asyncio.to_thread(
        build_candidates, user_id, knowledge.categories
    )
    # 태그 난립 방지용 기준점(기존 태그). OpenSearch 집계라 스레드로 돌린다.
    existing_tags = await asyncio.to_thread(_existing_tag_names, user_id, knowledge)

    max_tags = max_tags or settings.default_max_tags

    generated = await asyncio.to_thread(
        run_agent_generation,
        ocr_text, analysis_dict, [c.name for c in candidates], max_tags, language,
        existing_tags, image_bytes,
    )

    if image_bytes is not None and not generated.get("informative", True):
        # 융합 경로의 비정보성 판정 — 임베딩·카테고리 판정·저장을 모두 생략한다.
        return {"informative": False, "reason": str(generated.get("reason", ""))}

    summary = generated.get("summary", "") or generated.get("title", "")
    proposed = (generated.get("categories") or ["기타"])[0]

    # 요약 임베딩: 카테고리 판정에 쓰고, 검색 적재용으로 콜백에도 실어 보낸다.
    # AGENT 가 후보에 없는 새 이름을 제안한 경우(희귀 경로)에는 이름 중복 관문용
    # 이름 임베딩(제안 + 후보 전부)을 같은 배치에 실어 보낸다 — 추가 호출 0회.
    proposed_key = normalize_name(proposed)
    name_matched = any(normalize_name(c.name) == proposed_key for c in candidates)
    name_slots: list[str] = []
    if not name_matched and candidates:
        name_slots = [proposed] + [c.name for c in candidates]
    embedding_model, vectors = await asyncio.to_thread(
        gemini_client.embed, [summary] + name_slots, "DOCUMENT"
    )
    name_vectors = {
        normalize_name(name): vec
        for name, vec in zip(name_slots, vectors[1:])
    }
    # 순수 계산이라 스레드로 안 넘긴다(외부 호출이 없다 — 벡터는 위에서 준비했다).
    resolution: CategoryResolution = resolve_category(
        vectors[0], proposed, candidates,
        settings.category_name_dup_threshold,
        name_vectors=name_vectors,
        proposed_name_vector=name_vectors.get(proposed_key),
    )
    logger.info(
        "카테고리 판정 imageId=%s 제안=%s → %s (유사도=%.3f, 기준=%s, 신규=%s)",
        image_id, proposed, resolution.name,
        resolution.similarity, resolution.matched_by, resolution.created,
    )

    # 판정된 카테고리의 대표 벡터를 이 요약 임베딩으로 갱신한다(없으면 생성).
    # 이 저장이 있어야 카테고리와 그 벡터가 서버 재기동 뒤에도 남고, 이미지가
    # 쌓일수록 centroid 가 정확해진다.
    #
    # 오염 가드: 이름 일치인데 그 카테고리 centroid 와 감사 코사인이 명백히
    # 낮으면 누적하지 않는다. 수동 수정이 없는 제품이라 오분류가 centroid 에
    # 들어가면 자가 교정 경로가 없다(동의서→금융 오염 실측, 2026-07-30).
    # 첫 이미지(centroid 부재 → 감사 코사인 1.0)는 이 가드로 못 막는다.
    guard = settings.category_guard_min_cosine
    if (guard > 0 and resolution.matched_by in ("name", "name-variant", "name-dup")
            and resolution.similarity < guard):
        logger.warning(
            "카테고리 오염 가드: '%s' 이름 일치지만 감사 코사인 %.3f < %.2f — centroid 누적 생략",
            resolution.name, resolution.similarity, guard,
        )
    else:
        await asyncio.to_thread(
            category_store.upsert_category_vector, user_id, resolution.name, vectors[0]
        )

    tags = [str(t) for t in (generated.get("tags") or [])][:max_tags]
    return {
        "title": generated.get("title", ""),
        "summary": summary,
        "tags": tags,
        # 카테고리는 항상 하나다(프롬프트가 하나만 고르게 하고 resolve_category 도
        # 하나를 돌려준다). 배열로 감싸면 Spring 이 없는 다중 카테고리를 처리해야 한다.
        "category": resolution.name,
        # Spring 이 준 categoryId 가 있으면 그것, 없으면 이름 해시로 채운다.
        # null 을 내보내면 Spring 이 이름으로 재조회해야 하는데, Spring 미연동
        # 기간에는 후보에 categoryId 가 아예 없어(build_candidates) 늘 null 이 된다.
        # reanalyze 는 이미 같은 폴백을 쓴다 — 두 경로가 같은 이름에 같은 id 를 준다.
        "categoryId": resolution.category_id or search.stable_id(resolution.name),
        "keyInformation": generated.get("key_information") or [],
        "analysisConfidence": float(generated.get("analysis_confidence", 0.0)),
        # 캘린더용 일정. {type:"schedule", fields:{start*/end* 연·월·일·시각(HH:MM)}} 또는
        # 일정 없으면 {type:null, fields:{}}. 백엔드가 DB 저장 후 앱 캘린더로 서빙한다.
        "scheduleData": _build_schedule_data(generated.get("schedule")),
        # 신규 카테고리 여부(스프링이 생성 판단에 사용). 계약 확정 필요.
        "categoryCreated": resolution.created,
        # 명세 6-2 categories[].confidence 로 내려줄 값
        "categoryConfidence": round(resolution.similarity, 3),
        # 검색용 문서 임베딩. Spring 이 pgvector 등에 그대로 적재한다.
        # (요약 텍스트 기준. purpose=DOCUMENT 로 생성)
        "documentVector": vectors[0],
        "embeddingModel": embedding_model,
        "embeddingDimension": len(vectors[0]),
        # 앱 상세화면 "추출된 텍스트"용. 융합 경로에서 모델이 이미지를 보고 교정한
        # 화면 텍스트다(요약 아님). rawText 는 오인식이 그대로라 폴백으로 쓰지 않는다.
        "ocrRefinedText": (generated.get("refined_text") or "").strip() or None,
        # 융합 모드에서 모델이 짚은 시각적 핵심의 세로 위치(0~1). 썸네일 크롭용
        # 내부 값 — 파이프라인이 pop 해서 쓰고 콜백에는 싣지 않는다.
        "thumbnailFocus": generated.get("thumbnail_focus"),
    }


async def _index_for_search(request: AnalyzeRequest, result: dict[str, Any]) -> None:
    """AGENT 결과를 OpenSearch 에 색인한다(검색 전담).

    색인 실패가 분석 자체를 실패시키지 않도록 best-effort 로 처리한다.
    (OpenSearch 가 잠시 죽어도 분석·콜백은 정상 진행)
    """
    ocr = request.input.ocr
    raw_text = (ocr.refined_text or ocr.raw_text) if ocr else ""
    try:
        await asyncio.to_thread(
            search.index_document,
            image_id=request.image_id,
            user_id=request.user_id,
            title=result.get("title", ""),
            summary=result.get("summary", ""),
            tags=result.get("tags", []),
            category_name=result.get("category"),
            raw_text=raw_text,
            created_at=now_iso(),
        )
    except Exception as e:
        logger.warning("검색 색인 실패 imageId=%s: %s (분석은 정상 진행)",
                       request.image_id, e)


# ── 앱 직결 전체 분석 ─────────────────────────────────────────────────────
# OCR 이 비었거나 정보성이 없다고 판정된 이미지에 붙는 시스템 카테고리.
OTHER_CATEGORY = "기타"


async def _index_as_other(
    job_id: int, image_id: int, user_id: int, s3_key: str, ocr_text: str
) -> None:
    """분석을 생략하고 '기타' 로만 분류해 색인한다(명세 10-4 처리 규칙 3·4).

    AGENT 를 돌리지 않으므로 제목·태그는 비어 있지만, 이미지 자체는 목록과
    검색에 남는다. 상태는 EMPTY 로 두어 앱이 '분석 생략'임을 구분할 수 있게 한다.
    """
    job_store.update(job_id, "PROCESSING", stage="INDEXING")
    result = {
        "title": "",
        "summary": "",
        "tags": [],
        "category": OTHER_CATEGORY,
        "key_information": [],
        "confidence": 0.0,
        "s3_key": s3_key,
    }
    try:
        await asyncio.to_thread(
            search.index_document,
            image_id=image_id,
            user_id=user_id,
            title="",
            summary="",
            tags=[],
            category_name=OTHER_CATEGORY,
            raw_text=ocr_text,
            created_at=now_iso(),
            s3_key=s3_key,
            key_information=[],
            # 명세 1.4: OCR 이 비었거나 비정보성이면 LLM 단계 결과가 EMPTY 다.
            status="EMPTY",
        )
    except Exception as e:
        logger.warning("검색 색인 실패 imageId=%s: %s", image_id, e)

    job_store.update(job_id, "EMPTY", result=result, stage="INDEXING")


def _empty_callback_result(thumbnail_key: str | None = None) -> dict[str, Any]:
    """분석을 건너뛴(EMPTY) 경우의 콜백 result.

    Spring 의 AnalysisCallbackService 는 EMPTY 도 성공으로 취급해 결과 행을 남기므로
    (`case "COMPLETED", "EMPTY" -> handleSuccess`), **키 구조를 COMPLETED 와 완전히
    동일하게** 맞추고 값만 비운다(2026-07-29 백엔드 요청 — 만들지 않은 값은 null).
    informative·reason 은 콜백에서 제외(백엔드 미사용 확정) — ocrRefinedText 는
    2026-07-31 앱 '추출된 텍스트' 표시용으로 다시 넣었다(융합 경로에서만 값이 찬다).
    판정 사유(reason)는 서버 로그에 남는다(비정보성 판정 → '기타' 분류 INFO 로그).
    """
    return {
        "title": "",
        "summary": "",
        "tags": [],
        "category": OTHER_CATEGORY,
        # COMPLETED 경로가 '기타' 로 판정했을 때와 같은 id 여야 한다. null 로 두면
        # 같은 카테고리명이 status 에 따라 id 가 있기도 없기도 하다.
        "categoryId": search.stable_id(OTHER_CATEGORY),
        "keyInformation": [],
        "analysisConfidence": 0.0,
        "scheduleData": {"type": None, "fields": {}},
        # '기타' 는 기본 카테고리라 신규 생성 판단이 필요 없다.
        "categoryCreated": False,
        "categoryConfidence": 0.0,
        # 요약이 없어 임베딩을 만들지 않는다 — 구조만 유지하고 null.
        "documentVector": None,
        "embeddingModel": None,
        "embeddingDimension": None,
        "ocrRefinedText": None,
        "thumbnailKey": thumbnail_key,
    }


def _text_from_image_analysis(analysis: dict[str, Any]) -> str:
    """이미지 분석 결과에서 OCR 대용 텍스트를 만든다.

    analysis-worker 의 FULL 요청에는 OCR 이 실려 오지 않는다(FastApiAnalysisClient 가
    input 에 image 만 담는다). 그대로 두면 정보성 판정 입력이 항상 비어 EMPTY 로
    떨어지므로, 비전 모델이 화면에서 읽어낸 텍스트를 대신 쓴다.
    """
    parts = [analysis.get("description") or ""]
    parts += [str(t) for t in (analysis.get("detectedTexts") or [])]
    return "\n".join(p for p in parts if p.strip()).strip()


async def run_app_analysis(
    job_id: int, image_id: int, user_id: int, s3_key: str, ocr_text: str
) -> None:
    """앱 직결(4-2 업로드 완료) 진입점.

    결과는 콜백 대신 job_store 에 담기고 앱이 폴링으로 가져간다. 동시 실행 제한은
    여기서 잡는다 — 본체(_run_full_pipeline)는 잡지 않는다.
    """
    async with _get_semaphore():
        await _run_full_pipeline(job_id, image_id, user_id, s3_key, ocr_text)


# 재큐잉 태스크 참조 보관용. asyncio 는 실행 중인 태스크를 약참조로만 들고 있어서
# 여기에 담아 두지 않으면 도중에 GC 되어 분석이 조용히 사라질 수 있다.
_requeued: set[asyncio.Task] = set()

# 한 번에 되살릴 최대 건수. 넘치면 다음 기동에서 이어 받는다.
REQUEUE_LIMIT = 500


async def requeue_interrupted() -> None:
    """재기동으로 끊긴 분석을 다시 큐에 넣는다(기동 시 1회, best-effort).

    job_store 는 프로세스 메모리라 재시작하면 사라지는데 색인 문서는
    status=PROCESSING 으로 남는다. 이 서버에 재분석 트리거가 따로 없어서
    (4-2 upload-complete 재호출이 유일한데, 앱은 '진행 중' 으로만 보여 다시 부를
    이유를 모른다) 그대로 두면 그 사진은 영원히 분석 중이다. 재업로드로도 못 푼다
    — uploaded_at 이 이미 찍혀 있어 4-1 중복 판정에 걸린다(search.find_by_content_hash).

    QUEUED 는 건드리지 않는다. 4-1 등록만 되고 스토리지 업로드가 아직 안 끝난
    상태일 수 있어서, 여기서 분석을 시작하면 원본이 없는 채로 실패시켜 버린다.

    ⚠️ 단일 프로세스 전제. uvicorn 워커를 늘리면 워커마다 같은 문서를 재큐잉해
    같은 이미지를 중복 분석한다 — jobs.py 가 말하는 Redis 이전 시점에 함께 볼 것.
    """
    try:
        stuck = await asyncio.to_thread(
            search.find_by_status, "PROCESSING", REQUEUE_LIMIT
        )
    except Exception as e:
        logger.warning("끊긴 분석 조회 실패: %s — 재큐잉을 건너뛴다", e)
        return
    if not stuck:
        return
    if len(stuck) >= REQUEUE_LIMIT:
        logger.warning("끊긴 분석이 상한 %s건에 걸렸다 — 나머지는 다음 기동에서 이어 받는다",
                       REQUEUE_LIMIT)

    requeued = 0
    for doc in stuck:
        image_id, s3_key = doc.get("image_id"), doc.get("s3_key")
        if not image_id or not s3_key:
            # 원본 key 가 없으면 되살릴 수 없다. PROCESSING 인데 s3_key 가 비는
            # 경로는 없지만, 남겨 두면 영원히 재시도되므로 실패로 끊는다.
            logger.warning("재큐잉 불가(원본 key 없음) imageId=%s", image_id)
            if image_id:
                try:
                    await asyncio.to_thread(search.set_status, image_id, "FAILED")
                except Exception as e:
                    logger.warning("status 갱신 실패 imageId=%s: %s", image_id, e)
            continue
        user_id = doc.get("user_id") or 0
        job = job_store.create(user_id, s3_key, image_id)
        task = asyncio.create_task(run_app_analysis(
            job["job_id"], image_id, user_id, s3_key, doc.get("raw_text") or "",
        ))
        _requeued.add(task)
        task.add_done_callback(_requeued.discard)
        requeued += 1

    logger.warning("재기동으로 끊긴 분석 %s건 재큐잉 (동시 %s건씩 순차 소화)",
                   requeued, get_settings().max_concurrent_stages)


async def _run_full_pipeline(
    job_id: int,
    image_id: int,
    user_id: int,
    s3_key: str,
    ocr_text: str,
    max_tags: int | None = None,
    language: str | None = None,
    allow_vision_ocr: bool = False,
) -> tuple[str, dict[str, Any] | None, CallbackError | None]:
    """정보성 판정 → 이미지 분석 → AGENT → 색인을 한 번에 수행한다.

    앱 직결 경로(run_app_analysis)와 Spring 연동의 FULL stage 가 함께 쓴다.
    `(status, result, error)` 를 돌려주며, status 는 COMPLETED / EMPTY / FAILED 다.
    result 는 명세 10-4 콜백 result 형태(camelCase)라 그대로 실어 보내면 된다.

    **세마포어를 잡지 않는다.** 호출자(run_app_analysis / execute_stage)가 이미
    잡고 있으며, 여기서 또 잡으면 재진입 데드락이 난다.

    allow_vision_ocr=True 면 OCR 텍스트가 없을 때 이미지 분석을 먼저 돌려 거기서
    읽어낸 텍스트로 진행한다(OCR 없이 오는 FULL 요청용). 앱 직결 경로는 온디바이스
    OCR 이 항상 함께 오므로 기본값 False 로 두어 기존 동작을 유지한다.
    """
    timings: dict[str, float] = {}
    started = time.perf_counter()
    job_store.update(job_id, "PROCESSING", stage="LLM")
    # 색인 문서의 status 도 함께 올려 6-1 목록에서 진행 중임이 보이게 한다.
    try:
        await asyncio.to_thread(search.set_status, image_id, "PROCESSING")
    except Exception as e:
        logger.warning("status 갱신 실패 imageId=%s: %s", image_id, e)
    logger.info("전체 분석 시작 jobId=%s imageId=%s", job_id, image_id)
    try:
        # 0) 썸네일 생성 후 썸네일 버킷에 저장.
        #    분석을 건너뛰는 이미지(빈 OCR·비정보성)도 목록에는 나오므로
        #    정보성 판정보다 먼저 만든다. 여기서 읽은 원본은 이미지 분석에서 재사용한다.
        #    실패해도 분석은 계속한다(썸네일 조회 시 즉석 생성으로 메꿔진다).
        original: bytes | None = None
        # 썸네일은 원본과 같은 key 로 썸네일 버킷에 저장된다(관례). 성공 시 그 key 를
        # 콜백 thumbnailKey 로 알려 Spring 이 thumbnail 테이블(image_id, s3_key)을
        # 채울 수 있게 한다. 실패하면 null — Spring 은 썸네일 없음으로 처리한다.
        thumbnail_key: str | None = None
        try:
            with _timed(timings, "THUMBNAIL"):
                original = await asyncio.to_thread(storage.fetch_image, s3_key)
                await asyncio.to_thread(storage.store_thumbnail, s3_key, original)
            thumbnail_key = s3_key
        except Exception as e:
            logger.warning("썸네일 저장 실패 imageId=%s: %s (분석은 계속)", image_id, e)

        # 1) 저장할 가치가 있는 정보인지 판정한다.
        #    명세 10-4 처리 규칙: OCR 이 비었거나(EMPTY) 정보성이 없으면
        #    IMAGE_ANALYSIS·AGENT 를 생략하고 '기타' 로 분류한 뒤 색인한다.
        #    분석을 건너뛸 뿐 사용자 이미지는 목록·검색에서 사라지지 않는다.
        analysis: dict[str, Any] | None = None
        if not ocr_text.strip() and allow_vision_ocr and original is None:
            # OCR 이 없고 **융합도 못 하는** 경우(원본 확보 실패)에만 이미지 분석을
            # 앞당겨 그 묘사를 텍스트로 대신한다.
            #
            # 융합이 가능하면 이 호출을 하지 않는다: 융합 호출이 이미지를 직접 보므로
            # 묘사를 미리 만들 필요가 없고, 워커가 FULL 요청에 OCR 을 안 실어 보내므로
            # (FastApiAnalysisClient 가 image 만 담는다) 그대로 두면 **모든 이미지가
            # 비전 1콜을 추가로 태워** 융합의 3콜→1콜 이점이 절반 사라진다(2026-08-03 실측).
            # 부작용으로 '글자가 전혀 없는 사진'은 아래 게이트에서 EMPTY 가 되는데,
            # 스크린샷 지식 DB 라는 제품 성격상 그게 맞는 판정이다(카메라로 찍은 밥상
            # 사진이 '음식' 으로 분석되던 것을 오너가 기타로 교정, 2026-08-03).
            job_store.update(job_id, "PROCESSING", stage="IMAGE_ANALYSIS")
            try:
                with _timed(timings, "IMAGE_ANALYSIS"):
                    analysis = await asyncio.to_thread(
                        run_image_analysis, s3_key, original
                    )
                ocr_text = _text_from_image_analysis(analysis)
            except Exception as e:
                logger.warning("이미지 분석 실패 imageId=%s: %s — OCR 대체 불가",
                               image_id, e)
                analysis = {}

        if not ocr_text.strip():
            logger.info("읽어낼 텍스트 없음 → '기타' 분류 후 색인 jobId=%s", job_id)
            with _timed(timings, "INDEXING"):
                await _index_as_other(job_id, image_id, user_id, s3_key, ocr_text)
            timings["TOTAL"] = round(time.perf_counter() - started, 2)
            logger.info("전체 분석 완료(EMPTY) jobId=%s 소요=%s", job_id, timings)
            return "EMPTY", _empty_callback_result(thumbnail_key), None

        if original is not None:
            # 1·2·3 융합) 정보성 판정·시각 이해·메타데이터 생성을 한 호출로.
            #    모델이 화면 이미지를 직접 본다 — 별도 비전 요약을 거치면 기프티콘
            #    바코드·문서 서식 같은 시각 신호가 한 줄로 압축되며 증발한다
            #    (실사진 40장 파일럿: 요약 경유 45% vs 직접 투입 70%).
            #    호출 3회 → 1회라 비용·지연도 준다. 이미지는 image_part 가
            #    프로바이더 한도 안으로 자동 축소한다 — gemini 는 GMS 본문
            #    한도(~90KB)에 맞춰 40KB, claude 는 모델 입력 해상도 계층에
            #    맞춰 면적 기준으로 줄인다(app/llm/claude.py 참고).
            job_store.update(job_id, "PROCESSING", stage="AGENT")
            with _timed(timings, "AGENT"):
                generated = await run_agent_core(
                    user_id=user_id,
                    image_id=image_id,
                    ocr_text=ocr_text,
                    analysis_dict={},
                    max_tags=max_tags,
                    language=language,
                    image_bytes=original,
                )
            if not generated.get("informative", True):
                logger.info("비정보성 판정(융합) → '기타' 분류 후 색인 jobId=%s 사유=%s",
                            job_id, generated.get("reason", ""))
                with _timed(timings, "INDEXING"):
                    await _index_as_other(job_id, image_id, user_id, s3_key, ocr_text)
                timings["TOTAL"] = round(time.perf_counter() - started, 2)
                logger.info("전체 분석 완료(비정보성) jobId=%s 소요=%s", job_id, timings)
                return "EMPTY", _empty_callback_result(thumbnail_key), None
        else:
            # 원본을 못 읽었다(스토리지 장애 등) — 기존 3단계 경로로 폴백.
            with _timed(timings, "LLM"):
                _, verdict = await asyncio.to_thread(run_llm, ocr_text)
            if not verdict.get("informative", False):
                reason = verdict.get("reason", "")
                logger.info("비정보성 판정 → '기타' 분류 후 색인 jobId=%s 사유=%s",
                            job_id, reason)
                with _timed(timings, "INDEXING"):
                    await _index_as_other(job_id, image_id, user_id, s3_key, ocr_text)
                timings["TOTAL"] = round(time.perf_counter() - started, 2)
                logger.info("전체 분석 완료(비정보성) jobId=%s 소요=%s", job_id, timings)
                return "EMPTY", _empty_callback_result(thumbnail_key), None

            # 2) 이미지 분석. 실패해도 OCR 만으로 계속 진행한다.
            #    위에서 이미 돌렸으면(OCR 대체) 다시 부르지 않는다.
            if analysis is None:
                job_store.update(job_id, "PROCESSING", stage="IMAGE_ANALYSIS")
                try:
                    with _timed(timings, "IMAGE_ANALYSIS"):
                        analysis = await asyncio.to_thread(
                            run_image_analysis, s3_key, original
                        )
                except Exception as e:
                    logger.warning("이미지 분석 실패 imageId=%s: %s — OCR 만으로 진행",
                                   image_id, e)
                    analysis = {}

            # 3) 메타데이터 생성 + 카테고리 판정
            job_store.update(job_id, "PROCESSING", stage="AGENT")
            with _timed(timings, "AGENT"):
                generated = await run_agent_core(
                    user_id=user_id,
                    image_id=image_id,
                    ocr_text=ocr_text,
                    analysis_dict=analysis,
                    max_tags=max_tags,
                    language=language,
                )
        # 모델이 짚어준 시각적 핵심 위치로 썸네일을 다시 만든다(휴리스틱 크롭 대체).
        # 콜백 계약에 새 키가 새지 않도록 여기서 pop 한다. 실패해도 0단계에서
        # 만든 휴리스틱 썸네일이 남아 있으므로 조용히 넘어간다.
        focus = generated.pop("thumbnailFocus", None)
        if focus is not None and original is not None and thumbnail_key is not None:
            try:
                await asyncio.to_thread(
                    storage.store_thumbnail, s3_key, original, float(focus))
            except Exception as e:
                logger.warning("포커스 썸네일 재생성 실패 imageId=%s: %s", image_id, e)

        category = generated.get("category")
        result = {
            "title": generated.get("title", ""),
            "summary": generated.get("summary", ""),
            "tags": generated.get("tags", []),
            "category": category,
            "key_information": generated.get("keyInformation") or [],
            "confidence": float(generated.get("analysisConfidence", 0.0)),
            "s3_key": s3_key,
        }

        # 4) 검색 색인. 실패해도 분석 결과 자체는 살린다.
        job_store.update(job_id, "PROCESSING", stage="INDEXING")
        try:
            with _timed(timings, "INDEXING"):
                await asyncio.to_thread(
                    search.index_document,
                    image_id=image_id,
                    user_id=user_id,
                    title=result["title"],
                    summary=result["summary"],
                    tags=result["tags"],
                    category_name=category,
                    raw_text=ocr_text,
                    created_at=now_iso(),
                    s3_key=s3_key,
                    key_information=result["key_information"],
                    status="COMPLETED",
                    category_confidence=generated.get("categoryConfidence"),
                )
        except Exception as e:
            logger.warning("검색 색인 실패 imageId=%s: %s (분석 결과는 유지)",
                           image_id, e)

        job_store.update(job_id, "COMPLETED", result=result, stage="INDEXING")
        timings["TOTAL"] = round(time.perf_counter() - started, 2)
        logger.info("전체 분석 완료 jobId=%s imageId=%s category=%s 소요=%s",
                    job_id, image_id, category, timings)

        # 콜백 result. Spring 의 AnalysisCallbackService 가 읽는 키를 이 이름으로 담는다.
        # informative 는 싣지 않는다(2026-07-29 백엔드 합의) —
        # 정보성 여부는 status(COMPLETED/EMPTY)로 구분되고, OCR 원문은 앱이 4-1 로
        # 보낸 것을 Spring 이 자체 보관(image_schema.ocr.content)한다.
        callback_result = dict(generated)
        callback_result["thumbnailKey"] = thumbnail_key
        return "COMPLETED", callback_result, None
    except Exception as e:
        timings["TOTAL"] = round(time.perf_counter() - started, 2)
        logger.exception("전체 분석 실패 jobId=%s 소요=%s", job_id, timings)
        try:
            await asyncio.to_thread(search.set_status, image_id, "FAILED")
        except Exception:
            pass
        job_store.update(
            job_id,
            "FAILED",
            error={"code": "ANALYSIS_FAILED",
                   "message": str(e)[:500],
                   "retryable": True},
        )
        return "FAILED", None, CallbackError(
            code="ANALYSIS_FAILED", message=str(e)[:500], retryable=True
        )


# ── 단계 실행 + 콜백 ──────────────────────────────────────────────────────
async def execute_stage(request: AnalyzeRequest) -> None:
    # 동시 실행 제한: 수백 장이 한꺼번에 들어와도 세마포어가 꽉 차면 여기서 대기한다.
    # 초과분은 버려지지 않고 자연스럽게 큐잉되므로 500장+ 도 순차적으로 소화된다.
    async with _get_semaphore():
        await _execute_stage(request)


async def _execute_stage(request: AnalyzeRequest) -> None:
    settings = get_settings()
    stage = request.stage
    status = "FAILED"
    result: dict[str, Any] | None = None
    error: CallbackError | None = None
    model_version = settings.llm_model_name

    logger.info("단계 시작 jobId=%s imageId=%s stage=%s",
                request.job_id, request.image_id, stage)
    try:
        if stage == "LLM":
            ocr = request.input.ocr
            status, result = await asyncio.to_thread(
                run_llm, (ocr.raw_text if ocr else "")
            )
        elif stage == "IMAGE_ANALYSIS":
            model_version = settings.vision_model_name
            result = await asyncio.to_thread(
                run_image_analysis, request.input.image.s3_key  # type: ignore[union-attr]
            )
            status = "COMPLETED"
        elif stage == "FULL":
            # 세마포어는 execute_stage 가 이미 잡고 있다. 본체를 직접 부른다.
            ocr = request.input.ocr
            ocr_text = ((ocr.refined_text or ocr.raw_text) if ocr else "") or ""
            options = request.options
            status, result, error = await _run_full_pipeline(
                job_id=request.job_id,
                image_id=request.image_id,
                user_id=request.user_id,
                s3_key=request.input.image.s3_key,  # type: ignore[union-attr]
                ocr_text=ocr_text,
                max_tags=options.max_tags if options else None,
                language=options.language if options else None,
                allow_vision_ocr=True,
            )
            # 색인은 _run_full_pipeline 안에서 이미 끝냈다(_index_for_search 중복 호출 금지).
        else:  # AGENT
            result = await run_agent(request)
            status = "COMPLETED"
            await _index_for_search(request, result)
    except Exception as e:
        logger.exception("단계 실패 jobId=%s stage=%s", request.job_id, stage)
        status, result = "FAILED", None
        error = CallbackError(
            code=f"{stage}_FAILED",
            message=str(e)[:500],
            retryable=True,
        )

    job_registry.mark(request.job_id, stage, status)
    logger.info("단계 종료 jobId=%s imageId=%s stage=%s status=%s",
                request.job_id, request.image_id, stage, status)

    payload = CallbackRequest(
        job_id=request.job_id,
        image_id=request.image_id,
        stage=stage,
        status=status,  # type: ignore[arg-type]
        result=result,
        error=error,
        model_version=model_version,
        completed_at=now_iso(),
    )
    await spring_client.post_callback(payload, request.callback_url)
