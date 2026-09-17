#!/usr/bin/env bash
# =============================================================================
#  init_db.sh — створення бази даних techdistrib зі стандартними модулями
# =============================================================================
#  Використання:
#      ./scripts/init_db.sh              # створити (або оновити) базу techdistrib
#      ./scripts/init_db.sh --fresh      # ВИДАЛИТИ і створити заново
#      ./scripts/init_db.sh --force      # обійти перевірку «сервер уже працює»
#      ./scripts/init_db.sh --help       # коротка довідка
#
#  ВАЖЛИВО: спершу зупини сервер, і лише потім запускай цей скрипт. Чому саме
#  так — див. блок «Захист» нижче (там описаний реальний інцидент).
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
FORCE="${FORCE:-0}"

# --- Розбір аргументів -------------------------------------------------------
# Невідомий аргумент — помилка, а не тихе ігнорування: одруківка "--freesh"
# інакше означала б «звичайна ініціалізація», і ти б не зрозумів, чому база
# не перестворилась.
for arg in "$@"; do
    case "$arg" in
        --fresh) FRESH=1 ;;
        --force) FORCE=1 ;;
        -h|--help)
            echo "Використання: ./scripts/init_db.sh [--fresh] [--force]"
            echo
            echo "  --fresh   видалити базу $DB_NAME і створити заново (ДАНІ ЗНИКНУТЬ)"
            echo "  --force   не зупинятись, навіть якщо на базі працює Odoo (небезпечно)"
            exit 0
            ;;
        *)
            echo "❌ Невідомий аргумент: $arg"
            echo "   Дозволені: --fresh, --force, --help"
            exit 2
            ;;
    esac
done

# --- Перевірка, що Postgres живий --------------------------------------------
if ! docker exec techdistrib_db pg_isready -U odoo -d postgres >/dev/null 2>&1; then
    echo "❌ PostgreSQL не відповідає. Запусти:  docker compose up -d"
    exit 1
fi

# --- Захист: на цій базі не має працювати інший Odoo -------------------------
# Реальний випадок 2026-09-17: --fresh запустили, коли dev-сервер уже слухав
# 8069. DROP DATABASE зніс базу під живим процесом; сервер побачив зміну
# signaling, перезавантажив реєстр моделей у ту саму секунду, коли нової
# ir_module_module ще не існувало, закешував ПОРОЖНІЙ реєстр і почав віддавати
# 500 «KeyError: ir.http», доки ініціалізація не завершилась і не штовхнула
# base_registry_signaling (див. vendor/odoo/odoo/modules/loading.py:393-396 та
# vendor/odoo/odoo/modules/registry.py:135-136, 898-900).
#
# Тому перевіряємо три незалежні ознаки живого Odoo. Достатньо однієї, щоб
# зупинитись: краще зайвий раз попросити зупинити сервер, ніж знести базу
# з-під нього.

# Значення ключа з odoo.conf: cfg_value <файл> <ключ>.
# Саме тому порт і ім'я бази не дублюються в скрипті — якщо конфіг зміниться,
# перевірка не почне тихо брехати.
cfg_value() {
    { grep -E "^[[:space:]]*$2[[:space:]]*=" "$1" 2>/dev/null || true; } |
        tail -1 | cut -d= -f2 | tr -d '[:space:]'
}

HTTP_PORT="$(cfg_value "$ROOT/config/odoo.conf" http_port)"
HTTP_PORT="${HTTP_PORT:-8069}"

# 1) Процеси odoo-bin цього проєкту, які працюють із НАШОЮ базою.
#    База процесу визначається так: явний -d/--database має пріоритет, інакше
#    беремо db_name із конфіга, який цьому процесу передали. Так окремий
#    сервер на techdistrib_test не блокує ініціалізацію techdistrib (і навпаки).
LIVE_PIDS=""
while read -r pid; do
    [[ -z "$pid" ]] && continue
    [[ "$pid" == "$$" || "$pid" == "$PPID" ]] && continue
    cmdline="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
    db_arg=""
    if [[ "$cmdline" =~ (^|[[:space:]])(-d|--database)(=|[[:space:]])([^[:space:]]+) ]]; then
        db_arg="${BASH_REMATCH[4]}"
    elif [[ "$cmdline" =~ (^|[[:space:]])(-c|--config)(=|[[:space:]])([^[:space:]]+) ]]; then
        cfg="${BASH_REMATCH[4]}"
        [[ -r "$cfg" ]] && db_arg="$(cfg_value "$cfg" db_name)"
    fi
    # -d приймає список через кому: "-d techdistrib,techdistrib_test"
    [[ ",$db_arg," == *",$DB_NAME,"* ]] || continue
    LIVE_PIDS="${LIVE_PIDS}${LIVE_PIDS:+ }$pid"
