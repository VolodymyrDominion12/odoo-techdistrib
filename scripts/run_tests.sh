#!/usr/bin/env bash
# =============================================================================
#  run_tests.sh — запуск тестів кастомного модуля
# =============================================================================
#  Використання:
#      ./scripts/run_tests.sh techdistrib_credit
#      ./scripts/run_tests.sh techdistrib_warranty:TestWarrantyClaim
#
#  ЯК ПРАЦЮЮТЬ ТЕСТИ В ODOO (це важливо зрозуміти):
#
#  Odoo не використовує pytest «як є». Його тестовий фреймворк:
#    1. створює ОКРЕМУ тестову базу (ім'я з суфіксом, напр. techdistrib_test);
#    2. встановлює туди модуль і всі його залежності;
#    3. проганяє класи з tests/;
#    4. ПІСЛЯ КОЖНОГО тесту відкочує транзакцію (rollback).
#
#  Наслідок, який рятує години життя: тести НЕ бачать даних одне одного,
#  і твоя робоча база techdistrib ніколи не буде зіпсована тестами.
#  Саме тому в тестах НЕ ПОТРІБНО (і не можна) робити env.cr.commit().
#
#  Важливий нюанс: --test-tags фільтрує, ЩО саме запускати.
#      /module_name                 — усі тести модуля
#      /module_name:TestClass       — конкретний клас
#      /module_name:TestClass.method— конкретний метод
#      :TestClass                   — клас у будь-якому модулі
#      -/module                     — ВИКЛЮЧИТИ модуль
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TAGS="${1:-}"
if [[ -z "$TAGS" ]]; then
    echo "Використання: $0 <тег>    напр.: $0 techdistrib_credit"
    exit 1
fi

# -d: ім'я тестової бази. Якщо її немає — Odoo створить.
# Прибираємо суфікс тегу після ':' — модуль потрібен для -i.
# wkhtmltopdf у PATH — щоб тести друку PDF працювали (див. run_odoo.sh)
WKHTML_DIR="$ROOT/vendor/wkhtmltopdf/extracted/usr/local/bin"
[[ -x "$WKHTML_DIR/wkhtmltopdf" ]] && export PATH="$WKHTML_DIR:$PATH"

MODULE="${TAGS%%:*}"
TEST_DB="${TEST_DB:-techdistrib_test}"

# --- Режим «усі модулі проєкту» ----------------------------------------------
# Використання:  ./scripts/run_tests.sh --all
#
# Тут видно справжню цінність ТЕГІВ. У кожного тестового класу проєкту є
# однаковий тег 'techdistrib':
#     @tagged('post_install', '-at_install', 'techdistrib')
# Тому один прогін із --test-tags techdistrib запускає тести ВСІХ модулів
# одразу. Без тегів довелося б перелічувати класи поіменно.
#
# Окрема тестова база (techdistrib_test_all) потрібна, щоб не змішувати
# результати з прогонами окремих модулів.
if [[ "$TAGS" == "--all" ]]; then
    MODULE="techdistrib_base,techdistrib_credit,techdistrib_pricing,techdistrib_warranty"
    TAGS="techdistrib"
    TEST_DB="${TEST_DB:-techdistrib_test_all}"
    echo "🧪 Повний прогін: усі модулі TechDistrib"
else
    echo "🧪 Тестую '$TAGS' у базі '$TEST_DB' ..."
fi
echo "(перший прогін довший: база створюється з нуля)"
echo

# --test-enable       — без цього прапорця Odoo НЕ запускає тести взагалі
# --stop-after-init   — завершитись після тестів, не слухати порт
# -i $MODULE          — гарантуємо, що модуль встановлено (разом із залежностями)
# --no-http           — просимо не піднімати веб-сервер
# --http-port=8099    — І ОСЬ ЧОМУ ВІН УСЕ ОДНО ПОТРІБЕН:
#
#   У коді Odoo є такий рядок (vendor/odoo/odoo/service/server.py:591):
#
#       if test_mode or (config['http_enable'] and not stop):
#           ... start http server ...
#
#   Тобто в РЕЖИМІ ТЕСТІВ (test_mode = True) Odoo піднімає HTTP-сервер
#   ЗАВЖДИ — навіть якщо передати --no-http. Це зроблено навмисно:
#   тести, успадковані від HttpCase, ходять у справжній веб-сервер.
#
#   Практичний наслідок: якщо поруч працює робочий Odoo на 8069,
#   тести впадуть із «Address already in use». Тому даємо їм свій порт.
#
# --log-level=test    — показує результат кожного тесту
"$ROOT/.venv/bin/python" "$ROOT/vendor/odoo/odoo-bin" \
    --config="$ROOT/config/odoo.conf" \
    -d "$TEST_DB" \
    -i "$MODULE" \
    --test-enable \
    --test-tags "$TAGS" \
    --stop-after-init \
    --no-http \
    --http-port=8099 \
    --log-level=test \
    --without-demo=all

echo
echo "✅ Прогін завершено. Рядки 'FAIL'/'ERROR' вище — це проблеми, які треба виправити."
