"""앱 직결 이미지 API (`/api/v1/images/*`).

명세 4 장(등록·업로드·OCR)과 6 장(목록·상세·썸네일·원본). 앱 API 공통 규약과
Spring 복귀 전제는 아래 배너에 그대로 남겨 뒀다.

라우트 본문은 main.py 에 있던 것을 그대로 옮겼다 — `@app.` 이 `@router.` 로
바뀐 것뿐이다. 공유 헬퍼(`_owned_image`·`_thumbnail_url` 등)는 deps.py 에 있다.
"""

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.responses import Response

from . import responses, search, storage
from .config import get_settings
from .timeutil import now_iso
from .deps import (
    CurrentUserId,
    _category_refs,
    _image_url,
    _owned_image,
    _resolve_filters,
    _tag_refs,
    _thumbnail_url,
    _to_list_item,
    require_internal_token,
)
from .jobs import job_store
from .schemas import (
    ApiResponse,
    DuplicatedUpload,
    FailedUpload,
    ImageDetail,
    ImageListItem,
    OcrInput,
    OcrSubmitResponse,
    PageData,
    RegisteredUpload,
    ThumbnailResponse,
    UploadCompleteResponse,
    UploadRequest,
    UploadResponse,
    UploadUrlResponse,
)
from .stages import run_app_analysis

logger = logging.getLogger(__name__)

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────
# 앱 API (Spring 우회 구간)
#
# 팀 API 명세의 외부 API 규약을 따른다: 공통 envelope, 페이지 형식, 에러 코드.
# Spring 이 복귀하면 앱은 호출 대상만 Spring 으로 바꾸면 되도록 응답 구조를 맞춰 둔다.
# 이 서비스가 채울 수 없는 값(favorite, fileName, structuredData 등)은
# 필드를 유지한 채 null 로 내려보낸다.
# ─────────────────────────────────────────────────────────────────────────


# ── 4-1 이미지 등록 및 업로드 URL 발급 ───────────────────────────────────
# 명세대로 **바이너리를 받지 않는다.** 메타데이터를 등록하고 콘텐츠 해시로 중복을
# 판정한 뒤 presigned PUT URL 을 발급한다. 앱이 그 URL 로 스토리지에 직접 올리고
# 4-2 로 완료를 통지하면 분석이 시작된다.
# 이미지 바이트가 AI 서버를 통과하지 않아 대량 업로드에도 서버가 흔들리지 않는다.
_ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "heic", "heif"}


def _extension(file_name: str) -> str | None:
    _, _, ext = (file_name or "").rpartition(".")
    ext = ext.lower()
    return ext if ext in _ALLOWED_EXTENSIONS else None


@router.post("/api/v1/images/upload", dependencies=[Depends(require_internal_token)],
          response_model=ApiResponse[UploadResponse])
