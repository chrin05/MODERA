--liquibase formatted sql

--changeset modera:061-image-client-request-id
ALTER TABLE image
    ADD COLUMN client_request_id VARCHAR(100);

UPDATE image
SET client_request_id = 'legacy-' || image_id
WHERE client_request_id IS NULL;

ALTER TABLE image
    ALTER COLUMN client_request_id SET NOT NULL;

CREATE UNIQUE INDEX uq_image_user_client_request
    ON image (user_id, client_request_id);

--rollback DROP INDEX uq_image_user_client_request;
--rollback ALTER TABLE image DROP COLUMN client_request_id;
