#!/bin/bash
# First-start only (docker-entrypoint-initdb.d): create least-privilege roles and the database.
# The bootstrap superuser is used here and nowhere else; no service connects as superuser.
set -euo pipefail
read_secret() { tr -d '\r\n' < "/run/secrets/$1"; }
OWNER_PW=$(read_secret db_owner_password)
APP_PW=$(read_secret db_app_password)
BACKUP_PW=$(read_secret db_backup_password)
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
     -v owner_pw="$OWNER_PW" -v app_pw="$APP_PW" -v backup_pw="$BACKUP_PW" <<'SQL'
CREATE ROLE memory_owner  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'owner_pw';
CREATE ROLE memory_app    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'app_pw';
CREATE ROLE memory_backup LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'backup_pw';
CREATE DATABASE memory OWNER memory_owner ENCODING 'UTF8' TEMPLATE template0;
REVOKE ALL ON DATABASE memory FROM PUBLIC;
GRANT CONNECT ON DATABASE memory TO memory_app, memory_backup;
SQL
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname memory <<'SQL'
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO memory_owner;
SQL
