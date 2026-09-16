#!/usr/bin/env bash
# =============================================================================
#  setup_venv.sh — створення Python-середовища та встановлення залежностей Odoo
# =============================================================================
#  Запускати один раз після клонування Odoo.
#  Ідемпотентний: можна запускати повторно, нічого не зламається.
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# --- Кеш uv усередині проєкту -------------------------------------------------
# Стандартний кеш uv лежить у ~/.cache/uv. У цьому середовищі запис за межі
# робочої папки обмежений, тому тримаємо кеш поруч із проєктом.
export UV_CACHE_DIR="$ROOT/.uv-cache"
mkdir -p "$UV_CACHE_DIR"

# --- 1. Вихідний код Odoo ----------------------------------------------------
if [[ ! -f "$ROOT/vendor/odoo/odoo-bin" ]]; then
    echo "📥 Клоную Odoo 18 (це займе кілька хвилин, ~1.3 ГБ)..."
    rm -rf "$ROOT/vendor/odoo"
    git clone --depth 1 --branch 18.0 https://github.com/odoo/odoo.git "$ROOT/vendor/odoo"
else
    echo "✅ Код Odoo вже є: $(git -C "$ROOT/vendor/odoo" log --oneline -1)"
fi

# --- 2. venv -----------------------------------------------------------------
# ЧОМУ САМЕ PYTHON 3.12:
# Odoo 18 офіційно підтримує Python 3.10–3.13, але requirements.txt має
# окремі піни під кожну версію. Python 3.12 — це версія, під яку Odoo 18
# розроблявся і тестується найретельніше (див. коментарі "(Noble)" у
# requirements.txt: Noble = Ubuntu 24.04, а в ній саме Python 3.12).
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
    echo "🐍 Створюю venv на Python 3.12..."
    uv venv --python /usr/bin/python3.12 "$ROOT/.venv"
else
    echo "✅ venv уже є: $("$ROOT/.venv/bin/python" --version)"
fi

# --- 3. Залежності -----------------------------------------------------------
# ВАЖЛИВИЙ НЮАНС: виключаємо python-ldap.
#
# Розбираємось, чому:
#   * requirements.txt вимагає python-ldap==3.4.4;
#   * на PyPI для python-ldap НЕМАЄ готових wheel-пакетів — тільки сорці;
#   * отже pip змушений компілювати C-розширення;
#   * компіляція вимагає заголовків libldap2-dev і libsasl2-dev (файл lber.h);
#   * у цьому середовищі немає sudo, тому поставити їх через apt неможливо.
#
# Чи це проблема? Ні. Перевірено пошуком по коду:
#     grep -rln "import ldap" vendor/odoo
#     → addons/auth_ldap/models/res_company_ldap.py
# Тобто python-ldap потрібен РІВНО ОДНОМУ необов'язковому модулю — auth_ldap
# (автентифікація через LDAP-каталог). Ми його не встановлюємо, тому Odoo
# працює повністю без цієї бібліотеки.
#
# Якщо колись знадобиться LDAP:
#     sudo apt install libldap2-dev libsasl2-dev
#     uv pip install python-ldap
echo "📦 Встановлюю залежності Odoo..."
grep -v -E '^[[:space:]]*python-ldap' "$ROOT/vendor/odoo/requirements.txt" \
    > "$UV_CACHE_DIR/requirements-no-ldap.txt"

VIRTUAL_ENV="$ROOT/.venv" uv pip install -r "$UV_CACHE_DIR/requirements-no-ldap.txt"

echo
echo "✅ Готово!"
"$ROOT/.venv/bin/python" --version
echo "Наступний крок:  docker compose up -d && ./scripts/init_db.sh"
