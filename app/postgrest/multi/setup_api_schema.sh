#!/usr/bin/env bash
# 方案 X: 用 api schema 隔离 public 中的扩展危险函数
# 将 public 业务表移动到 api schema，只暴露 api 给 PostgREST
set -e
OUT=/tmp/setup_api_schema.log
: > "$OUT"
DBS="appdb $(seq -f 'pg%g' 1 100)"

for db in $DBS; do
  echo "=== $db ===" | tee -a "$OUT"
  sudo -u postgres psql -h /var/run/postgresql -p 5432 -d "$db" >>"$OUT" 2>&1 <<'SQL'
-- 1. 创建 api schema
CREATE SCHEMA IF NOT EXISTS api;

-- 2. 将 public 下的用户基表移动到 api schema（排除已存在的同名表）
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT c.relname AS t
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind = 'r'
      AND NOT EXISTS (SELECT 1 FROM pg_class c2 JOIN pg_namespace n2 ON n2.oid=c2.relnamespace
                      WHERE n2.nspname='api' AND c2.relname=c.relname AND c2.relkind='r')
  LOOP
    EXECUTE format('ALTER TABLE public.%I SET SCHEMA api', r.t);
  END LOOP;
END $$;

-- 3. 授权 dbuser_app 使用并访问 api schema 下的对象
GRANT USAGE ON SCHEMA api TO dbuser_app;
GRANT ALL ON ALL TABLES IN SCHEMA api TO dbuser_app;
GRANT ALL ON ALL SEQUENCES IN SCHEMA api TO dbuser_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA api GRANT ALL ON TABLES TO dbuser_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA api GRANT ALL ON SEQUENCES TO dbuser_app;

-- 4. 在 api schema 部署安全的 create_table RPC（建表落在 api）
CREATE OR REPLACE FUNCTION api.create_table(table_name text, columns text)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE sql text;
BEGIN
  IF table_name !~ '^[a-zA-Z_][a-zA-Z0-9_]*$' THEN
    RETURN jsonb_build_object('ok', false, 'error', 'invalid table_name');
  END IF;
  sql := format('CREATE TABLE IF NOT EXISTS api.%I (%s)', table_name, columns);
  EXECUTE sql;
  PERFORM pg_notify('pgrst', 'reload schema');
  RETURN jsonb_build_object('ok', true, 'table', table_name, 'schema', 'api');
END; $$;

-- 5. 刷新 PostgREST schema cache
NOTIFY pgrst, 'reload schema';
SQL
done
echo "DONE -> $OUT"