async def app_image_upload(user_id: CurrentUserId, request: UploadRequest):
    """이미지를 등록하고 업로드 URL 을 발급한다(명세 4-1).

    항목별로 성공·중복·실패를 나눠 돌려주므로, 일부가 실패해도 나머지는 계속 진행된다.
    `clientRequestId` 를 그대로 되돌려 주어 앱이 원래 사진과 매칭할 수 있다.
    """
    import asyncio

    settings = get_settings()
    if not request.images:
        return responses.failure("INVALID_PARAMETER", "요청 값이 올바르지 않습니다.",
                                 [{"field": "images", "message": "필수 값입니다."}])

    limit = settings.max_upload_mb * 1024 * 1024
    registered: list[RegisteredUpload] = []
    duplicated: list[DuplicatedUpload] = []
    failed: list[FailedUpload] = []
    # 같은 배치 안의 중복도 잡아야 한다. 방금 등록한 문서는 색인 refresh 전이라
    # find_by_content_hash 로는 아직 안 보이기 때문이다.
    batch_hashes: dict[str, int] = {}

    for item in request.images:
        ext = _extension(item.file_name)
        if ext is None:
            failed.append(FailedUpload(client_request_id=item.client_request_id,
                                       file_name=item.file_name,
                                       reason="UNSUPPORTED_FORMAT"))
            continue
        if item.file_size <= 0 or item.file_size > limit:
            failed.append(FailedUpload(client_request_id=item.client_request_id,
                                       file_name=item.file_name,
                                       reason="FILE_SIZE_EXCEEDED"))
            continue
        if not item.content_hash.strip():
            failed.append(FailedUpload(client_request_id=item.client_request_id,
                                       file_name=item.file_name,
                                       reason="INVALID_CONTENT_HASH"))
            continue

        # 중복 판정: 같은 사용자가 이미 올린 같은 내용이면 재분석하지 않는다.
        existing = batch_hashes.get(item.content_hash)
        if existing is None:
            existing = await asyncio.to_thread(
                search.find_by_content_hash, user_id, item.content_hash
            )
        if existing is not None:
            duplicated.append(DuplicatedUpload(client_request_id=item.client_request_id,
                                               file_name=item.file_name,
                                               existing_image_id=existing))
            continue

        image_id = job_store.next_image_id()
        s3_key = f"u/{user_id}/{image_id}.{ext}"
        try:
            url, expires = await asyncio.to_thread(storage.presigned_put_url, s3_key)
        except Exception as e:
            logger.warning("업로드 URL 발급 실패 imageId=%s: %s", image_id, e)
            failed.append(FailedUpload(client_request_id=item.client_request_id,
                                       file_name=item.file_name,
                                       reason="UPLOAD_URL_ISSUE_FAILED"))
            continue

        ocr_text = (item.ocr.refined_text or item.ocr.raw_text or "").strip()
        await asyncio.to_thread(
            search.create_pending_document,
            image_id=image_id, user_id=user_id, s3_key=s3_key,
            file_name=item.file_name, content_hash=item.content_hash,
            file_size=item.file_size, created_at=now_iso(),
            raw_text=ocr_text,
            ocr_confidence=item.ocr.confidence,
        )
        batch_hashes[item.content_hash] = image_id
        registered.append(RegisteredUpload(
            client_request_id=item.client_request_id, image_id=image_id,
            file_name=item.file_name, upload_url=url, upload_expires_in=expires,
        ))

    if registered:
        # 배치당 1회. 이걸 안 하면 방금 등록한 이미지가 6-1 목록에 안 나오고
        # 다음 요청의 중복 판정에서도 놓친다.
        await asyncio.to_thread(search.refresh_index)

    logger.info("이미지 등록 userId=%s 성공=%s 중복=%s 실패=%s",
                user_id, len(registered), len(duplicated), len(failed))

    body = UploadResponse(registered=registered, duplicated=duplicated,
                          failed=failed).model_dump(by_alias=True)

    # 한 건도 등록되지 않았는데 실패만 남았으면 성공이 아니다. code 까지 SUCCESS 로
    # 주면 프론트가 code 만 보고 성공 처리해 사진이 조용히 유실된다.
    # data 형태(registered/duplicated/failed)는 그대로 두므로 파싱은 깨지지 않는다.
    if failed and not registered:
        reasons = [f.reason for f in failed]
        logger.warning("이미지 등록 전건 실패 userId=%s 사유=%s", user_id, reasons)
        # URL 발급 실패는 스토리지 장애(서버 탓), 나머지는 요청 값 문제(클라이언트 탓).
        server_fault = "UPLOAD_URL_ISSUE_FAILED" in reasons
        return responses.failure(
            "UPLOAD_REGISTRATION_FAILED", "이미지를 등록하지 못했습니다.",
            body, http_status=502 if server_fault else 400,
        )

    return responses.success(body, "이미지가 등록되었습니다.")


