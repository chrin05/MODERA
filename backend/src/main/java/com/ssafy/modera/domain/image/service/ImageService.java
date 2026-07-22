package com.ssafy.modera.domain.image.service;

import com.ssafy.modera.domain.image.dto.request.ImageUploadRequest;
import com.ssafy.modera.domain.image.dto.response.ImageUploadResponse;
import com.ssafy.modera.domain.image.entity.Image;
import com.ssafy.modera.domain.image.exception.ImageErrorCode;
import com.ssafy.modera.domain.image.repository.ImageRepository;
import com.ssafy.modera.global.exception.BusinessException;
import com.ssafy.modera.global.storage.S3StorageService;
import com.ssafy.modera.global.storage.StorageException;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.util.List;
import java.util.Set;

@Service
@RequiredArgsConstructor
public class ImageService {

    private static final long MAX_FILE_SIZE = 10 * 1024 * 1024;
    private static final Set<String> ALLOWED_CONTENT_TYPES = Set.of(
            "image/jpeg", "image/png", "image/webp"
    );

    private final S3StorageService storageService;
    private final ImageRepository imageRepository;

    @Transactional
    public ImageUploadResponse createUploadUrls(Long userId, ImageUploadRequest request) {
        List<ImageUploadResponse.ImageItem> results = request.images().stream()
                .map(item -> createUploadUrl(userId, item))
                .toList();
        return new ImageUploadResponse(results);
    }

    private ImageUploadResponse.ImageItem createUploadUrl(
            Long userId,
            ImageUploadRequest.ImageItem item
    ) {
        validateFile(item);

        try {
            S3StorageService.UploadTarget target = storageService.createUploadUrl(
                    userId, item.fileName(), item.contentType()
            );

            ImageUploadRequest.Ocr ocr = item.ocr();
            Image image = Image.queued(
                    userId,
                    item.clientRequestId(),
                    item.fileName(),
                    item.contentHash(),
                    item.fileSize(),
                    target.key(),
                    ocr == null ? null : ocr.rawText(),
                    ocr == null ? null : ocr.lang(),
                    ocr == null || ocr.confidence() == null
                            ? null
                            : ocr.confidence().floatValue()
            );
            Image savedImage = imageRepository.save(image);

            return new ImageUploadResponse.ImageItem(
                    savedImage.getImageId(),
                    item.clientRequestId(),
                    target.key(),
                    target.uploadUrl()
            );
        } catch (StorageException exception) {
            throw new BusinessException(ImageErrorCode.UPLOAD_URL_GENERATION_FAILED);
        }
    }

    private void validateFile(ImageUploadRequest.ImageItem item) {
        if (!StringUtils.hasText(item.fileName())) {
            throw new BusinessException(ImageErrorCode.INVALID_FILE_NAME);
        }
        if (item.fileSize() <= 0) {
            throw new BusinessException(ImageErrorCode.EMPTY_IMAGE_FILE);
        }
        if (item.fileSize() > MAX_FILE_SIZE) {
            throw new BusinessException(ImageErrorCode.IMAGE_FILE_TOO_LARGE);
        }
        if (!StringUtils.hasText(item.contentType())
                || !ALLOWED_CONTENT_TYPES.contains(item.contentType())) {
            throw new BusinessException(ImageErrorCode.UNSUPPORTED_IMAGE_TYPE);
        }
        if (!isValidSha256(item.contentHash())) {
            throw new BusinessException(ImageErrorCode.INVALID_CONTENT_HASH);
        }
    }

    private boolean isValidSha256(String contentHash) {
        return StringUtils.hasText(contentHash)
                && contentHash.matches("^[a-fA-F0-9]{64}$");
    }
}
