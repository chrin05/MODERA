package com.ssafy.modera.domain.image;

import com.ssafy.modera.domain.image.dto.request.ImageUploadRequest;
import com.ssafy.modera.domain.image.dto.response.ImageUploadResponse;
import com.ssafy.modera.domain.image.service.ImageService;
import com.ssafy.modera.global.domain.dto.CommonResponse;
import com.ssafy.modera.global.security.principal.PrincipalDetails;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
@RestController
@RequestMapping("/api/v1/images")
@RequiredArgsConstructor
public class ImageController {
	
	private final ImageService imageService;
	
	@PostMapping("/upload")
	public CommonResponse<ImageUploadResponse> createUploadUrls(
			@AuthenticationPrincipal PrincipalDetails principal,
			@Valid @RequestBody ImageUploadRequest request
			) {
		ImageUploadResponse response =
                imageService.createUploadUrls(
                        principal.getUserId(),
                        request
                );

        return CommonResponse.onSuccess(response);
	}
}
