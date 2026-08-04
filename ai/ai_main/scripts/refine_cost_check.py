"""OCR 교정 비용 판단용 실측. 실제 OpenSearch 에 붙어 세 숫자만 센다.

    python scripts/refine_cost_check.py

왜 이 숫자가 필요한가. 융합 호출은 이미지 한 장마다 OCR 교정을 만든다(출력
토큰의 큰 덩어리). 그런데 그 교정본은 6-2 상세화면에서만 쓰인다. 그래서
선택지가 둘이다:

  A. 지금처럼 분석 때 전량 교정  — 열람률이 높으면 이게 싸다(융합 호출에
     얹혀 가므로 이미지·프롬프트 입력을 재전송하지 않는다)
  B. 상세 첫 조회 때 온디맨드 교정 + refined_text 캐시 — 열람률이 낮으면 이게 싸다
     (단 독립 호출이라 이미지 ~500토큰 + OCR + 지시문을 다시 보낸다)

손익분기는 열람률 대략 35~45% 다. 그 위면 A, 아래면 B. 추측하지 말고 잰다.
`last_viewed_at` 은 6-2 조회 성공 시에만 갱신되므로(명세 8-1) 그대로 열람 여부다.

읽기만 한다 — 문서를 고치지 않는다.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("INTERNAL_TOKEN", "check")
os.environ.setdefault("GEMINI_API_KEY", "check")

from app import search  # noqa: E402


def count(**query) -> int:
    index = search.get_settings().opensearch_index
    return search._client().count(index=index, body={"query": query})["count"]


def main() -> None:
    analyzed = count(term={"status": "COMPLETED"})
    if not analyzed:
        print("분석 완료 문서가 0건 — 열람률을 잴 표본이 없다.")
        return
    viewed = count(bool={"filter": [{"term": {"status": "COMPLETED"}},
                                    {"exists": {"field": "last_viewed_at"}}]})
    refined = count(bool={"filter": [{"term": {"status": "COMPLETED"}},
                                     {"exists": {"field": "refined_text"}}]})
    rate = viewed / analyzed

    print(f"분석 완료      {analyzed:>7,}건")
    print(f"상세 열람됨    {viewed:>7,}건  ({rate:.1%})")
    print(f"교정본 적재됨  {refined:>7,}건  ({refined / analyzed:.1%})")
    print()
    # 손익분기 40% 를 가운데로 두고 판정한다. 경계 구간은 굳이 한쪽으로 몰지
    # 않는다 — 그 구간에서는 절감액보다 온디맨드의 첫 조회 지연이 더 큰 변수다.
    if rate < 0.35:
        print(f"→ 열람률 {rate:.1%}: 온디맨드(B)가 유리하다. 융합 호출에서 교정을 빼고")
        print("  상세 첫 조회 때 교정 후 refined_text 에 캐시한다(이미지당 평생 1회).")
    elif rate > 0.45:
        print(f"→ 열람률 {rate:.1%}: 지금 방식(A) 유지가 유리하다. 교정을 융합 호출에")
        print("  얹혀 보내는 편이 독립 호출보다 한계비용이 싸다.")
    else:
        print(f"→ 열람률 {rate:.1%}: 손익분기 구간이다. 토큰 차이가 작으니 상세 첫 조회")
        print("  지연을 감수할지로 결정한다(온디맨드는 첫 조회에 LLM 1콜이 붙는다).")

    if refined < viewed:
        print()
        print(f"⚠️ 열람된 {viewed:,}건 중 교정본이 없는 문서가 있다 — 배선 이전에 분석된")
        print("  문서이거나 비정보성(EMPTY) 판정이다. 앱은 rawText 로 폴백한다.")


if __name__ == "__main__":
    main()
