#!/usr/bin/env bash
# Back up the Plujka PostgreSQL database with pg_dump (custom format, compressed).
#
# Usage:
#   DATABASE_URL=postgresql://plujka:...@host:5432/plujka scripts/backup_db.sh [backup_dir]
#
# Environment:
#   DATABASE_URL   owner connection (default: local docker-compose database)
#   BACKUP_KEEP    number of newest dumps to keep in backup_dir (default 7; 0 keeps all)
#   BACKUP_SCHEMAS space-separated schemas to dump (default: everything). The mart schema can be
#                  rebuilt from parquet with scripts/build_mart.py, so "public" alone is enough when
#                  only logs, feedback and legacy tables matter.
#
# Restore (into an empty database):
#   pg_restore --clean --if-exists --no-owner -d "$DATABASE_URL" backups/plujka-YYYYmmdd-HHMMSS.dump
# The read-only role plujka_mart_reader is not part of a dump; scripts/build_mart.py recreates it.
set -euo pipefail

DATABASE_URL="${DATABASE_URL:-postgresql://plujka:plujka@localhost:5432/plujka}"
BACKUP_DIR="${1:-backups}"
BACKUP_KEEP="${BACKUP_KEEP:-7}"

mkdir -p "$BACKUP_DIR"
target="$BACKUP_DIR/plujka-$(date -u +%Y%m%d-%H%M%S).dump"
schema_args=()
for schema in ${BACKUP_SCHEMAS:-}; do
  schema_args+=(--schema "$schema")
done

# Write to a temporary name first so an interrupted dump never looks like a valid backup.
pg_dump --format=custom --no-owner "${schema_args[@]}" --file "$target.partial" "$DATABASE_URL"
mv "$target.partial" "$target"
echo "backup written: $target ($(du -h "$target" | cut -f1))"

if [ "$BACKUP_KEEP" -gt 0 ]; then
  ls -1t "$BACKUP_DIR"/plujka-*.dump | tail -n +"$((BACKUP_KEEP + 1))" | while read -r old; do
    rm -f -- "$old"
    echo "removed old backup: $old"
  done
fi
