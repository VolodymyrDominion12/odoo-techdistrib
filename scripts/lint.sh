#!/usr/bin/env bash
# =============================================================================
#  lint.sh — єдина команда перевірки стилю й типових помилок Odoo
# =============================================================================
#  Використання:
#      ./scripts/lint.sh              # перевірити всі кастомні аддони
#      ./scripts/lint.sh --fix        # спробувати автоправки ruff (БЕЗПЕЧНІ,
#                                     #  бо конфіг ruff.toml вимикає руйнівні)
#      ./scripts/lint.sh techdistrib_credit    # лише один модуль
#
#  ЩО ТУТ ПЕРЕВІРЯЄТЬСЯ І ЧОМУ САМЕ ТАК:
#
#  1. ruff — швидкий (мілісекунди) і ловить реальні помилки Python:
#     неіснуючі імена, тіні, пастки bugbear. Конфіг ruff.toml СВІДОМО вимикає
#     два правила, які на Odoo-аддоні ламають код (F401 для __init__.py та
#     UP031 для перекладів) — подробиці в шапці ruff.toml.
#
#  2. pylint + pylint_odoo — повільніший, але знає про Odoo те, чого не знає
#     ніщо інше: застарілі ключі маніфесту, позиційні плейсхолдери в _(),
#     search([]) без limit, перевизначення методів без super().
#
#  ЧОМУ PYTHON ДЛЯ ЛІНТЕРІВ ОКРЕМИЙ (.venv-lint):
#  Odoo-venv (.venv) зібраний через `uv` під сам рантайм Odoo. Лінтери — це
#  інструменти розробки, і тримати їх осторонь чистіше: вони не впливають на
#  те, що виконується в проді, і їх можна оновити, не чіпаючи рантайм.
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LINT_VENV="${LINT_VENV:-$ROOT/.venv-lint}"
# pylint пише кеш у ~/.cache/pylint. У деяких оточеннях запис туди заборонено —
# тоді pylint не падає, але й не кешує, і працює помітно повільніше. Тримаємо
# кеш у проєкті, щоб результат був однаковий у будь-якій оболонці.
export PYLINTHOME="${PYLINTHOME:-$ROOT/.pylint-cache}"

FIX=""
TARGET=""
for arg in "$@"; do
    case "$arg" in
        --fix) FIX="--fix" ;;
        -*) echo "Невідомий прапорець: $arg" >&2; exit 2 ;;
        *) TARGET="$arg" ;;
    esac
done

# --- 1. Забезпечити наявність лінтерів ---------------------------------------
if [[ ! -x "$LINT_VENV/bin/pylint" ]]; then
    echo "🔧 Створюю середовище для лінтерів у .venv-lint (лише першого разу)..."
    python3 -m venv "$LINT_VENV"
    "$LINT_VENV/bin/pip" install --quiet --upgrade pip
    "$LINT_VENV/bin/pip" install --quiet pylint-odoo ruff
    echo "   готово."
    echo
fi

# --- 2. Що саме перевіряємо --------------------------------------------------
if [[ -n "$TARGET" ]]; then
    PATHS=("custom_addons/$TARGET")
    if [[ ! -d "${PATHS[0]}" ]]; then
        echo "Немає такого модуля: ${PATHS[0]}" >&2
        exit 2
    fi
else
    PATHS=("custom_addons")
fi

mkdir -p "$PYLINTHOME"
STATUS=0

# --- 3. ruff -----------------------------------------------------------------
echo "▶ ruff ($( "$LINT_VENV/bin/ruff" --version ))"
if ! "$LINT_VENV/bin/ruff" check "${PATHS[@]}" $FIX; then
    STATUS=1
fi
echo

# --- 4. pylint + pylint_odoo -------------------------------------------------
echo "▶ pylint + pylint_odoo"
if ! "$LINT_VENV/bin/pylint" --rcfile="$ROOT/.pylintrc" "${PATHS[@]}"; then
    STATUS=1
fi

echo
if [[ "$STATUS" -eq 0 ]]; then
    echo "✅ Лінтери чисті."
else
    echo "❌ Є зауваження вище. Виправ або — якщо це хибне спрацьовування —"
    echo "   додай правило у ruff.toml / .pylintrc З КОМЕНТАРЕМ, чому саме."
    echo "   Не вимикай перевірку мовчки: наступний агент не знатиме, що це рішення."
fi
exit "$STATUS"