# ── 4-2 업로드 완료 통지 ─────────────────────────────────────────────────
@router.post("/api/v1/images/{image_id}/upload-complete",
          dependencies=[Depends(require_internal_token)],
          response_model=ApiResponse[UploadCompleteResponse])
async def app_upload_complete(
    image_id: int, user_id: CurrentUserId, background_tasks: BackgroundTasks
):
    """스토리지 업로드 완료를 통지받고 분석 파이프라인을 시작한다(명세 4-2).

    명세는 이 API 가 분석 작업을 만들지 않고 OCR 제출 후 LLM 부터 시작한다고 하지만,
    개정으로 OCR 이 4-1 요청에 합쳐졌다. 그래서 원본과 OCR 이 모두 갖춰지는 시점이
    바로 여기이고, 여기서 파이프라인을 시작한다.
    """
    found = await _owned_image(image_id, user_id)
    if found is None:
        return responses.failure("IMAGE_NOT_FOUND", "이미지를 찾을 수 없습니다.",
                                 f"imageId: {image_id}", http_status=404)

    s3_key = found.get("s3_key")
    if not s3_key or not await asyncio.to_thread(storage.object_exists, s3_key):
        return responses.failure("UPLOAD_NOT_FOUND", "업로드된 파일을 찾을 수 없습니다.",
                                 f"imageId: {image_id}", http_status=409)

    uploaded_at = now_iso()
    await asyncio.to_thread(search.mark_uploaded, image_id, uploaded_at)

    job = job_store.create(found.get("user_id") or 0, s3_key, image_id)
    background_tasks.add_task(
        run_app_analysis,
        job["job_id"], image_id, found.get("user_id") or 0, s3_key,
        found.get("raw_text", ""),
    )
    logger.info("업로드 완료 통지 imageId=%s jobId=%s", image_id, job["job_id"])

    return responses.success(
        UploadCompleteResponse(image_id=image_id, upload_completed=True,
                               uploaded_at=uploaded_at).model_dump(by_alias=True),
        "업로드가 완료되었습니다.",
    )


# ── 4-5 업로드 URL 재발급 ────────────────────────────────────────────────
@router.post("/api/v1/images/{image_id}/upload-url",
          dependencies=[Depends(require_internal_token)],
          response_model=ApiResponse[UploadUrlResponse])
async def app_reissue_upload_url(image_id: int, user_id: CurrentUserId):
    """presigned URL 이 만료됐는데 아직 업로드가 안 끝난 이미지에 새 URL 을 준다(명세 4-5)."""
    import asyncio

    found = await _owned_image(image_id, user_id)
    if found is None:
        return responses.failure("IMAGE_NOT_FOUND", "이미지를 찾을 수 없습니다.",
                                 f"imageId: {image_id}", http_status=404)
    if found.get("uploaded_at"):
        return responses.failure("UPLOAD_ALREADY_COMPLETED", "업로드가 이미 완료되었습니다.",
                                 f"imageId: {image_id}", http_status=409)
    if (found.get("status") or "QUEUED") not in ("QUEUED",):
        return responses.failure("ANALYSIS_IN_PROGRESS", "분석이 이미 시작되었습니다.",
                                 f"imageId: {image_id}", http_status=409)

    try:
        url, expires = await asyncio.to_thread(storage.presigned_put_url,
                                               found["s3_key"])
    except Exception as e:
        logger.exception("업로드 URL 재발급 실패 imageId=%s", image_id)
        return responses.failure("INTERNAL_ERROR", "업로드 URL 을 발급하지 못했습니다.",
                                 str(e)[:300], http_status=500)

    return responses.success(
        UploadUrlResponse(image_id=image_id, upload_url=url,
                          upload_expires_in=expires).model_dump(by_alias=True))


# ── 4-3 온디바이스 OCR 결과 제출 ─────────────────────────────────────────
@router.post("/api/v1/images/{image_id}/ocr",
          dependencies=[Depends(require_internal_token)],
          response_model=ApiResponse[OcrSubmitResponse])
