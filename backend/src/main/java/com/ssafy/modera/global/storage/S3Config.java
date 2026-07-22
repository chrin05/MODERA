package com.ssafy.modera.global.storage;


import java.net.URI;

import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.S3Configuration;
import software.amazon.awssdk.services.s3.presigner.S3Presigner;

@Configuration
@EnableConfigurationProperties(S3Properties.class)
public class S3Config {
	@Bean 
	public S3Presigner s3Presigner(S3Properties properties) {
		return S3Presigner.builder()
				.endpointOverride(URI.create(properties.endpoint()))
				.region(Region.of(properties.region()))
				.credentialsProvider(StaticCredentialsProvider.create(
						AwsBasicCredentials.create(
								properties.accessKey(),
								properties.secretKey()
								)
						)
				)
				.serviceConfiguration(
						S3Configuration.builder()
						.pathStyleAccessEnabled(properties.pathStyleAccess())
						.build()
						)
				.build();
	}
}
