#!/usr/bin/env bash
# =============================================================================
#  run_odoo.sh — запуск сервера Odoo 18 у режимі розробки
# =============================================================================
#  Використання:
#      ./scripts/run_odoo.sh                      # звичайний запуск
#      ./scripts/run_odoo.sh --log-level=debug    # докладні логи
#      ./scripts/run_odoo.sh -u techdistrib_base  # оновити модуль і запуститись
#      ./scripts/run_odoo.sh -i techdistrib_base  # встановити новий модуль
#
#  Що робить скрипт:
#    1. визначає корінь проєкту (працює з будь-якої директорії);
#    2. перевіряє, що venv і база даних готові;
#    3. запускає odoo-bin з нашим конфігом;
#    4. будь-які додаткові аргументи передає Odoo як є.
#
#  set -euo pipefail — «суворий режим» bash:
#    -e          зупинитись на першій же помилці
#    -u          помилка при зверненні до неіснуючої змінної
#    -o pipefail помилка в будь-якій ланці конвеєра робить весь конвеєр невдалим
#  Без цього скрипт «тихо» продовжить роботу після збою — і ти годинами
#  шукатимеш причину дивної поведінки.
# =============================================================================
set -euo pipefail

# --- 1. Визначаємо корінь проєкту --------------------------------------------
# BASH_SOURCE[0] — шлях до цього скрипта. dirname → scripts/,
# потім /.. → корінь проєкту. Так скрипт працює незалежно від поточної теки.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# --- 2. Перевірки перед запуском ---------------------------------------------
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
    echo "❌ Не знайдено venv: $ROOT/.venv/bin/python"
    echo "   Запусти спочатку:  ./scripts/setup_venv.sh"
    exit 1
fi

if [[ ! -f "$ROOT/vendor/odoo/odoo-bin" ]]; then
    echo "❌ Не знайдено код Odoo: $ROOT/vendor/odoo/odoo-bin"
    echo "   Клонуй:  git clone --depth 1 --branch 18.0 https://github.com/odoo/odoo.git vendor/odoo"
    exit 1
fi

# Перевіряємо, чи жива база. Якщо ні — підказуємо команду, а не падаємо мовчки.
if ! docker exec techdistrib_db pg_isready -U odoo -d postgres >/dev/null 2>&1; then
    echo "⚠️  PostgreSQL (techdistrib_db) не відповідає."
    echo "   Запусти:  docker compose up -d"
    echo "   ...продовжую запуск Odoo все одно (можливо, база ось-ось підніметься)."
fi

# --- 3. Робочі директорії ----------------------------------------------------
mkdir -p "$ROOT/data"

# --- 3.1. wkhtmltopdf для друку PDF ------------------------------------------
# Odoo шукає цей бінарник звичайним пошуком у PATH
# (odoo/addons/base/models/ir_actions_report.py:62, find_in_path('wkhtmltopdf')).
#
# У цьому середовищі немає sudo, тому поставити пакунок через apt неможливо.
# Рішення: портативна збірка, розпакована з .deb у vendor/ БЕЗ root —
# командою `dpkg-deb -x` (вона не потребує прав адміністратора).
#
# Якщо тека відсутня — нічого не ламається: Odoo просто не зможе друкувати
# PDF і попередить про це в логах. Інструкція — в README.
WKHTML_DIR="$ROOT/vendor/wkhtmltopdf/extracted/usr/local/bin"
if [[ -x "$WKHTML_DIR/wkhtmltopdf" ]]; then
    export PATH="$WKHTML_DIR:$PATH"
    echo "📄 wkhtmltopdf: $("$WKHTML_DIR/wkhtmltopdf" --version 2>/dev/null | head -1)"
else
    echo "ℹ️  wkhtmltopdf не знайдено — друк PDF буде недоступний (див. README)."
fi

# --- 4. Запуск ---------------------------------------------------------------
# exec замінює процес bash на процес python: сигнали (Ctrl+C) ідуть напряму
# в Odoo, і він коректно завершується, а не вбивається.
echo "🚀 Odoo 18 → http://localhost:8069   (Ctrl+C для зупинки)"
exec "$ROOT/.venv/bin/python" "$ROOT/vendor/odoo/odoo-bin" \
    --config="$ROOT/config/odoo.conf" \
    "$@"
