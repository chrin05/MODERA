"""BM25 질의 변환(무변별어 스트립 + 표기 별칭) 자가 점검. OpenSearch 없이 돈다.

조용히 틀어지면 검색이 통째로 빈 결과가 되는 자리라 확인해 둔다 — 별칭이 안
걸리는 질의까지 변형이 늘면 dis_max 가 붙어 점수 스케일이 바뀌고, 반대로 별칭이
안 먹으면 "싸피" 질의가 SSAFY 문서를 못 잡는다(원래 증상).

    python scripts/query_variants_check.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("INTERNAL_TOKEN", "check")
os.environ.setdefault("GMS_KEY", "check")
os.environ.setdefault("GEMINI_API_KEY", "check")
os.environ.setdefault("GCP_PROJECT", "check")

from app import search                                # noqa: E402


def main() -> None:
    variants = search._query_variants

    # 별칭이 걸리는 질의: 스트립된 원문 + 치환본, 이 순서.
    # 치환은 조사 꼬리를 함께 먹는다("싸피랑"→"SSAFY") — 꼬리를 남기면 nori 가
    # ssafy·랑 두 토큰으로 끊어 msm 75%=1개가 되고, '랑' 단독 매칭 정크가 샌다.
    assert variants("싸피랑 관련된 사진") == ["싸피랑", "SSAFY"]
    assert variants("싸피") == ["싸피", "SSAFY"]
    assert variants("싸피에서 찍은 단체사진") == ["싸피에서 찍은 단체사진",
                                                  "SSAFY 찍은 단체사진"]
    # 반대 방향(영문 → 한글)도 같은 쌍으로 동작한다. 대소문자는 무시한다.
    assert variants("SSAFY 지원 자격") == ["SSAFY 지원 자격", "싸피 지원 자격"]
    assert variants("ssafy 커리큘럼") == ["ssafy 커리큘럼", "싸피 커리큘럼"]

    # 별칭과 무관한 질의는 변형이 1개 — 호출부가 dis_max 를 안 씌우고 기존 경로 그대로.
    assert variants("쿠팡 로켓배송") == ["쿠팡 로켓배송"]
    # 무변별어 스트립은 그대로 살아 있다.
    assert variants("쿠팡 관련된 사진 보여줘") == ["쿠팡"]
    # 전부 무변별어면 원문을 유지한다(빈 BM25 질의 금지).
    assert variants("사진 보여줘") == ["사진 보여줘"]

    # 별칭을 끄면(운영에서 env 로 비울 수 있다) 스트립만 남는다.
    search.get_settings.cache_clear()
    os.environ["SEARCH_QUERY_ALIASES"] = ""
    try:
        assert search._query_variants("싸피랑 관련된 사진") == ["싸피랑"]
    finally:
        del os.environ["SEARCH_QUERY_ALIASES"]
        search.get_settings.cache_clear()

    print("query variants check: OK")


if __name__ == "__main__":
    main()
