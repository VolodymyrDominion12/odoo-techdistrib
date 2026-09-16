#!/usr/bin/env bash
# =============================================================================
#  load_demo.sh — завантажити демонстраційні дані TechDistrib
# =============================================================================
#  Використання:
#      ./scripts/load_demo.sh              # база techdistrib
#      DB_NAME=інша_база ./scripts/load_demo.sh
#
#  Скрипт ідемпотентний: повторний запуск не створить дублів.
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DB_NAME="${DB_NAME:-techdistrib}"

if ! docker exec techdistrib_db pg_isready -U odoo -d postgres >/dev/null 2>&1; then
    echo "❌ PostgreSQL не відповідає. Запусти:  docker compose up -d"
    exit 1
fi

echo "📦 Завантажую демо-дані в базу '$DB_NAME' ..."
echo

# odoo shell читає скрипт зі stdin і виконує його у своєму середовищі.
# Змінна `env` доступна автоматично.
"$ROOT/scripts/odoo_shell.sh" -d "$DB_NAME" < "$ROOT/scripts/load_demo_data.py"
