#!/usr/bin/env bash
# =============================================================================
#  odoo_shell.sh — інтерактивна консоль Odoo (найкращий інструмент для навчання)
# =============================================================================
#  Використання:
#      ./scripts/odoo_shell.sh                       # shell на базі techdistrib
#      ./scripts/odoo_shell.sh -d інша_база
#      echo "print(env['res.partner'].search_count([]))" | ./scripts/odoo_shell.sh
#
#  Навіщо це потрібно:
#  Замість того щоб створювати записи через веб-інтерфейс, ти пишеш один рядок
#  Python і одразу бачиш результат. Це в рази швидше для перевірки логіки.
#
#  У shell доступні:
#      env  — «середовище» Odoo: головна точка входу в ORM
#      self — порожній запис (не використовуй)
#
#  Приклади:
#      >>> env['res.partner'].search([('is_company','=',True)], limit=5).mapped('name')
#      >>> env['product.product'].search_count([('type','=','consu')])
#      >>> partner = env['res.partner'].create({'name': 'Тест'})
#      >>> env.cr.commit()          # ЗБЕРЕГТИ зміни (без цього все відкотиться!)
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
    echo "❌ Немає venv. Запусти: ./scripts/setup_venv.sh"
    exit 1
fi

# shell — це підкоманда odoo-bin. Вона підключається до бази і дає REPL.
# --no-http потрібен, щоб консоль не займала порт 8069 (інакше конфлікт
# із уже запущеним сервером).
exec "$ROOT/.venv/bin/python" "$ROOT/vendor/odoo/odoo-bin" shell \
    --config="$ROOT/config/odoo.conf" \
    --no-http \
    "$@"
