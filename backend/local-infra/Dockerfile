FROM pgvector/pgvector:pg16
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential git ca-certificates postgresql-server-dev-16 \
 && git clone --depth 1 https://github.com/pgbigm/pg_bigm.git /tmp/pg_bigm \
 && make -C /tmp/pg_bigm USE_PGXS=1 install \
 && apt-get purge -y --auto-remove build-essential git postgresql-server-dev-16 \
 && rm -rf /var/lib/apt/lists/* /tmp/pg_bigm
