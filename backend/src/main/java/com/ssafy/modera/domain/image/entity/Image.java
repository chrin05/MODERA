package com.ssafy.modera.domain.image.entity;

import com.ssafy.modera.global.domain.BaseTimeEntity;
import jakarta.persistence.*;
import lombok.AccessLevel;
import lombok.Getter;
import lombok.NoArgsConstructor;
import org.hibernate.annotations.JdbcTypeCode;

import java.sql.Types;

@Entity
@Table(name = "image")
@Getter
@NoArgsConstructor(access = AccessLevel.PROTECTED)
public class Image extends BaseTimeEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    @Column(name = "image_id")
    private Long imageId;

    @Column(name = "user_id", nullable = false)
    private Long userId;

    @Column(name = "client_request_id", nullable = false, length = 100)
    private String clientRequestId;

    @Column(name = "file_name", nullable = false, length = 255)
    private String fileName;

    @JdbcTypeCode(Types.CHAR)
    @Column(name = "content_hash", nullable = false, length = 64)
    private String contentHash;

    @Column(name = "file_size", nullable = false)
    private int fileSize;

    @Column(name = "s3_key", length = 255)
    private String s3Key;

    @Column(name = "status", nullable = false, length = 12)
    private String status;

    @Column(name = "ocr_raw_text")
    private String ocrRawText;

    @Column(name = "ocr_lang", length = 10)
    private String ocrLang;

    @Column(name = "ocr_confidence")
    private Float ocrConfidence;

    public static Image queued(
            Long userId, String clientRequestId, String fileName,
            String contentHash, long fileSize, String s3Key,
            String ocrRawText, String ocrLang, Float ocrConfidence
    ) {
        Image image = new Image();
        image.userId = userId;
        image.clientRequestId = clientRequestId;
        image.fileName = fileName;
        image.contentHash = contentHash;
        image.fileSize = Math.toIntExact(fileSize);
        image.s3Key = s3Key;
        image.status = "QUEUED";
        image.ocrRawText = ocrRawText;
        image.ocrLang = ocrLang;
        image.ocrConfidence = ocrConfidence;
        return image;
    }
}
