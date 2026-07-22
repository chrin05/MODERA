# 로컬 개발 인프라 (local-infra)

modera 백엔드를 **완전 로컬**로 돌리기 위한 인프라입니다.
PostgreSQL(pgvector+pg_bigm) · Redis · MinIO 를 각자 PC에 컨테이너로 띄웁니다.

> **서버(pem·터널) 없이** 개발·테스트할 수 있습니다. 데이터도 각자 격리됩니다.
> 서버 배포용 인프라는 이것이 아니라 서버의 `~/infra` 입니다.

---

## 사전 준비물

- Docker / Docker Desktop
- JDK 21

---

## 1) 인프라 띄우기

```bash
cd backend/local-infra
cp .env.example .env        # 값 채우기 (로컬용이라 기본값 그대로도 OK)
docker compose up -d --build
```

처음 실행 시 postgres 이미지를 빌드하므로 몇 분 걸립니다(pg_bigm 소스 빌드).

**확인:**
```bash
docker compose ps           # postgres/redis/minio 가 healthy 인지
```
- MinIO 콘솔: http://localhost:19001 (계정: .env 의 MINIO_ROOT_USER / PASSWORD)
- 버킷 `pictures`, `thumbnails`, `modera` 가 자동 생성됨

---

## 2) 앱 실행

인프라와 **별개로**, 앱은 환경변수를 주입해서 실행합니다.
(비밀값은 코드/파일에 넣지 않습니다.)

```bash
cd ..    # backend/ 로

JWT_SECRET=$(openssl rand -base64 32) \
DB_PASSWORD=localdev \
S3_ACCESS_KEY=minioadmin \
S3_SECRET_KEY=minioadmin \
./gradlew bootRun
```

- `DB_PASSWORD` = 인프라 `.env` 의 `POSTGRES_PASSWORD` 와 동일하게
- `S3_ACCESS_KEY` / `S3_SECRET_KEY` = 인프라 `.env` 의 `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` 와 동일하게
- `JWT_SECRET` 은 로컬 검증용이라 매번 새로 생성해도 무방

> 나머지 접속 정보(DB_HOST, S3_ENDPOINT 등)는 application-local.yml 의
> 기본값이 localhost 라 별도 주입 없이 로컬 인프라에 붙습니다.

앱이 뜨면 Liquibase 가 자동으로 스키마(001-init-schema.sql)를 **로컬 PG** 에 적용합니다.

---

## 3) 동작 확인

- Swagger: http://localhost:8080/swagger-ui.html
- 회원가입 → 로그인 → (업로드 작업 시) presigned 발급 테스트

---

## 자주 겪는 문제

| 증상 | 원인 / 해결 |
|---|---|
| `password authentication failed` | `DB_PASSWORD` 가 인프라 `.env` 의 `POSTGRES_PASSWORD` 와 다름 |
| S3/MinIO 인증 실패 | `S3_ACCESS_KEY/SECRET_KEY` 가 `MINIO_ROOT_USER/PASSWORD` 와 다름 |
| 포트 충돌(5432 등) | 기존에 로컬 PG/Redis 를 쓰고 있거나, **DB 터널이 열려 있음** → 터널 끄기 |
| `validate` 스키마 오류 | 엔티티와 스키마 불일치 — 스키마 변경 시 발생, 팀에 공유 |
| pg_bigm 빌드 실패 | Docker 네트워크/자원 확인 후 `docker compose build --no-cache postgres` |

---

## 정리(초기화)

```bash
docker compose down          # 컨테이너만 내림 (데이터 유지)
docker compose down -v       # 볼륨까지 삭제 (DB·MinIO 데이터 완전 초기화)
```

`down -v` 후 다시 `up` 하면 빈 상태에서 시작합니다(스키마는 bootRun 시 재적용).
