package com.ssafy.modera.api.domain.schedule.service;

import com.fasterxml.jackson.databind.JsonNode;

import java.time.DateTimeException;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.LocalTime;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;

/**
 * AI가 추출한 일정 fields(startYear/…/endTime)를 시각으로 해석한다.
 * 일정 생성(ScheduleCreationService)과 이미지 상세 응답 변환(ImageQueryService)이
 * 같은 해석을 공유한다 — 복사하면 한쪽만 고쳐져 "생성된 일정과 상세 화면의 시각이
 * 다른" 사고가 난다.
 */
public final class ScheduleTimeParser {

    /** AI가 일정 시각을 한국 로컬 시각(예: "18:00")으로 추출하므로 이 시간대로 해석한다. */
    private static final ZoneId KOREA_ZONE = ZoneId.of("Asia/Seoul");
    /** "9:30"처럼 시(hour)가 한 자리로 와도 받는다. */
    private static final DateTimeFormatter TIME_FORMAT = DateTimeFormatter.ofPattern("H:mm[:ss]");

    private ScheduleTimeParser() {
    }

    /**
     * 년·월·일 중 하나라도 없거나 유효하지 않은 날짜면 null. 시각이 없으면 자정으로 본다.
     * 숫자가 "2026"처럼 문자열로 와도 받는다.
     */
    public static OffsetDateTime toDateTime(
            JsonNode fields, String yearKey, String monthKey, String dayKey, String timeKey) {
        if (fields == null) {
            return null;
        }
        Integer year = asInteger(fields.get(yearKey));
        Integer month = asInteger(fields.get(monthKey));
        Integer day = asInteger(fields.get(dayKey));
        if (year == null || month == null || day == null) {
            return null;
        }
        LocalTime time = asTime(fields.get(timeKey));
        try {
            return LocalDateTime.of(
                            LocalDate.of(year, month, day),
                            time == null ? LocalTime.MIDNIGHT : time)
                    .atZone(KOREA_ZONE)
                    .toOffsetDateTime();
        } catch (DateTimeException exception) {
            return null;
        }
    }

    private static Integer asInteger(JsonNode node) {
        if (node == null || node.isNull()) {
            return null;
        }
        if (node.canConvertToInt()) {
            return node.asInt();
        }
        if (node.isTextual()) {
            try {
                return Integer.parseInt(node.asText().trim());
            } catch (NumberFormatException exception) {
                return null;
            }
        }
        return null;
    }

    private static LocalTime asTime(JsonNode node) {
        if (node == null || !node.isTextual() || node.asText().isBlank()) {
            return null;
        }
        try {
            return LocalTime.parse(node.asText().trim(), TIME_FORMAT);
        } catch (DateTimeParseException exception) {
            return null;
        }
    }
}
