#!/usr/bin/env bash
set -euo pipefail

cd /opt/postpilot
backup_dir=/var/backups/postpilot
install -d -m 700 "$backup_dir"

backup_file="$backup_dir/postpilot-$(date +%F).sql.gz"
temporary_file=$(mktemp "$backup_dir/.postpilot-$(date +%F).XXXXXX")
trap 'rm -f "$temporary_file"' EXIT

docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "$temporary_file"
gzip -t "$temporary_file"
mv "$temporary_file" "$backup_file"
trap - EXIT

printf 'Database backup written to %s\n' "$backup_file"
