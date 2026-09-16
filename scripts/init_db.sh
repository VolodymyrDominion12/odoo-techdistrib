#!/usr/bin/env bash
# =============================================================================
#  init_db.sh — створення бази даних techdistrib зі стандартними модулями
# =============================================================================
#  Використання:
#      ./scripts/init_db.sh              # створити (або оновити) базу techdistrib
#      ./scripts/init_db.sh --fresh      # ВИДАЛИТИ і створити заново
#
#  Як Odoo створює базу:
#  Окремої «команди створити базу» немає. Ти передаєш -d назва -i модулі,
#  і Odoo сам розуміє: бази немає → треба створити й встановити ці модулі.
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DB_NAME="${DB_NAME:-techdistrib}"
FRESH=0
[[ "${1:-}" == "--fresh" ]] && FRESH=1

# --- Перевірка, що Postgres живий --------------------------------------------
if ! docker exec techdistrib_db pg_isready -U odoo -d postgres >/dev/null 2>&1; then
    echo "❌ PostgreSQL не відповідає. Запусти:  docker compose up -d"
    exit 1
fi

# --- Свіжий старт ------------------------------------------------------------
if [[ $FRESH -eq 1 ]]; then
    echo "🗑️  Видаляю базу $DB_NAME ..."
    # dropdb через psql: спершу рвемо всі з'єднання, інакше DROP DATABASE
    # впаде з "database is being accessed by other users".
    PGPASSWORD=odoo psql -h 127.0.0.1 -p 5437 -U odoo -d postgres -c \
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$DB_NAME' AND pid <> pg_backend_pid();" >/dev/null
    PGPASSWORD=odoo psql -h 127.0.0.1 -p 5437 -U odoo -d postgres -c \
        "DROP DATABASE IF EXISTS \"$DB_NAME\";" >/dev/null
    rm -rf "$ROOT/data/filestore/$DB_NAME"
    echo "✅ Базу видалено."
fi

# --- Список модулів для встановлення -----------------------------------------
# Це «ядро» нашого ERP. Розберемо, що кожен дає (і що тягне за собою):
#
#   base             — фундамент: партнери, компанії, користувачі, права,
#                      валюти, країни. Ставиться завжди і першим.
#   contacts         — розширює картку партнера: адреси, кілька контактних осіб.
#   sale_management  — продажі: комерційні пропозиції → замовлення → рахунки.
#                      Тягне за собою product (номенклатура) і account.
#   purchase         — закупівлі: замовлення постачальнику → прихід → рахунок.
#   stock            — склад: локації, переміщення, серійні номери, партії.
#   sale_stock       — МІСТ між продажем і складом: саме він створює
#                      автоматичне відвантаження при підтвердженні замовлення.
#   purchase_stock   — міст між закупівлею і складом (приймання).
#   account          — бухгалтерія/інвойсинг: рахунки, платежі, дебіторка.
#                      Потрібен для кредитних лімітів (ми рахуватимемо борг).
#
# Свідомо НЕ ставимо: mrp (виробництво), hr, pos, project, website —
# щоб база лишалась легкою і зрозумілою.
MODULES="base,contacts,sale_management,purchase,stock,sale_stock,purchase_stock,account"

echo "🏗️  Встановлюю модулі: $MODULES"
echo "    База: $DB_NAME  (перший запуск може тривати 1–3 хвилини)"
echo

# --stop-after-init — критично важливий прапорець: Odoo виконає встановлення
#                     і ЗАВЕРШИТЬСЯ, а не почне слухати порт 8069. Без нього
#                     команда «зависне», і ти подумаєш, що щось зламалось.
# -i                — install: встановити перелічені модулі.
"$ROOT/.venv/bin/python" "$ROOT/vendor/odoo/odoo-bin" \
    --config="$ROOT/config/odoo.conf" \
    -d "$DB_NAME" \
    -i "$MODULES" \
    --without-demo=all \
    --stop-after-init \
    --log-level=warn

echo
echo "✅ База $DB_NAME готова."
echo "   Запусти сервер:  ./scripts/run_odoo.sh"
echo "   Відкрий:         http://localhost:8069"
echo "   Логін:           admin / admin"
