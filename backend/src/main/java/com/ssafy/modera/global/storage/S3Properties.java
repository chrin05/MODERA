package com.ssafy.modera.global.storage;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "storage.s3")
public record S3Properties(
		String endpoint,
		String region,
		String bucket,
		String accessKey,
		String secretKey,
		boolean pathStyleAccess
		) {
	
}
