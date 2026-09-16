# -*- coding: utf-8 -*-
"""
load_demo_data.py — наповнення бази TechDistrib демонстраційними даними.

ЗАПУСК
------
    ./scripts/load_demo.sh

Або вручну:
    ./scripts/odoo_shell.sh -d techdistrib < scripts/load_demo_data.py

ЩО СТВОРЮЄТЬСЯ
--------------
* 3 вендори (постачальники)
* 5 дилерів із різними рівнями та кредитними лімітами
* 1 дилер із простроченою заборгованістю (щоб побачити блокування)
* Товари в кожній категорії (частина — з обліком за серійним номером)
* Демонстраційне замовлення, яке перевищує кредитний ліміт

СКРИПТ ІДЕМПОТЕНТНИЙ
--------------------
Повторний запуск не створить дублів: усі записи перевіряються за назвою.
Запускати можна скільки завгодно разів.

Цей скрипт виконується в середовищі `odoo shell`, тому тут доступна
змінна `env` — «середовище» Odoo (див. docs/00-overview.md, розділ 1).
"""

# =============================================================================
#  ДОПОМІЖНІ ФУНКЦІЇ
# =============================================================================

def get_or_create(model_name, domain, values):
    """Знайти запис за доменом або створити новий.

    Це «ідемпотентний create»: саме так роблять скрипти імпорту, щоб
    їх можна було запускати повторно без створення дублів.
    """
    record = env[model_name].search(domain, limit=1)
    if record:
        return record, False
    return env[model_name].create(values), True


created = {'partners': 0, 'products': 0, 'orders': 0}


def report(label, record, was_created):
    status = 'створено' if was_created else 'уже існує'
    print(f'   [{status:>10}] {label}: {record.display_name}')


# =============================================================================
#  1. ВЕНДОРИ
# =============================================================================
print('\n=== 1. Вендори ===')

vendors_data = [
    ('Dell Technologies', 'US'),
    ('Cisco Systems', 'US'),
    ('Advantech', 'TW'),
]
for name, country_code in vendors_data:
    country = env['res.country'].search([('code', '=', country_code)], limit=1)
    vendor, is_new = get_or_create(
        'res.partner',
        [('name', '=', name)],
        {'name': name, 'is_company': True, 'country_id': country.id if country else False,
         'supplier_rank': 1},
    )
    report('Вендор', vendor, is_new)
    created['partners'] += int(is_new)


# =============================================================================
#  2. ДИЛЕРИ
# =============================================================================
print('\n=== 2. Дилерська мережа ===')

# Зверни увагу: ми НЕ створюємо прайс-листи й не проставляємо код дилера
# вручну. Усе це робить наш код модулів автоматично:
#   * techdistrib_base     — присвоює код DLR-XXXXX
#   * techdistrib_pricing  — призначає прайс-лист рівня
# Це і є перевірка, що наша автоматизація працює.

dealers_data = [
    ('ТОВ Альфа-Тех', 'gold', 500000.0),
    ('ТОВ Бета-Системс', 'silver', 250000.0),
    ('ТОВ Гамма-Нетворк', 'platinum', 1500000.0),
    ('ФОП Іваненко (дрібний дилер)', 'bronze', 50000.0),
    ('ТОВ Дельта-Інтеграція', 'silver', 300000.0),
]

dealers = []
for name, tier_code, credit_limit in dealers_data:
    tier = env['techdistrib.dealer.tier'].search([('code', '=', tier_code)], limit=1)
    dealer, is_new = get_or_create(
        'res.partner',
        [('name', '=', name)],
        {'name': name, 'is_company': True, 'is_dealer': True,
         'dealer_tier_id': tier.id},
    )
    if is_new:
        # credit_limit — СТАНДАРТНЕ поле Odoo (модуль account), ми його
        # перевикористовуємо, а не створюємо своє.
        dealer.sudo().credit_limit = credit_limit
        created['partners'] += 1
    report(f'Дилер ({tier_code})', dealer, is_new)
    dealers.append(dealer)


# =============================================================================
#  3. ТОВАРИ
# =============================================================================
print('\n=== 3. Номенклатура ===')

