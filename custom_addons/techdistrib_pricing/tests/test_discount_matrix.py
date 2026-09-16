# -*- coding: utf-8 -*-
# =============================================================================
#  Тести матриці дилерських знижок
# =============================================================================
#  Тут є НАСКРІЗНИЙ тест — найцінніший тип тесту: він перевіряє, що
#  вся ланцюжок працює від початку до кінця:
#
#      рядок матриці → правило прайс-листа → прайс-лист дилера →
#      ціна й знижка в рядку замовлення
#
#  Юніт-тести кожного окремого кроку не гарантують, що кроки з'єднані
#  правильно. Наскрізний — гарантує.
# =============================================================================

from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install', 'techdistrib')
class TestDiscountMatrix(TransactionCase):
    """Матриця знижок і її вплив на ціни в замовленні."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.matrix_model = cls.env['techdistrib.discount.matrix']

        cls.tier_bronze = cls.env.ref('techdistrib_base.dealer_tier_bronze')
        cls.tier_gold = cls.env.ref('techdistrib_base.dealer_tier_gold')
        cls.tier_platinum = cls.env.ref('techdistrib_base.dealer_tier_platinum')

        cls.categ_servers = cls.env.ref(
            'techdistrib_pricing.product_category_servers')
        cls.categ_network = cls.env.ref(
            'techdistrib_pricing.product_category_network')

        # ⚠️ УРОК ПРО ДИЗАЙН ТЕСТІВ (реальна помилка, яку я тут зловив).
        #
        # Спочатку мої тести створювали рядки матриці прямо для категорій
        # із data-файлу. І вони всі впали:
        #     psycopg2.errors.UniqueViolation: duplicate key value violates
        #     unique constraint "techdistrib_discount_matrix_tier_categ_uniq"
        #
        # Причина: data-файл УЖЕ створив усі 16 рядків (4 рівні × 4 категорії).
        # Мої тести намагались створити ті самі пари вдруге.
        #
        # Це помилка ДИЗАЙНУ ТЕСТІВ, а не коду: тест не має права
        # припускати, що база порожня, якщо модуль сам її наповнює.
        #
        # Правильне рішення: для перевірки механіки синхронізації створити
        # ВЛАСНІ категорії, яких немає в data-файлі. А засіяні рядки
        # використовувати для наскрізних тестів ціноутворення.
        cls.categ_test_a = cls.env['product.category'].create({
            'name': 'ТЕСТ: категорія A',
        })
        cls.categ_test_b = cls.env['product.category'].create({
            'name': 'ТЕСТ: категорія B',
        })

        # Товар із категорії «Сервери». list_price = 1000 — базова ціна,
        # від якої рахується знижка.
        cls.product = cls.env['product.product'].create({
            'name': 'Сервер Dell R650',
            'type': 'consu',
            'list_price': 1000.0,
            'categ_id': cls.categ_servers.id,
            'taxes_id': [(5, 0, 0)],   # без податків, щоб тести були прозорими
        })

        # Дилер рівня Gold
        cls.dealer_gold = cls.env['res.partner'].create({
            'name': 'ТОВ Голд-Тех',
            'is_dealer': True,
            'dealer_tier_id': cls.tier_gold.id,
        })

    # =========================================================================
    #  СИНХРОНІЗАЦІЯ З ПРАЙС-ЛИСТОМ
    # =========================================================================

    def test_matrix_row_creates_pricelist_item(self):
        """FR-B02/B03: рядок матриці створює правило прайс-листа."""
        row = self.matrix_model.create({
            'tier_id': self.tier_bronze.id,
            'categ_id': self.categ_test_a.id,
            'discount': 9.0,
        })

        self.assertTrue(row.pricelist_item_id,
                        'Має бути створене правило прайс-листа')
        item = row.pricelist_item_id

        self.assertEqual(item.compute_price, 'percentage')
        self.assertEqual(item.applied_on, '2_product_category')
        self.assertEqual(item.base, 'list_price')
        self.assertAlmostEqual(item.percent_price, 9.0, places=2)
        self.assertEqual(item.categ_id, self.categ_test_a)
        # Правило має лежати саме в прайс-листі цього рівня
        self.assertEqual(item.pricelist_id, self.tier_bronze.pricelist_id)

    def test_created_pricelist_belongs_to_tier(self):
        """Кожен рівень отримує ВЛАСНИЙ прайс-лист."""
        pricelist = self.tier_gold._ensure_pricelist()
        self.assertTrue(pricelist)
        self.assertEqual(self.tier_gold.pricelist_id, pricelist)
        # Прайс-лист має бути активним, інакше Odoo його не застосує
        self.assertTrue(pricelist.active)

    def test_ensure_pricelist_is_idempotent(self):
        """Повторний виклик не створює другий прайс-лист."""
        first = self.tier_platinum._ensure_pricelist()
        second = self.tier_platinum._ensure_pricelist()
        self.assertEqual(first, second)

    def test_changing_discount_updates_pricelist_item(self):
        """Зміна відсотка ОНОВЛЮЄ правило, а не створює нове."""
        row = self.matrix_model.create({
            'tier_id': self.tier_bronze.id,
            'categ_id': self.categ_test_a.id,
            'discount': 3.0,
        })
        original_item = row.pricelist_item_id

        row.discount = 7.5

        self.assertEqual(row.pricelist_item_id, original_item,
                         'Правило має оновитись, а не створитись заново — '
                         'інакше прайс-лист заросте дублями')
        self.assertAlmostEqual(row.pricelist_item_id.percent_price, 7.5, places=2)

    def test_deleting_row_removes_pricelist_item(self):
        """Видалення рядка матриці прибирає правило прайс-листа.

        Без цього у прайс-листі зависло б «осиротіле» правило, яке
        продовжувало б давати знижку — тихо й незрозуміло звідки.
        """
        row = self.matrix_model.create({
            'tier_id': self.tier_platinum.id,
            'categ_id': self.categ_test_a.id,
            'discount': 5.0,
        })
        item = row.pricelist_item_id
        self.assertTrue(item.exists())

        row.unlink()

        self.assertFalse(item.exists(),
                         'Правило прайс-листа має бути видалене разом із рядком')

    def test_unique_constraint_per_tier_and_category(self):
        """Не можна мати два рядки для однієї пари «рівень + категорія»."""
        from psycopg2 import IntegrityError
        self.matrix_model.create({
            'tier_id': self.tier_platinum.id,
            'categ_id': self.categ_test_a.id,
            'discount': 1.0,
        })
        with self.assertRaises(IntegrityError):
            self.matrix_model.create({
                'tier_id': self.tier_platinum.id,
                'categ_id': self.categ_test_a.id,
                'discount': 2.0,
            })

    def test_absurd_discount_is_rejected(self):
        """Захист від помилки введення: знижка 95 % — майже напевно одруківка."""
        with self.assertRaises(ValidationError):
            self.matrix_model.create({
                'tier_id': self.tier_bronze.id,
                'categ_id': self.categ_test_a.id,
                'discount': 95.0,
            })

    def test_regenerate_pricelist_restores_broken_link(self):
        """Кнопка «Перебудувати» лагодить розсинхронізацію."""
        row = self.matrix_model.create({
            'tier_id': self.tier_bronze.id,
            'categ_id': self.categ_test_a.id,
            'discount': 4.0,
        })
        pricelist = row.tier_id.pricelist_id

        # Імітуємо втручання людини: хтось видалив правило прямо в прайс-листі
        row.pricelist_item_id.unlink()
        row.invalidate_recordset(['pricelist_item_id'])
        self.assertFalse(row.pricelist_item_id)

        # «Рятівне коло»
        row.tier_id.action_regenerate_pricelist()

        self.assertTrue(row.pricelist_item_id,
                        'Правило має відновитись у тому ж прайс-листі')
        self.assertEqual(row.pricelist_item_id.pricelist_id, pricelist)

    # =========================================================================
    #  ПРИЗНАЧЕННЯ ПРАЙС-ЛИСТА ДИЛЕРУ
    # =========================================================================

    def test_dealer_gets_tier_pricelist(self):
        """FR-B02: дилер автоматично отримує прайс-лист свого рівня."""
        dealer = self.env['res.partner'].create({
            'name': 'ТОВ Тестовий Gold',
            'is_dealer': True,
            'dealer_tier_id': self.tier_gold.id,
        })
        self.assertEqual(
            dealer.property_product_pricelist,
            self.tier_gold._ensure_pricelist(),
            'Дилер має отримати прайс-лист рівня Gold',
        )

    def test_changing_tier_changes_pricelist(self):
        """Зміна рівня дилера автоматично змінює його прайс-лист."""
        dealer = self.env['res.partner'].create({
            'name': 'ТОВ Дилер що зростає',
            'is_dealer': True,
            'dealer_tier_id': self.tier_bronze.id,
        })
        self.assertEqual(dealer.property_product_pricelist,
                         self.tier_bronze._ensure_pricelist())

        dealer.dealer_tier_id = self.tier_gold

        self.assertEqual(
            dealer.property_product_pricelist,
            self.tier_gold._ensure_pricelist(),
            'Після підвищення рівня прайс-лист має змінитись автоматично',
        )

    def test_non_dealer_does_not_get_tier_pricelist(self):
        """Наш код НЕ призначає прайс-лист не-дилерам.

        ⚠️ УРОК ПРО ТЕ, ЩО САМЕ ПЕРЕВІРЯТИ В ТЕСТІ (реальна історія).

        Спочатку тест перевіряв, що в не-дилера поле
        property_product_pricelist ПОРОЖНЄ. Він упав:
            AssertionError: product.pricelist(1,) is not false

        А потім, після першої спроби виправлення, упав ще раз:
            AssertionError: 1 unexpectedly found in [4, 3, 2, 1]

        Причина обох падінь одна й повчальна: у тестовій базі, створеній
        з --without-demo=all, НЕ існує стандартного «Public Pricelist».
        А поле property_product_pricelist у Odoo має власну логіку
        типового значення — воно підставляє ПЕРШИЙ АКТИВНИЙ прайс-лист.
        Ним виявився наш же прайс-лист Bronze (він створився першим).

        Тобто тест перевіряв НЕ нашу логіку, а поведінку Odoo за
        замовчуванням — і падав через особливість тестового середовища,
        якої в продакшені не буде (там є налаштований типовий прайс-лист).

        ПРАВИЛЬНИЙ ПІДХІД: перевіряти КОНТРАКТ НАШОГО КОДУ —
        «наша синхронізація не чіпає не-дилерів», а не кінцеве значення
        поля, на яке впливають ще й стандартні механізми Odoo.
        """
        client = self.env['res.partner'].create({'name': 'Звичайний клієнт'})
        before = client.property_product_pricelist

        client._sync_dealer_pricelist()

        self.assertEqual(
            client.property_product_pricelist, before,
            'Наша синхронізація не має змінювати прайс-лист не-дилера',
        )

    def test_dealer_without_tier_does_not_get_pricelist(self):
        """Дилер без рівня теж не отримує прайс-лист рівня.

        Це перевірка граничного випадку: галочка «Дилер» стоїть,
        а рівень ще не присвоєний. Наш код має коректно нічого не робити,
        а не падати з помилкою.
        """
        dealer = self.env['res.partner'].create({
            'name': 'Дилер без рівня',
            'is_dealer': True,
        })
        before = dealer.property_product_pricelist

        dealer._sync_dealer_pricelist()

        self.assertEqual(dealer.property_product_pricelist, before)

    # =========================================================================
    #  НАСКРІЗНИЙ ТЕСТ: від матриці до ціни в замовленні
    # =========================================================================

    def test_end_to_end_discount_in_sale_order(self):
        """НАСКРІЗНИЙ: матриця → прайс-лист → ціна й знижка в замовленні.

        Це найважливіший тест модуля. Він перевіряє всю ланку:

          1. У матриці Gold/Сервери стоїть 16 % (з data-файлу).
          2. Gold має прайс-лист із правилом «-16 %» на категорію «Сервери».
          3. Дилер Gold автоматично отримав цей прайс-лист.
          4. У замовленні Odoo підставив БАЗОВУ ціну 1000 і ЗНИЖКУ 16 %.
          5. Підсумок рядка = 840.

        Якщо цей тест зелений — FR-B02, B03 і B06 виконані повністю,
        і при цьому ми не написали жодного рядка розрахунку ціни.
        """
        order = self.env['sale.order'].create({
            'partner_id': self.dealer_gold.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_qty': 1,
            })],
        })

        line = order.order_line

        # 3. Прайс-лист підставився
        self.assertEqual(order.pricelist_id, self.tier_gold._ensure_pricelist(),
                         'Замовлення має взяти прайс-лист дилера')

        # 4. Базова ціна і знижка
        self.assertAlmostEqual(
            line.price_unit, 1000.0, places=2,
            msg='Odoo має лишити базову ціну, а знижку показати окремо',
        )
        self.assertAlmostEqual(
            line.discount, 16.0, places=2,
            msg='Знижка рівня Gold для категорії «Сервери» — 16 %. '
                'Якщо тут 0 — найімовірніше, вимкнено функцію «Discounts» '
                '(див. data/res_groups_data.xml)',
        )

        # 5. Підсумок
        self.assertAlmostEqual(line.price_subtotal, 840.0, places=2)

        # І наші допоміжні поля показують правильний розклад
        self.assertAlmostEqual(line.tier_discount, 16.0, places=2)
        self.assertAlmostEqual(line.manual_discount, 0.0, places=2)

    def test_different_tiers_get_different_prices(self):
        """Різні рівні — різні ціни на той самий товар."""
        results = {}
        for tier in (self.tier_bronze, self.tier_gold, self.tier_platinum):
            dealer = self.env['res.partner'].create({
                'name': 'Дилер %s' % tier.name,
                'is_dealer': True,
                'dealer_tier_id': tier.id,
            })
            order = self.env['sale.order'].create({
                'partner_id': dealer.id,
                'order_line': [(0, 0, {
                    'product_id': self.product.id,
                    'product_uom_qty': 1,
                })],
            })
            results[tier.code] = order.order_line.price_subtotal

        self.assertAlmostEqual(results['bronze'], 950.0, places=2)     # -5 %
        self.assertAlmostEqual(results['gold'], 840.0, places=2)       # -16 %
        self.assertAlmostEqual(results['platinum'], 780.0, places=2)   # -22 %

        self.assertLess(results['platinum'], results['gold'])
        self.assertLess(results['gold'], results['bronze'])

    # =========================================================================
    #  КОНТРОЛЬ РУЧНОЇ ЗНИЖКИ (FR-B04, FR-B05)
    # =========================================================================

    def test_manual_discount_within_limit_is_allowed(self):
        """Менеджер може додати знижку в межах ліміту рівня."""
        # Gold: ліміт ручної знижки 5 % (з data-файлу)
        self.assertAlmostEqual(self.tier_gold.max_manual_discount, 5.0, places=2)

        order = self.env['sale.order'].create({
            'partner_id': self.dealer_gold.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_qty': 1,
            })],
        })
        line = order.order_line
        self.assertAlmostEqual(line.discount, 16.0, places=2)

        # Додаємо 5 % зверху — це рівно ліміт, має пройти
        line.discount = 21.0
        self.assertAlmostEqual(line.manual_discount, 5.0, places=2)
        self.assertAlmostEqual(line.manual_discount_excess, 0.0, places=2)

    def test_manual_discount_over_limit_is_rejected(self):
        """FR-B05: знижка понад ліміт блокується."""
        order = self.env['sale.order'].create({
            'partner_id': self.dealer_gold.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_qty': 1,
            })],
        })
        line = order.order_line
        self.assertAlmostEqual(line.discount, 16.0, places=2)

        # 16 + 5 = 21 — межа. Спробуємо 25 → ручна 9 % > ліміту 5 %
        with self.assertRaises(ValidationError):
            line.discount = 25.0

    def test_manual_discount_check_uses_only_manual_part(self):
        """КЛЮЧОВИЙ ТЕСТ: перевіряється ЛИШЕ ручна частина знижки.

        Це захист від найпідступнішої помилки в цьому модулі: якщо
        порівнювати ПОВНУ знижку з лімітом ручної, то Gold-дилер із
        знижкою 16 % від прайс-листа не отримав би жодної ручної знижки,
        бо 16 > 5. Ліміт перетворився б на заборону.
        """
        order = self.env['sale.order'].create({
            'partner_id': self.dealer_gold.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_qty': 1,
            })],
        })
        line = order.order_line

        # Повна знижка 21 % значно більша за ліміт 5 %, але ручна — рівно 5 %
        line.discount = 21.0
        self.assertGreater(line.discount, line.manual_discount_limit)
        self.assertLessEqual(line.manual_discount, line.manual_discount_limit)

        # А тепер зменшимо ЗНИЖКУ нижче рівня прайс-листа — це теж дозволено
        # (менеджер може продати дорожче, ніж дозволяє прайс-лист)
        line.discount = 10.0
        self.assertAlmostEqual(line.manual_discount, -6.0, places=2)
        self.assertAlmostEqual(line.manual_discount_excess, 0.0, places=2,
                               msg='Відʼємна ручна знижка не є перевищенням')

    def test_product_without_matrix_row_has_no_tier_discount(self):
        """Товар із категорії без рядка в матриці не отримує знижки."""
        other_category = self.env['product.category'].create({
            'name': 'Категорія без знижок',
        })
        product = self.env['product.product'].create({
            'name': 'Товар без знижки',
            'type': 'consu',
            'list_price': 500.0,
            'categ_id': other_category.id,
            'taxes_id': [(5, 0, 0)],
        })
        order = self.env['sale.order'].create({
            'partner_id': self.dealer_gold.id,
            'order_line': [(0, 0, {
                'product_id': product.id,
                'product_uom_qty': 1,
            })],
        })
        line = order.order_line
        self.assertAlmostEqual(line.discount, 0.0, places=2)
        self.assertAlmostEqual(line.price_subtotal, 500.0, places=2)
        self.assertAlmostEqual(line.tier_discount, 0.0, places=2)
