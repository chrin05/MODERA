package com.ssafy.modera.domain.image.exception;

import com.ssafy.modera.global.domain.ErrorCode;
import lombok.Getter;
import lombok.RequiredArgsConstructor;
import org.springframework.http.HttpStatus;

@Getter
@RequiredArgsConstructor
public enum ImageErrorCode implements ErrorCode {

    // 파일 검증
    EMPTY_IMAGE_FILE(
            HttpStatus.BAD_REQUEST,
            "I001",
            "빈 이미지 파일은 업로드할 수 없습니다."
    ),

    UNSUPPORTED_IMAGE_TYPE(
            HttpStatus.UNSUPPORTED_MEDIA_TYPE,
            "I002",
            "지원하지 않는 이미지 형식입니다."
    ),

    IMAGE_FILE_TOO_LARGE(
            HttpStatus.PAYLOAD_TOO_LARGE,
            "I003",
            "이미지 파일 크기가 허용 범위를 초과했습니다."
    ),

    INVALID_FILE_NAME(
            HttpStatus.BAD_REQUEST,
            "I004",
            "유효하지 않은 파일 이름입니다."
    ),

    INVALID_CONTENT_HASH(
            HttpStatus.BAD_REQUEST,
            "I005",
            "유효하지 않은 이미지 해시값입니다."
    ),

    // S3·MinIO 업로드
    UPLOAD_URL_GENERATION_FAILED(
            HttpStatus.INTERNAL_SERVER_ERROR,
            "I006",
            "이미지 업로드 URL 생성에 실패했습니다."
    ),

    IMAGE_UPLOAD_FAILED(
            HttpStatus.BAD_GATEWAY,
            "I007",
            "이미지 저장소 업로드에 실패했습니다."
    ),

    UPLOADED_IMAGE_NOT_FOUND(
            HttpStatus.NOT_FOUND,
            "I008",
            "업로드된 이미지 파일을 찾을 수 없습니다."
    ),

    // 이미지 조회
    IMAGE_NOT_FOUND(
            HttpStatus.NOT_FOUND,
            "I009",
            "이미지 정보를 찾을 수 없습니다."
    ),

    IMAGE_ACCESS_DENIED(
            HttpStatus.FORBIDDEN,
            "I010",
            "해당 이미지에 접근할 권한이 없습니다."
    ),
    ;

    private final HttpStatus status;
    private final String code;
    private final String message;
}