products_data = [
    # (назва, категорія, ціна, серійний облік)
    ('Сервер Dell PowerEdge R650', 'product_category_servers', 8500.0, True),
    ('СХД Dell PowerVault ME5024', 'product_category_servers', 12400.0, True),
    ('Комутатор Cisco Catalyst 9300', 'product_category_network', 5200.0, True),
    ('Маршрутизатор Cisco ISR 4331', 'product_category_network', 3800.0, True),
    ('Промисловий шлюз Advantech WISE-6610', 'product_category_iot', 1250.0, True),
    ('Контролер Advantech ADAM-6050', 'product_category_iot', 480.0, False),
    ('Кабель SFP+ DAC 3м', 'product_category_accessories', 85.0, False),
    ('Комплект кріплення в стійку', 'product_category_accessories', 120.0, False),
]

products = []
for name, categ_xmlid, price, serial in products_data:
    categ = env.ref(f'techdistrib_pricing.{categ_xmlid}')
    product, is_new = get_or_create(
        'product.product',
        [('name', '=', name)],
        {
            'name': name,
            'type': 'consu',
            # ⚠️ is_storable — ОБОВ'ЯЗКОВО у Odoo 18 для складського обліку.
            # Без нього Odoo відмовиться створювати залишки.
            'is_storable': True,
            'list_price': price,
            'categ_id': categ.id,
            'tracking': 'serial' if serial else 'none',
            'taxes_id': [(5, 0, 0)],   # без податків, щоб суми були прозорими
        },
    )
    report(f'Товар ({categ.name})', product, is_new)
    created['products'] += int(is_new)
    products.append(product)


# =============================================================================
#  4. ДЕМОНСТРАЦІЙНЕ ЗАМОВЛЕННЯ, ЩО ПЕРЕВИЩУЄ ЛІМІТ
# =============================================================================
print('\n=== 4. Демонстраційне замовлення ===')

dealer_alpha = env['res.partner'].search([('name', '=', 'ТОВ Альфа-Тех')], limit=1)
server = env['product.product'].search(
    [('name', '=', 'Сервер Dell PowerEdge R650')], limit=1)

if dealer_alpha and server:
    existing = env['sale.order'].search([
        ('partner_id', '=', dealer_alpha.id),
        ('state', '=', 'draft'),
    ], limit=1)

    if existing:
        print(f'   [уже існує] Чернетка замовлення: {existing.name}')
        demo_order = existing
    else:
        demo_order = env['sale.order'].create({
            'partner_id': dealer_alpha.id,
            'order_line': [(0, 0, {
                'product_id': server.id,
                'product_uom_qty': 100,     # 100 × 8500 = 850 000 > ліміт 500 000
            })],
        })
        created['orders'] += 1
        print(f'   [  створено] Замовлення: {demo_order.name}')

    # Показуємо, як працює наша логіка
    print()
    print('   Перевірка кредитної політики:')
    print(f'     Кредитний ліміт дилера:  {dealer_alpha.sudo().credit_limit:,.2f}')
    print(f'     Сума замовлення:         {demo_order.amount_total:,.2f}')
    print(f'     Заблоковано:             {demo_order.credit_is_blocked}')
    if demo_order.credit_block_reason:
        first_line = demo_order.credit_block_reason.split('\n')[0]
        print(f'     Причина:                 {first_line}')

    print()
    print('   Матриця знижок для Gold:')
    gold_tier = env['techdistrib.dealer.tier'].search([('code', '=', 'gold')], limit=1)
    for row in gold_tier.matrix_ids:
        print(f'     {row.categ_id.name:<28} {row.discount:>5.1f} %')


# =============================================================================
#  5. ПІДСУМОК
# =============================================================================
env.cr.commit()

print('\n' + '=' * 62)
print('  ДЕМО-ДАНІ ЗАВАНТАЖЕНО')
print('=' * 62)
print(f"  Партнерів створено:  {created['partners']}")
print(f"  Товарів створено:    {created['products']}")
print(f"  Замовлень створено:  {created['orders']}")
print()
print('  Що подивитись у браузері (http://localhost:8069):')
print('    1. TechDistrib → Дилерська програма → Дилери')
print('       Коди DLR-XXXXX присвоєні автоматично.')
print('    2. TechDistrib → Дилерська програма → Матриця знижок')
print('       4 рівні × 4 категорії, редагуються прямо у списку.')
print('    3. Продажі → Замовлення → відкрити чернетку')
print('       Жовтий банер із розрахунком перевищення кредитного ліміту.')
print('    4. Склад → Серійні номери → вкладка «Гарантія»')
print('       (заповниться після першого реального відвантаження).')
print('=' * 62)
