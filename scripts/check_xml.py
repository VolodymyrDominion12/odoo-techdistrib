#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_xml.py — перевірка всіх XML-файлів кастомних модулів.

НАВІЩО ЦЕЙ СКРИПТ
------------------
У XML є дві помилки, які легко зробити руками і важко помітити оком:

  1. ПОДВІЙНИЙ ДЕФІС у коментарі. Стандарт XML забороняє послідовність
     з двох дефісів усередині коментаря. Але ж ми звикли малювати
     розділювачі саме дефісами:

         <!-- ========== СЕКЦІЯ ========== -->      ← ок
         <!-- ---------- СЕКЦІЯ ---------- -->      ← ПОМИЛКА

     А ще гірше — коли дефіси всередині тексту:
         <!-- НЕ МОЖЕ містити два дефіси поспіль -->

     Odoo у відповідь видає незрозуміле:
         lxml.etree.XMLSyntaxError: Double hyphen within comment

  2. НЕЗАКРИТИЙ ТЕГ або неправильна вкладеність.

У цьому проєкті я припустився помилки №1 ТРИЧІ. Саме тому цей скрипт існує:
перевіряти XML треба автоматично, а не очима.

ЗАПУСК
------
    python3 scripts/check_xml.py
    python3 scripts/check_xml.py custom_addons/techdistrib_base

Код виходу: 0 — усе гаразд, 1 — знайдено проблеми.
"""

import re
import sys
import pathlib
import xml.etree.ElementTree as ET


def check_file(path):
    """Повертає список проблем у файлі (порожній список — усе гаразд)."""
    problems = []
    try:
        text = path.read_text(encoding='utf-8')
    except UnicodeDecodeError as exc:
        return [f'не вдалось прочитати як UTF-8: {exc}']

    # --- 1. Подвійні дефіси всередині коментарів -----------------------------
    for match in re.finditer(r'<!--(.*?)-->', text, re.DOTALL):
        body = match.group(1)
        if '--' in body:
            line = text[:match.start()].count('\n') + 1
            # Показуємо фрагмент, щоб було зрозуміло, ЩО саме виправити
            snippet = re.search(r'.{0,30}--.{0,30}', body, re.DOTALL)
            found = snippet.group(0).replace('\n', ' ') if snippet else body[:60]
            problems.append(
                f'рядок {line}: два дефіси поспіль у коментарі → «...{found}...»'
            )

    # --- 2. Валідність XML ---------------------------------------------------
    try:
        ET.fromstring(text)
    except ET.ParseError as exc:
        line = getattr(exc, 'position', (0, 0))[0]
        problems.append(f'рядок {line}: невалідний XML → {exc}')

    return problems


def main():
    # За замовчуванням перевіряємо всі кастомні модулі
    roots = sys.argv[1:]
    if not roots:
        base = pathlib.Path(__file__).resolve().parent.parent
        roots = [str(base / 'custom_addons')]

    checked = 0
    total_problems = 0

    for root in roots:
        root_path = pathlib.Path(root)
        if not root_path.exists():
            print(f'⚠️  Шлях не існує: {root}')
            continue
        for path in sorted(root_path.rglob('*.xml')):
            checked += 1
            problems = check_file(path)
            if problems:
                total_problems += len(problems)
                print(f'\n❌ {path}')
                for problem in problems:
                    print(f'     {problem}')

    print()
    if total_problems:
        print(f'❌ Перевірено {checked} файлів, знайдено {total_problems} проблем.')
        print('   Найчастіша причина — два дефіси поспіль у коментарі.')
        return 1
    print(f'✅ Перевірено {checked} XML-файлів — усе гаразд.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
