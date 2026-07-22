package com.ssafy.modera.global.config;

import com.ssafy.modera.global.security.filter.JwtAuthenticationFilter;
import com.ssafy.modera.global.security.filter.SecurityAuditLogger;
import org.springframework.boot.security.autoconfigure.web.servlet.PathRequest;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.Customizer;
import org.springframework.security.config.annotation.method.configuration.EnableMethodSecurity;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.annotation.web.configurers.AbstractHttpConfigurer;
import org.springframework.security.config.annotation.web.configurers.HeadersConfigurer;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.UsernamePasswordAuthenticationFilter;
import org.springframework.security.web.header.writers.ReferrerPolicyHeaderWriter;
import org.springframework.web.cors.CorsConfigurationSource;

@Configuration
@EnableWebSecurity
@EnableMethodSecurity
public class SecurityConfig {
    private final CorsConfigurationSource corsConfigurationSource;
    private final SecurityExceptionConfig securityExceptionConfig;
    private final SecurityAuditLogger securityAuditLogger;
    private final JwtAuthenticationFilter jwtAuthenticationFilter;

    public SecurityConfig(
            @Qualifier("corsConfigurationSource") CorsConfigurationSource corsConfigurationSource,
            SecurityExceptionConfig securityExceptionConfig,
            SecurityAuditLogger securityAuditLogger,
            JwtAuthenticationFilter jwtAuthenticationFilter
    ) {
        this.corsConfigurationSource = corsConfigurationSource;
        this.securityExceptionConfig = securityExceptionConfig;
        this.securityAuditLogger = securityAuditLogger;
        this.jwtAuthenticationFilter = jwtAuthenticationFilter;
    }

    private static final String[] PUBLIC_URLS = {
            "/",
            "/favicon.ico",
            "/v3/api-docs/**",
            "/swagger-ui/**",
            "/swagger-ui.html",
            // 3-1·3-2·3-3 은 인증 불필요. 3-4 로그아웃은 Bearer 필요이므로 제외한다.
            "/api/v1/auth/register",
            "/api/v1/auth/login",
            "/api/v1/auth/refresh"
    };

    private static final String[] API_URLS = {
    };

    @Bean
    public SecurityFilterChain filterChain(HttpSecurity http) throws Exception {
        http
                .cors(cors -> cors.configurationSource(corsConfigurationSource))
                // CSRF 보호 비활성화:
                // 1. 세션 기반이 아닌, JWT 기반의 Stateless 인증을 사용함.
                // 2. Access Token은 헤더(Authorization)에 담겨 전송되므로 브라우저 자동 전송에 의한 공격에서 안전함.
                // 3. Refresh Token은 쿠키에 담기지만, SameSite=Lax 설정을 통해 CSRF를 방어하고 있음.
                .csrf(AbstractHttpConfigurer::disable)
                .sessionManagement(sm -> sm.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
                .formLogin(AbstractHttpConfigurer::disable)
                .httpBasic(AbstractHttpConfigurer::disable)
                .headers(
                        headers -> headers
                                // TODO 배포 시, HSTS 활성화 검토
//                                .httpStrictTransportSecurity(
//                                        hsts -> hsts
//                                                .includeSubDomains(true)
//                                                .maxAgeInSeconds(31536000)
//                                )
                                .frameOptions(HeadersConfigurer.FrameOptionsConfig::sameOrigin)
                                .contentTypeOptions(Customizer.withDefaults())
                                .cacheControl(HeadersConfigurer.CacheControlConfig::disable)
//                                .contentSecurityPolicy(csp -> csp
//                                        .policyDirectives("default-src 'self'; frame-ancestors 'self'")
//                                )
                                .referrerPolicy(rp -> rp
                                        .policy(ReferrerPolicyHeaderWriter.ReferrerPolicy.NO_REFERRER)
                                )
                                .permissionsPolicyHeader(pp -> pp
                                        .policy("geolocation=(), microphone=(), camera=()")
                                )

                )
                .authorizeHttpRequests(auth -> auth
                        .requestMatchers(PUBLIC_URLS).permitAll()
                        .requestMatchers(PathRequest.toStaticResources().atCommonLocations()).permitAll()
                        // L4/K8s 헬스체크는 인증 헤더를 못 보내므로 health(liveness/readiness)만 공개하고 나머지 actuator는 ADMIN으로 제한
                        .requestMatchers("/actuator/health/**").permitAll()
                        .requestMatchers("/actuator/**").hasRole("ADMIN")
                        .requestMatchers(API_URLS).authenticated()
                        .anyRequest().authenticated()
                )
                .exceptionHandling(securityExceptionConfig::configure)
                .addFilterBefore(jwtAuthenticationFilter, UsernamePasswordAuthenticationFilter.class)
                .addFilterBefore(securityAuditLogger, JwtAuthenticationFilter.class)
        ;

        return http.build();
    }
}
