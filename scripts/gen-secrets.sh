#!/bin/bash
# Generates deployment secrets into ./secrets (directory 0700, gitignored). Refuses to overwrite existing files.
# Nothing here is a default: every value is random and unique to this installation.
set -euo pipefail
dir="${1:-secrets}"
umask 077
mkdir -p "$dir"
chmod 700 "$dir"
gen() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
put() { # name value
  if [ -e "$dir/$1" ]; then echo "keep   $dir/$1 (exists)"; return; fi
  # 0644 inside a 0700 directory: Compose bind-mounts keep host modes and the containers run as non-root users.
  printf '%s' "$2" > "$dir/$1"; chmod 644 "$dir/$1"; echo "create $dir/$1"
}
db_super=$(gen); db_owner=$(gen); db_app=$(gen); db_backup=$(gen)
for n in db_superuser_password db_owner_password db_app_password db_backup_password; do
  case $n in db_superuser_password) v=$db_super;; db_owner_password) v=$db_owner;;
             db_app_password) v=$db_app;; db_backup_password) v=$db_backup;; esac
  put "$n" "$v"
done
# DSNs embed the password: derive from the files so an existing secret stays consistent.
dsn() { printf 'postgresql://%s:%s@db:5432/memory' "$1" "$(cat "$dir/$2")"; }
put database_url "$(dsn memory_app db_app_password)"
put migration_database_url "$(dsn memory_owner db_owner_password)"
put backup_database_url "$(dsn memory_backup db_backup_password)"
put hmac_key "$(gen)$(gen)"
put backup_passphrase "$(gen)"
echo "done. Back up hmac_key and backup_passphrase SEPARATELY from the data backups (see docs/operations/)."
