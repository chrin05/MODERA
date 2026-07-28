# 문서화 API 산출물 샘플

[`../DOCUMENT_API.md`](../DOCUMENT_API.md) 의 `POST /internal/v1/documents` 를 **실제로 호출해
받은 출력**이다. 손으로 쓴 예시가 아니다. 입력은 전부 합성 데이터(가상의 스크린샷 분석 결과)다.

| 파일 | 내용 |
| --- | --- |
| [`여행-일정.html`](여행-일정.html) | `html` 필드 전문. 브라우저로 열면 실제 화면이 보인다 |
| [`여행-일정.response.json`](여행-일정.response.json) | 응답 전문. Spring 이 받는 JSON 구조 |

입력 — 항공·숙소·맛집·관광지·렌터카 스크린샷 5장,
`title: "제주 3일 여행 준비"`, `instruction: "일정 순서대로 정리하고, 예약 확정된 것과 후보만 있는 것을 구분해 줘"`.
생성 — 모델 `gemini-3.5-flash-lite`, 2026-07-28.

## 볼 것

- **N장 → 문서 1개.** 5장이 하나의 글로 합쳐졌다. 장당 문서가 아니다.
- **섹션 구성은 `category` 가 아니라 `instruction` 을 따른다.** 입력 카테고리는 예약·음식·여행
  3종인데 `"예약 확정 / 후보"` 지시에 맞춰 2섹션으로 갈렸다.
- **`title` 은 요청값 고정.** 모델이 다른 제목을 내도 요청의 `"제주 3일 여행 준비"` 가 이긴다.
- **사실은 원문 그대로.** 예약번호(`A1B2C3`, `HT-9931`)·금액·시각이 입력값과 일치한다. 지어낸 값 없음.
- **출처가 두 층.** 섹션 끝 `.sec-src` 와 문서 끝 `.doc-src` 목록. 원본 스크린샷을 되찾기 위한 것.

## 스크립트 차단

`<head>` 에 `Content-Security-Policy: default-src 'none'` 이 박혀 있다. 모든 값을
이스케이프하는 데다, 그게 새더라도 script·fetch·iframe·외부 리소스가 실행되지 않는다.
`href`·`src`·`on*` 속성은 애초에 생성하지 않는다.

## 레이아웃 멱등성

`<style>` 블록과 태그 구조는 **이미지 종류·장수·섹션 수와 무관하게 항상 같다.**
파이썬이 고정 템플릿으로 렌더하고 모델은 구조(JSON)만 내기 때문이다.
쇼핑 문서로 바꿔도 이 파일의 `<head>` 와 클래스 이름은 한 글자도 안 바뀐다.
회귀 검사는 `test/test_document.py` 의 `test_layout_is_identical_across_documents`.

## 다시 만들려면

Swagger `/docs` 의 `POST /internal/v1/documents` Example Value 를 그대로 Try it out.
모델 출력이라 문장은 매번 조금씩 달라지지만 레이아웃은 동일하다.
