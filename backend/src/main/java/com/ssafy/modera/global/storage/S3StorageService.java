package com.ssafy.modera.global.storage;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Component;
import software.amazon.awssdk.core.exception.SdkException;
import software.amazon.awssdk.services.s3.model.PutObjectRequest;
import software.amazon.awssdk.services.s3.presigner.S3Presigner;
import software.amazon.awssdk.services.s3.presigner.model.PutObjectPresignRequest;

import java.time.Duration;
import java.util.UUID;

@Slf4j
@Component
@RequiredArgsConstructor
public class S3StorageService {

    private static final Duration UPLOAD_URL_EXPIRATION =
            Duration.ofMinutes(5);

    private final S3Presigner s3Presigner;
    private final S3Properties properties;

    public UploadTarget createUploadUrl(
            Long userId,
            String originalFileName,
            String contentType
    ) {
        String key = createObjectKey(userId, originalFileName);

        try {
            PutObjectRequest objectRequest = PutObjectRequest.builder()
                    .bucket(properties.bucket())
                    .key(key)
                    .contentType(contentType)
                    .build();

            PutObjectPresignRequest presignRequest =
                    PutObjectPresignRequest.builder()
                            .signatureDuration(UPLOAD_URL_EXPIRATION)
                            .putObjectRequest(objectRequest)
                            .build();

            String uploadUrl = s3Presigner
                    .presignPutObject(presignRequest)
                    .url()
                    .toString();

            return new UploadTarget(key, uploadUrl);

        } catch (SdkException | IllegalArgumentException exception) {
            log.error(
                    "Presigned URL 생성 실패: userId={}, key={}",
                    userId,
                    key,
                    exception
            );

            throw new StorageException(
                    "Presigned URL 생성에 실패했습니다.",
                    exception
            );
        }
    }

    private String createObjectKey(
            Long userId,
            String originalFileName
    ) {
        String extension = extractExtension(originalFileName);

        return "users/%d/images/%s%s".formatted(
                userId,
                UUID.randomUUID(),
                extension
        );
    }

    private String extractExtension(String fileName) {
        int position = fileName.lastIndexOf(".");

        return position >= 0
                ? fileName.substring(position).toLowerCase()
                : "";
    }

    public record UploadTarget(
            String key,
            String uploadUrl
    ) {
    }
}