async def app_submit_ocr(image_id: int, user_id: CurrentUserId, ocr: OcrInput):
    """모바일 OCR 결과를 저장한다(명세 4-3).

    개정 명세는 OCR 을 4-1 요청에 함께 받도록 바뀌었지만, 명세표에 4-3 이 그대로
    살아 있어 별도 제출도 받는다. 같은 내용의 재전송은 멱등하게 처리한다.
    """
    import asyncio

    found = await _owned_image(image_id, user_id)
    if found is None:
        return responses.failure("IMAGE_NOT_FOUND", "이미지를 찾을 수 없습니다.",
                                 f"imageId: {image_id}", http_status=404)

    incoming = (ocr.refined_text or ocr.raw_text or "").strip()
    existing = (found.get("raw_text") or "").strip()
    if existing and existing != incoming:
        return responses.failure("OCR_ALREADY_SUBMITTED",
                                 "다른 OCR 결과가 이미 제출되었습니다.",
                                 f"imageId: {image_id}", http_status=409)

    await asyncio.to_thread(search.save_ocr, image_id, incoming,
                            None, ocr.confidence)
    return responses.success({
        "imageId": image_id,
        "ocr": {"stage": "OCR", "status": "COMPLETED"},
    })


# ── 6-1 이미지 목록 ───────────────────────────────────────────────────────
@router.get("/api/v1/images", dependencies=[Depends(require_internal_token)],
         response_model=ApiResponse[PageData[ImageListItem]])
async def app_image_list(
    user_id: CurrentUserId,
    status: str | None = Query(None, description="분석 상태 필터, 콤마로 복수 전달"),
    category_id: int | None = Query(None, alias="categoryId"),
    tag_id: int | None = Query(None, alias="tagId"),
    favorite: bool | None = Query(None),
    date_from: str | None = Query(None, alias="dateFrom", description="ISO-8601"),
    date_to: str | None = Query(None, alias="dateTo", description="ISO-8601"),
    page: int = Query(0),
    size: int = Query(20),
    sort: str | None = Query(None, description="기본 createdAt,desc"),
):
    statuses = [s.strip().upper() for s in status.split(",") if s.strip()] if status else None
    try:
        import asyncio
        category, tag = await _resolve_filters(user_id, category_id, tag_id)
        images, total = await asyncio.to_thread(
            search.list_images, user_id, page, size, category, tag,
            statuses, favorite, date_from, date_to, sort,
        )
    except search.InvalidSortError:
        raise
    except Exception as e:
        logger.exception("이미지 목록 조회 실패")
        return responses.failure("INTERNAL_ERROR", "이미지 목록을 조회하지 못했습니다.",
                                 str(e)[:300], http_status=500)

    items = [_to_list_item(img).model_dump(by_alias=True) for img in images]
    return responses.success(responses.page_data(items, page, size, total))


# ── 6-2 이미지 상세 ───────────────────────────────────────────────────────
@router.get("/api/v1/images/{image_id}", dependencies=[Depends(require_internal_token)],
         response_model=ApiResponse[ImageDetail])
