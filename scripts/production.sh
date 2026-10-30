#!/usr/bin/env bash
# Always target the independent production project, never the demo or local compose stack.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
config_file="${GRAMASK_PRODUCTION_ENV:-.env.production}"
if [[ ! -f "$config_file" ]]; then
  echo "Copy deploy/production.env.example to .env.production and set your domain and email." >&2
  exit 1
fi
compose=(docker compose --project-name gramask-production --env-file "$config_file" -f compose.production.yaml)
case "${1:-help}" in
  check)
    "${compose[@]}" config --quiet
    "${compose[@]}" run --rm --no-deps proxy caddy validate --config /etc/caddy/Caddyfile
    ;;
  deploy)
    "${compose[@]}" config --quiet
    "${compose[@]}" build app
    "${compose[@]}" up -d
    "${compose[@]}" ps
    echo "Model download may still be running. Use 'bash scripts/production.sh status' to check readiness."
    ;;
  status)
    "${compose[@]}" ps
    "${compose[@]}" exec -T app python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/readyz",timeout=10).read().decode())'
    ;;
  logs) "${compose[@]}" logs --tail=100 -f app proxy ollama ;;
  smoke) "${compose[@]}" exec -T app python scripts/smoke_live.py --timeout 300 ;;
  backup)
    umask 077
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    filename="gramask-${stamp}.db"
    mkdir -p backups
    "${compose[@]}" exec -T app python scripts/backup_db.py --destination "/app/data/backups/$filename"
    "${compose[@]}" cp "app:/app/data/backups/$filename" "backups/$filename"
    chmod 600 "backups/$filename"
    echo "Verified backup: backups/$filename. Copy this file to protected off-server storage."
    ;;
  *) echo 'Usage: bash scripts/production.sh {check|deploy|status|logs|smoke|backup}' ;;
esac
