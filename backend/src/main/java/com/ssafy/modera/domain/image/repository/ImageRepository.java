package com.ssafy.modera.domain.image.repository;

import com.ssafy.modera.domain.image.entity.Image;
import org.springframework.data.jpa.repository.JpaRepository;

public interface ImageRepository extends JpaRepository<Image, Long> {
}
