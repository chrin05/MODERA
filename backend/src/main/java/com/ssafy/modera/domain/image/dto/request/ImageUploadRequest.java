package com.ssafy.modera.domain.image.dto.request;

import java.util.List;

import jakarta.validation.Valid;
import jakarta.validation.constraints.DecimalMax;
import jakarta.validation.constraints.DecimalMin;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Size;

public record ImageUploadRequest(
		@NotEmpty
//		@Size(max = 20)
		List<@Valid ImageItem> images) {
	public record ImageItem(
            @NotBlank String clientRequestId,
            @NotBlank String fileName,
            @NotBlank String contentType,
            @NotBlank String contentHash,
            @Positive long fileSize,
            @Valid Ocr ocr
    ) {
    }

    public record Ocr(
            String rawText,
            String lang,
            @DecimalMin("0.0")
            @DecimalMax("1.0")
            Double confidence
    ) {
    }
}