done < <(pgrep -f -- "vendor/odoo/odoo-bin" 2>/dev/null || true)

# 2) Хтось слухає наш http_port. Це може бути наш сервер, а може й чужий
#    процес — в обох випадках ініціалізувати базу «наосліп» не варто.
#    /dev/tcp — вбудована можливість bash, тому lsof/netstat не потрібні.
PORT_BUSY=0
if (exec 3<>"/dev/tcp/127.0.0.1/$HTTP_PORT") 2>/dev/null; then
    PORT_BUSY=1
fi

# 3) Живі з'єднання до самої бази. Ловить те, що не видно в процесах: сервер
#    із іншим конфігом, cron-воркер, забуту сесію odoo shell.
DB_CONNS="$(
    docker exec techdistrib_db psql -U odoo -d postgres -tAc \
        "SELECT count(*) FROM pg_stat_activity WHERE datname = '$DB_NAME';" 2>/dev/null || true
)"
DB_CONNS="${DB_CONNS//[[:space:]]/}"
[[ "$DB_CONNS" =~ ^[0-9]+$ ]] || DB_CONNS=0

PROBLEMS=""
if [[ -n "$LIVE_PIDS" ]]; then
    PROBLEMS+="   • процеси odoo-bin на цій базі: $LIVE_PIDS"$'\n'
fi
if [[ "$PORT_BUSY" == "1" ]]; then
    PROBLEMS+="   • порт $HTTP_PORT зайнятий (хтось віддає веб)"$'\n'
fi
if [[ "$DB_CONNS" -gt 0 ]]; then
    PROBLEMS+="   • активних з'єднань із базою $DB_NAME: $DB_CONNS"$'\n'
fi

if [[ -n "$PROBLEMS" && "$FORCE" != "1" ]]; then
    echo "⛔ Зупинено: база $DB_NAME зараз у роботі."
    echo
    echo "   Знайдено:"
    printf '%s' "$PROBLEMS"
    echo
    if [[ $FRESH -eq 1 ]]; then
        echo "   --fresh робить DROP DATABASE + rm -rf data/filestore/$DB_NAME."
        echo "   Під працюючим сервером це означає: дані зникають, а сервер сам"
        echo "   перезавантажує реєстр моделей у момент, коли база ще порожня —"
        echo "   і віддає 500 «KeyError: ir.http», доки інший процес не закінчить"
        echo "   ініціалізацію (саме це сталось 2026-09-17)."
    else
        echo "   Навіть без --fresh інсталяція модулів під живим сервером смикає"
        echo "   ir_module_module: сервер ловить ті самі 500 і блокування таблиць."
    fi
    echo
    echo "   Що робити:"
    echo "     1) зупини сервер — Ctrl+C у терміналі, де працює ./scripts/run_odoo.sh"
    if [[ -n "$LIVE_PIDS" ]]; then
        echo "        (або: kill $LIVE_PIDS)"
    fi
    FRESH_HINT=""
    if [[ $FRESH -eq 1 ]]; then
        FRESH_HINT=" --fresh"
    fi
    echo "     2) ./scripts/init_db.sh$FRESH_HINT"
    echo "     3) ./scripts/run_odoo.sh"
    echo
    echo "   Впевнений, що це хибне спрацювання?  ./scripts/init_db.sh --force"
    exit 1
fi

# --- Свіжий старт ------------------------------------------------------------
if [[ $FRESH -eq 1 ]]; then
    echo "🗑️  Видаляю базу $DB_NAME ..."
    # dropdb через psql: спершу рвемо всі з'єднання, інакше DROP DATABASE
    # впаде з "database is being accessed by other users".
    # (Перевірка вище вже мала це відсіяти — тут страховка на випадок
    #  з'єднання, що відкрилось між перевіркою і drop.)
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
