package com.ssafy.modera.api.domain.image.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.ssafy.modera.api.domain.image.repository.ImageListPage;
import com.ssafy.modera.api.domain.image.repository.ImageListRow;
import com.ssafy.modera.api.domain.image.repository.ImageQueryRepository;
import com.ssafy.modera.api.domain.image.repository.UserImageViewDetail;
import com.ssafy.modera.api.global.config.StorageProperties;
import com.ssafy.modera.api.global.exception.BusinessException;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.presigner.S3Presigner;
import software.amazon.awssdk.services.s3.presigner.model.PresignedGetObjectRequest;

import java.net.URI;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.when;
import static org.mockito.ArgumentMatchers.any;

@ExtendWith(MockitoExtension.class)
class ImageQueryServiceTest {

    @Mock ImageQueryRepository imageQueryRepository;
    @Mock StorageProperties storageProperties;
    @Mock S3Client s3Client;
    @Mock S3Presigner s3Presigner;

    private ImageQueryService imageQueryService;

    @BeforeEach
    void setUp() {
        // parseStructuredData가 실제 JSON을 파싱하므로 ObjectMapper는 진짜를 쓴다.
        imageQueryService = new ImageQueryService(
                imageQueryRepository, storageProperties, s3Client, s3Presigner, new ObjectMapper());
    }

    @Test
    void rejectsUnsupportedSort() {
        assertThatThrownBy(() -> imageQueryService.getImages(
                1, null, 0, 20, "UNKNOWN", null, null))
                .isInstanceOf(BusinessException.class);
    }

    @Test
    void returnsPageInSpecificationShape() {
        OffsetDateTime uploadedAt = OffsetDateTime.now();
        when(imageQueryRepository.findImages(
                1, null, null, "C++", "UPLOADED_DESC", 0, 20))
                .thenReturn(new ImageListPage(
                        List.of(new ImageListRow(
                                10, "C++", "summary", false, null,
                                List.of("C++", "공부"), "공부", uploadedAt)),
                        1
                ));

        var response = imageQueryService.getImages(
                1, null, 0, 20, "UPLOADED_DESC", "  C++  ", null);

        assertThat(response.totalElements()).isEqualTo(1);
        assertThat(response.totalPages()).isEqualTo(1);
        assertThat(response.hasNext()).isFalse();
        assertThat(response.hasPrevious()).isFalse();
        assertThat(response.list().getFirst().thumbnailUrl()).isNull();
        assertThat(response.list().getFirst().tags()).containsExactly("C++", "공부");
    }

    @Test
    void rejectsDetailWhenAnalysisIsNotCompleted() {
        when(imageQueryRepository.findDetail(1, 10))
                .thenReturn(Optional.of(new UserImageViewDetail(
                        "1/10-image.jpg",
                        null,
                        "UPLOADED",
                        "FAILED",
                        "image.jpg",
                        false,
                        null,
                        null,
                        List.of(),
                        List.of(),
                        null,
                        false,
                        false
                )));

        assertThatThrownBy(() -> imageQueryService.getImage(1, 10))
                .isInstanceOf(BusinessException.class)
                .satisfies(exception -> assertThat(
                        ((BusinessException) exception).getErrorCode().getCode()
                ).isEqualTo("IMAGE_ANALYSIS_NOT_COMPLETED"));
    }

    @Test
    void returnsCompletedImageDetail() throws Exception {
        when(imageQueryRepository.findDetail(1, 10))
                .thenReturn(Optional.of(new UserImageViewDetail(
                        "1/10-image.jpg",
                        null,
                        "UPLOADED",
                        "COMPLETED",
                        "image.jpg",
                        true,
                        "summary",
                        "공부",
                        List.of("C++"),
                        List.of("가격: 32000원"),
                        null,
                        false,
                        false
                )));
        StorageProperties.Bucket bucket = new StorageProperties.Bucket();
        bucket.setPictures("pictures");
        when(storageProperties.getBucket()).thenReturn(bucket);
        PresignedGetObjectRequest presigned =
                org.mockito.Mockito.mock(PresignedGetObjectRequest.class);
        when(presigned.url()).thenReturn(
                URI.create("https://storage.example/image").toURL()
        );
        when(s3Presigner.presignGetObject(any(
                software.amazon.awssdk.services.s3.presigner.model.GetObjectPresignRequest.class
        ))).thenReturn(presigned);

        var response = imageQueryService.getImage(1, 10);

        assertThat(response.imageId()).isEqualTo(10);
        assertThat(response.imageUrl()).isEqualTo("https://storage.example/image");
        assertThat(response.favorite()).isTrue();
        assertThat(response.tags()).containsExactly("C++");
    }

    @Test
    void convertsScheduleStructuredDataToTimestamps() throws Exception {
        when(imageQueryRepository.findDetail(1, 51))
                .thenReturn(Optional.of(new UserImageViewDetail(
                        "3/51-a.jpg", null, "UPLOADED", "COMPLETED", "공모전", false,
                        "요약", "일정", List.of(), List.of(),
                        "{\"type\":\"schedule\",\"fields\":{"
                                + "\"startYear\":null,\"startMonth\":null,\"startDay\":null,\"startTime\":null,"
                                + "\"endYear\":2026,\"endMonth\":8,\"endDay\":28,\"endTime\":\"18:00\"}}",
                        false, false)));
        stubPresignedImageUrl();

        var response = imageQueryService.getImage(1, 51);

        assertThat(response.scheduledData())
                .containsEntry("type", "schedule")
                .containsEntry("startAt", null)
                .containsEntry("endAt", "2026-08-28T09:00:00Z");
    }

    @Test
    void keepsNonScheduleStructuredDataUnchanged() throws Exception {
        when(imageQueryRepository.findDetail(1, 52))
                .thenReturn(Optional.of(new UserImageViewDetail(
                        "3/52-a.jpg", null, "UPLOADED", "COMPLETED", "상품", false,
                        "요약", "쇼핑", List.of(), List.of(),
                        "{\"type\":\"product-info\",\"fields\":{\"price\":32000}}",
                        false, false)));
        stubPresignedImageUrl();

        var response = imageQueryService.getImage(1, 52);

        assertThat(response.scheduledData()).containsEntry("type", "product-info");
        assertThat(response.scheduledData().get("fields"))
                .isEqualTo(java.util.Map.of("price", 32000));
    }

    private void stubPresignedImageUrl() throws Exception {
        StorageProperties.Bucket bucket = new StorageProperties.Bucket();
        bucket.setPictures("pictures");
        when(storageProperties.getBucket()).thenReturn(bucket);
        PresignedGetObjectRequest presigned =
                org.mockito.Mockito.mock(PresignedGetObjectRequest.class);
        when(presigned.url()).thenReturn(URI.create("https://storage.example/image").toURL());
        when(s3Presigner.presignGetObject(any(
                software.amazon.awssdk.services.s3.presigner.model.GetObjectPresignRequest.class
        ))).thenReturn(presigned);
    }
}
