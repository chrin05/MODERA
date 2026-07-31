package com.ssafy.modera.api.domain.schedule.service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.ssafy.modera.api.domain.library.entity.ImageSchedule;
import com.ssafy.modera.api.domain.library.entity.UserSchedule;
import com.ssafy.modera.api.domain.library.repository.ImageScheduleRepository;
import com.ssafy.modera.api.domain.library.repository.UserScheduleRepository;
import com.ssafy.modera.api.domain.schedule.entity.Schedule;
import com.ssafy.modera.api.domain.schedule.repository.ScheduleQueryRepository;
import com.ssafy.modera.api.domain.schedule.repository.ScheduleRepository;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;

/**
 * 분석 완료 결과의 scheduleData로 일정 후보(schedule + user_schedule + image_schedule +
 * user_schedule_view)를 생성한다. 명세서 9장: 일정은 분석 결과로만 생성되고 별도 생성 API가 없다.
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class ScheduleCreationService {

    private static final String TYPE_SCHEDULE = "schedule";
    private static final int TITLE_MAX_LENGTH = 100;

    private final ScheduleRepository scheduleRepository;
    private final UserScheduleRepository userScheduleRepository;
    private final ImageScheduleRepository imageScheduleRepository;
    private final ScheduleQueryRepository scheduleQueryRepository;
    private final ObjectMapper objectMapper;

    /**
     * 구조화 타입이 일정이 아니면 아무것도 하지 않는다. 같은 이미지에 활성 일정이 이미 있으면
     * 생성을 건너뛴다(재분석·at-least-once 중복 수신 대비). 시각 파싱 실패는 "추출 실패"로
     * 간주해 날짜 없는 후보로 만든다 — 여기서 예외를 던지면 컨슈머가 일시 오류로 분류해
     * 같은 이벤트를 계속 재전달받기 때문에 필드 문제로는 실패하지 않는다.
     */
    @Transactional
    public void createFromAnalysis(
            Integer userId,
            Integer imageId,
            String title,
            String structuredType,
            String structuredFieldsJson
    ) {
        if (structuredType == null || !TYPE_SCHEDULE.equalsIgnoreCase(structuredType.trim())) {
            return;
        }
        if (scheduleQueryRepository.existsActiveScheduleForImage(userId, imageId)) {
            log.info("이미 활성 일정이 있는 이미지라 일정 생성을 건너뜀: imageId={}", imageId);
            return;
        }

        JsonNode fields = parseFields(imageId, structuredFieldsJson);
        OffsetDateTime startAt = ScheduleTimeParser.toDateTime(
                fields, "startYear", "startMonth", "startDay", "startTime");
        OffsetDateTime endAt = ScheduleTimeParser.toDateTime(
                fields, "endYear", "endMonth", "endDay", "endTime");
        if (startAt != null && endAt != null && endAt.isBefore(startAt)) {
            // schedule 테이블의 CHECK(end_at >= start_at)를 지키기 위해 종료 시각을 버린다.
            log.warn("일정 종료가 시작보다 빨라 종료 시각을 무시한다: imageId={} start={} end={}",
                    imageId, startAt, endAt);
            endAt = null;
        }

        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        Schedule schedule = scheduleRepository.save(Schedule.builder()
                .title(normalizeTitle(title))
                .startAt(startAt)
                .endAt(endAt)
                .updatedAt(now)
                .build());
        userScheduleRepository.save(UserSchedule.builder()
                .userId(userId)
                .scheduleId(schedule.getScheduleId())
                .updatedAt(now)
                .build());
        imageScheduleRepository.save(ImageSchedule.builder()
                .imageId(imageId)
                .scheduleId(schedule.getScheduleId())
                .build());
        scheduleQueryRepository.insertView(
                userId, schedule.getScheduleId(), imageId, schedule.getTitle(), startAt, endAt, now);
        log.info("분석 결과로 일정 후보 생성: imageId={} scheduleId={} startAt={} endAt={}",
                imageId, schedule.getScheduleId(), startAt, endAt);
    }

    private String normalizeTitle(String title) {
        String value = title == null || title.isBlank() ? "일정" : title.trim();
        return value.length() <= TITLE_MAX_LENGTH ? value : value.substring(0, TITLE_MAX_LENGTH);
    }

    private JsonNode parseFields(Integer imageId, String structuredFieldsJson) {
        if (structuredFieldsJson == null || structuredFieldsJson.isBlank()) {
            return null;
        }
        try {
            return objectMapper.readTree(structuredFieldsJson);
        } catch (JsonProcessingException exception) {
            log.warn("일정 fields JSON 파싱 실패 — 날짜 없는 후보로 생성: imageId={}", imageId, exception);
            return null;
        }
    }
}
