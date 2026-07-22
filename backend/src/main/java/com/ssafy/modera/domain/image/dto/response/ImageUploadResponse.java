package com.ssafy.modera.domain.image.dto.response;

import java.util.List;

public record ImageUploadResponse(
        List<ImageItem> images
) {
    public record ImageItem(
            Long imageId,
            String clientRequestId,
            String s3Key,
            String uploadUrl
    ) {
    }
}