async def app_image_detail(image_id: int, user_id: CurrentUserId):
    try:
        found = await _owned_image(image_id, user_id)
    except Exception as e:
        logger.exception("이미지 상세 조회 실패")
        return responses.failure("INTERNAL_ERROR", "이미지를 조회하지 못했습니다.",
                                 str(e)[:300], http_status=500)

    if found is None:
        return responses.failure("IMAGE_NOT_FOUND", "이미지를 찾을 수 없습니다.",
                                 f"imageId: {image_id}", http_status=404)

    # 명세 6-2: 태그·필드의 출처는 전부 AGENT 다(6-3 사용자 수정이 범위 밖).
    field_sources = {
        name: "AGENT"
        for name in ("title", "summary", "tags", "categories")
        if found.get({"tags": "tags", "categories": "category"}.get(name, name))
    }
    detail = ImageDetail(
        image_id=found["image_id"],
        file_name=found.get("file_name"),
        content_hash=found.get("content_hash"),
        status=found.get("status") or "COMPLETED",
        favorite=found.get("favorite", False),
        title=found.get("title", ""),
        summary=found.get("summary", ""),
        # refinedText 는 융합 호출이 이미지와 대조해 교정한 화면 텍스트다.
        # 교정이 없었으면(비정보성·분석 전) null 이라 앱은 rawText 로 폴백한다.
        ocr=OcrInput(raw_text=found.get("raw_text", ""),
                     refined_text=found.get("refined_text"),
                     confidence=found.get("ocr_confidence")),
        tags=_tag_refs(found.get("tags") or [], source="AGENT"),
        categories=_category_refs(found.get("category"),
                                  found.get("category_confidence")),
        field_sources=field_sources,
        key_information=found.get("key_information") or [],
        image_url=_image_url(found["image_id"], found.get("s3_key")),
        created_at=found.get("created_at"),
        uploaded_at=found.get("uploaded_at"),
        updated_at=found.get("created_at"),
        last_viewed_at=found.get("last_viewed_at"),
    )

    # 명세 8-1: lastViewedAt 은 6-2 상세 조회 성공 시에만 갱신한다.
    # 응답에는 이번 조회 이전 값을 담아 내려보낸다(방금 본 시각을 돌려주면 무의미하다).
    await asyncio.to_thread(search.touch_last_viewed, found["image_id"], now_iso())
    return responses.success(detail.model_dump(by_alias=True))


# ── 6-6 썸네일 조회 ───────────────────────────────────────────────────────
@router.get("/api/v1/images/{image_id}/thumbnail",
         dependencies=[Depends(require_internal_token)],
         response_model=ApiResponse[ThumbnailResponse])
async def app_image_thumbnail_meta(image_id: int, user_id: CurrentUserId):
    """명세 6-6 Response data: `{thumbnailUrl, title, tags}`.

    `thumbnailUrl` 은 아래 `/raw` 경로를 가리킨다. 만료가 없어 앱이 캐시하기 좋고,
    스토리지를 외부에 공개하지 않아도 된다.
    """
    found = await _owned_image(image_id, user_id)
    if found is None:
        return responses.failure("IMAGE_NOT_FOUND", "이미지를 찾을 수 없습니다.",
                                 f"imageId: {image_id}", http_status=404)
    if not found.get("s3_key"):
        return responses.failure("THUMBNAIL_NOT_FOUND", "썸네일이 없습니다.",
                                 f"imageId: {image_id}", http_status=404)
    return responses.success({
        "thumbnailUrl": _thumbnail_url(image_id, found.get("s3_key")),
        "title": found.get("title", ""),
        # 명세 6-6 의 tags 는 이름 배열이다(6-1·6-2 의 객체 배열과 다르다).
        "tags": found.get("tags") or [],
    })


@router.get("/api/v1/images/{image_id}/thumbnail/raw",
         dependencies=[Depends(require_internal_token)],
         response_class=Response,
         responses={200: {"content": {"image/jpeg": {}},
                          "description": "썸네일 JPEG 바이너리"}})
