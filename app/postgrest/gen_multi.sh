#!/usr/bin/env bash
set -e
OUT=/root/pigsty/app/postgrest/docker-compose.multi.yml
APP_USER=dbuser_app
APP_PWD=F1LKkyTDilEyJTDE7hjTGBPu
PGHOST=217.69.2.217
JWT=0ZMG3j7g5y5evEsq1k8kdQ9J5MVCPTmbkvBVAR1N6eB
BASE=3100   # pg1 -> 3101 to avoid clash with appdb(3001)
{
echo "# Auto-generated: 100 PostgREST workers (pg1..pg100)"
echo "# port = 3100 + n  (appdb keeps 3001)"
echo "# docs/pigsty-postgrest-nginx-api.md §17"
echo "services:"
for n in $(seq 1 100); do
  port=$((BASE + n))
  db="pg${n}"
  cat <<EOF
  postgrest_${db}:
    container_name: postgrest_${db}
    image: postgrest/postgrest
    restart: always
    environment:
      PGRST_DB_URI: postgres://${APP_USER}:${APP_PWD}@${PGHOST}:5432/${db}
      PGRST_DB_SCHEMA: public
      PGRST_DB_ANON_ROLE: ${APP_USER}
      PGRST_SERVER_PORT: ${port}
      PGRST_JWT_SECRET: ${JWT}
      PGRST_DB_CHANNEL: pgrst
      PGRST_DB_CHANNEL_ENABLED: "true"
      PGRST_DB_POOL: "2"
    ports:
      - "127.0.0.1:${port}:${port}"
EOF
done
} > "$OUT"
echo "generated $OUT"; wc -l "$OUT"