async def app_image_thumbnail(image_id: int, user_id: CurrentUserId):
    """썸네일 바이너리(명세 6-6 의 thumbnailUrl 이 가리키는 실제 이미지).

    명세 6-6 본문은 `{thumbnailUrl, title, tags}` JSON 이라 그 형식은 상위 경로가
    담당하고, 이미지 자체는 이 경로가 준다. thumbnailUrl 을 스토리지 presigned GET
    으로 주지 않는 이유: presigned 는 만료(1시간)가 있어 앱의 이미지 캐시가 매번
    깨지고, 스토리지를 외부에 공개해야 한다. 이 경로는 만료가 없다.

    분석할 때 원본과 같은 key 로 썸네일 버킷에 올려 두므로 보통은 그것을 그대로 읽는다.
    없으면(분석 전 이미지, 저장 실패) 원본에서 즉석 생성해 내려주고 그때 버킷에도 채운다.

    presigned URL 을 저장했다가 돌려주면 만료로 깨지므로 주소는 이 경로로 고정한다.
    이 응답만 공통 envelope 를 쓰지 않는다.
    """
    found = await _owned_image(image_id, user_id)
    if found is None:
        return responses.failure("IMAGE_NOT_FOUND", "이미지를 찾을 수 없습니다.",
                                 f"imageId: {image_id}", http_status=404)

    image_ref = found.get("s3_key")
    if not image_ref:
        return responses.failure("THUMBNAIL_NOT_FOUND", "썸네일을 만들 원본이 없습니다.",
                                 f"imageId: {image_id}", http_status=404)

    thumb: bytes | None = None
    if get_settings().s3_thumbnail_bucket:
        try:
            thumb = await asyncio.to_thread(storage.fetch_thumbnail, image_ref)
        except Exception as e:
            logger.info("저장된 썸네일 없음 imageId=%s: %s — 즉석 생성", image_id, e)

    if thumb is None:
        try:
            thumb = await asyncio.to_thread(storage.store_thumbnail, image_ref)
        except Exception as e:
            logger.warning("썸네일 생성 실패 imageId=%s: %s", image_id, e)
            return responses.failure("THUMBNAIL_NOT_FOUND", "썸네일을 가져오지 못했습니다.",
                                     str(e)[:200], http_status=404)

    return Response(content=thumb, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


# ── 원본 이미지 조회 (명세 6-8 의 바이너리 버전) ──────────────────────────
@router.get("/api/v1/images/{image_id}/source",
         dependencies=[Depends(require_internal_token)],
         response_class=Response,
         responses={200: {"content": {"image/*": {}},
                          "description": "원본 이미지 바이너리"}})
async def app_image_source(image_id: int, user_id: CurrentUserId):
    """원본 이미지를 그대로 돌려준다.

    썸네일(`/thumbnail/raw`)은 정사각으로 잘려 있어 스크린샷 내용이 다 안 보인다.
    상세 화면처럼 전체를 봐야 하는 곳은 이 경로를 쓴다.

    원본은 수 MB 라 목록에서 여러 장을 한꺼번에 부르면 느려진다. 목록·격자는
    썸네일을, 상세는 이 경로를 쓰는 것을 전제로 한다.
    """
    import asyncio

    found = await _owned_image(image_id, user_id)
    if found is None:
        return responses.failure("IMAGE_NOT_FOUND", "이미지를 찾을 수 없습니다.",
                                 f"imageId: {image_id}", http_status=404)
    s3_key = found.get("s3_key")
    if not s3_key:
        return responses.failure("SOURCE_IMAGE_NOT_FOUND", "원본 이미지가 없습니다.",
                                 f"imageId: {image_id}", http_status=404)
    try:
        data = await asyncio.to_thread(storage.fetch_image, s3_key)
    except Exception as e:
        logger.warning("원본 조회 실패 imageId=%s: %s", image_id, e)
        return responses.failure("SOURCE_IMAGE_NOT_FOUND", "원본을 가져오지 못했습니다.",
                                 str(e)[:200], http_status=404)

    # 확장자로 미디어 타입을 정한다. 모르면 브라우저·이미지 로더가 알아서 판별하도록 둔다.
    ext = s3_key.rsplit(".", 1)[-1].lower() if "." in s3_key else ""
    media = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
             "webp": "image/webp", "heic": "image/heic",
             "heif": "image/heif"}.get(ext, "application/octet-stream")
    return Response(content=data, media_type=media,
                    headers={"Cache-Control": "public, max-age=86400"})
