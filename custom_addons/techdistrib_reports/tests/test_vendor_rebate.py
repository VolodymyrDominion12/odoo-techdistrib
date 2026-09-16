# -*- coding: utf-8 -*-
# =============================================================================
#  Тести ретро-бонусів вендорів
# =============================================================================
#  Тут є тест на НАЙРИЗИКОВАНІШУ частину — арифметику кварталів.
#  Помилка «на один день» у розрахунку періоду непомітна в інтерфейсі,
#  але призводить до того, що половина закупівель не потрапляє в бонус
#  або потрапляє двічі.
# =============================================================================

from datetime import date

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install', 'techdistrib')
class TestVendorRebate(TransactionCase):
    """Розрахунок ретро-бонусів і автоматичне створення за квартал."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.rebate_model = cls.env['techdistrib.vendor.rebate']

        # Вендор із домовленістю про бонус 5 %
        cls.vendor = cls.env['res.partner'].create({
            'name': 'ТОВ Вендор з бонусом',
            'is_company': True,
            'supplier_rank': 1,
            'vendor_rebate_percent': 5.0,
        })

        # Вендор без домовленості — для нього нарахування не створюється
        cls.vendor_no_rebate = cls.env['res.partner'].create({
            'name': 'ТОВ Вендор без бонусу',
            'is_company': True,
            'supplier_rank': 1,
        })

        cls.product = cls.env['product.product'].create({
            'name': 'Сервер для закупівлі',
            'type': 'consu',
            'is_storable': True,
            'list_price': 10000.0,
            'taxes_id': [(5, 0, 0)],
        })

    # --- Допоміжні методи ----------------------------------------------------

    def _create_purchase(self, vendor, amount, order_date, confirm=True):
        """Створити замовлення постачальнику із заданою датою й сумою."""
        order = self.env['purchase.order'].create({
            'partner_id': vendor.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_qty': 1,
                'price_unit': amount,
                'date_planned': order_date,
            })],
        })
        if confirm:
            order.button_confirm()
            # button_confirm перезаписує дату на поточну — тому
            # пересуваємо її ПІСЛЯ підтвердження.
            # Це той самий прийом, що в тестах Модуля 1 із sale.order.
            order.date_order = order_date
        return order

    # =========================================================================
    #  АРИФМЕТИКА КВАРТАЛІВ — найризикованіша частина
    # =========================================================================

    def test_previous_quarter_mid_year(self):
        """Травень → попередній квартал Q1 (січень-березень)."""
        date_from, date_to = self.rebate_model._get_previous_quarter_dates(
            date(2026, 5, 15))
        self.assertEqual(date_from, date(2026, 1, 1))
        self.assertEqual(date_to, date(2026, 3, 31))

    def test_previous_quarter_first_day_of_quarter(self):
        """1 квітня → попередній квартал теж Q1.

        Це найнебезпечніший день: межа. Помилка «на один день» тут
        призвела б до того, що 31 березня випало б із розрахунку.
        """
        date_from, date_to = self.rebate_model._get_previous_quarter_dates(
            date(2026, 4, 1))
        self.assertEqual(date_from, date(2026, 1, 1))
        self.assertEqual(date_to, date(2026, 3, 31))

    def test_previous_quarter_year_boundary(self):
        """Лютий 2027 → попередній квартал Q4 2026 (жовтень-грудень)."""
        date_from, date_to = self.rebate_model._get_previous_quarter_dates(
            date(2027, 2, 10))
        self.assertEqual(date_from, date(2026, 10, 1))
        self.assertEqual(date_to, date(2026, 12, 31))

    def test_previous_quarter_december(self):
        """Грудень → попередній квартал Q3 (липень-вересень)."""
        date_from, date_to = self.rebate_model._get_previous_quarter_dates(
            date(2026, 12, 31))
        self.assertEqual(date_from, date(2026, 7, 1))
        self.assertEqual(date_to, date(2026, 9, 30))

    # =========================================================================
    #  РОЗРАХУНОК БОНУСУ
    # =========================================================================

    def test_rebate_amount_calculation(self):
        """Бонус = оборот за період × відсоток / 100."""
        self._create_purchase(self.vendor, 100000.0, '2026-02-10 10:00:00')
        self._create_purchase(self.vendor, 50000.0, '2026-03-05 10:00:00')

        rebate = self.rebate_model.create({
            'vendor_id': self.vendor.id,
            'date_from': date(2026, 1, 1),
            'date_to': date(2026, 3, 31),
        })

        self.assertAlmostEqual(rebate.purchase_amount, 150000.0, places=2)
        self.assertAlmostEqual(rebate.rebate_amount, 7500.0, places=2)   # 5 %
        self.assertEqual(rebate.purchase_order_count, 2)

    def test_draft_orders_not_counted(self):
        """Непідтверджені замовлення НЕ входять в оборот."""
        self._create_purchase(self.vendor, 100000.0, '2026-02-10 10:00:00')
        # Чернетка — не має враховуватись
        self._create_purchase(self.vendor, 999999.0, '2026-02-15 10:00:00',
                              confirm=False)

        rebate = self.rebate_model.create({
            'vendor_id': self.vendor.id,
            'date_from': date(2026, 1, 1),
            'date_to': date(2026, 3, 31),
        })
        self.assertAlmostEqual(rebate.purchase_amount, 100000.0, places=2)

    def test_orders_outside_period_not_counted(self):
        """Замовлення поза періодом не входять у розрахунок.

        Це перевірка МЕЖ періоду. Особливо важливі дати 31.03 та 01.04:
        замовлення від 31 березня має потрапити в Q1, а від 1 квітня —
        уже в Q2.
        """
        self._create_purchase(self.vendor, 100000.0, '2026-03-31 23:00:00')  # Q1 ✓
        self._create_purchase(self.vendor, 500000.0, '2026-04-01 00:30:00')  # Q2 ✗

        rebate = self.rebate_model.create({
            'vendor_id': self.vendor.id,
            'date_from': date(2026, 1, 1),
            'date_to': date(2026, 3, 31),
        })
        self.assertAlmostEqual(
            rebate.purchase_amount, 100000.0, places=2,
            msg='Замовлення від 1 квітня не має потрапляти в період Q1',
        )

    def test_percent_taken_from_vendor_card(self):
        """Якщо відсоток не задано — підставляється з картки вендора."""
        rebate = self.rebate_model.create({
            'vendor_id': self.vendor.id,
            'date_from': date(2026, 1, 1),
            'date_to': date(2026, 3, 31),
        })
        self.assertAlmostEqual(rebate.rebate_percent, 5.0, places=2)

    def test_percent_can_be_overridden_per_period(self):
        """Відсоток можна змінити для конкретного періоду."""
        rebate = self.rebate_model.create({
            'vendor_id': self.vendor.id,
            'date_from': date(2026, 1, 1),
            'date_to': date(2026, 3, 31),
            'rebate_percent': 8.0,      # домовленість на цей квартал
        })
        self.assertAlmostEqual(rebate.rebate_percent, 8.0, places=2)

    def test_rebate_gets_sequence_number(self):
        rebate = self.rebate_model.create({
            'vendor_id': self.vendor.id,
            'date_from': date(2026, 1, 1),
            'date_to': date(2026, 3, 31),
        })
        self.assertTrue(rebate.name.startswith('RB-'),
                        f'Очікували RB-XXXXX, отримали {rebate.name}')

    # =========================================================================
    #  АВТОМАТИЧНЕ СТВОРЕННЯ ЗА КВАРТАЛ (FR-F05)
    # =========================================================================

    def test_cron_creates_rebate_for_vendor_with_percent(self):
        """Cron створює нарахування лише для вендорів із відсотком."""
        created = self.rebate_model._cron_generate_quarterly_rebates()

        self.assertGreaterEqual(created, 1)

        rebate = self.rebate_model.search([
            ('vendor_id', '=', self.vendor.id),
        ], limit=1)
        self.assertTrue(rebate, 'Для вендора з відсотком має створитись нарахування')

        # Для вендора БЕЗ відсотка нарахування не створюється
        no_rebate = self.rebate_model.search([
            ('vendor_id', '=', self.vendor_no_rebate.id),
        ])
        self.assertFalse(no_rebate,
                         'Без домовленості про бонус нарахування не потрібне')

    def test_cron_is_idempotent(self):
        """Повторний запуск cron НЕ створює дублів.

        Це головна вимога до будь-якої cron-задачі. Перевіряємо і
        кількість нарахувань, і те, що друге створення не впало
        з помилкою цілісності.
        """
        self.rebate_model._cron_generate_quarterly_rebates()
        count_after_first = self.rebate_model.search_count([
            ('vendor_id', '=', self.vendor.id),
        ])

        self.rebate_model._cron_generate_quarterly_rebates()
        count_after_second = self.rebate_model.search_count([
            ('vendor_id', '=', self.vendor.id),
        ])

        self.assertEqual(count_after_first, count_after_second,
                         'Повторний запуск cron не має створювати нових нарахувань')
        self.assertEqual(count_after_first, 1)

    def test_cron_recalculates_draft_but_not_confirmed(self):
        """Cron перераховує чернетки, але НЕ чіпає підтверджені.

        Це принципово: підтверджене нарахування — грошове зобов'язання,
        воно не має змінюватись заднім числом.
        """
        self.rebate_model._cron_generate_quarterly_rebates()
        rebate = self.rebate_model.search([('vendor_id', '=', self.vendor.id)], limit=1)

        # Додаємо закупівлю в минулому кварталі — чернетка має перерахуватись
        period_from, period_to = self.rebate_model._get_previous_quarter_dates(
            __import__('odoo').fields.Date.context_today(self.rebate_model))
        self._create_purchase(
            self.vendor, 200000.0,
            f'{period_from} 10:00:00')

        self.rebate_model._cron_generate_quarterly_rebates()
        self.assertAlmostEqual(rebate.purchase_amount, 200000.0, places=2,
                               msg='Чернетка має перерахуватись')

        # Підтверджуємо і додаємо ще одну закупівлю
        rebate.action_confirm()
        self._create_purchase(self.vendor, 100000.0, f'{period_from} 12:00:00')

        self.rebate_model._cron_generate_quarterly_rebates()
        self.assertAlmostEqual(
            rebate.purchase_amount, 200000.0, places=2,
            msg='Підтверджене нарахування НЕ має перераховуватись: '
                'бонус — це грошове зобовʼязання, воно не «пливе»',
        )

    # =========================================================================
    #  ЖИТТЄВИЙ ЦИКЛ
    # =========================================================================

    def test_full_lifecycle(self):
        self._create_purchase(self.vendor, 100000.0, '2026-02-10 10:00:00')
        rebate = self.rebate_model.create({
            'vendor_id': self.vendor.id,
            'date_from': date(2026, 1, 1),
            'date_to': date(2026, 3, 31),
        })

        rebate.action_confirm()
        self.assertEqual(rebate.state, 'confirmed')

        rebate.action_mark_invoiced()
        self.assertEqual(rebate.state, 'invoiced')

        rebate.action_mark_received()
        self.assertEqual(rebate.state, 'received')

    def test_cannot_confirm_empty_rebate(self):
        """Нарахування з нульовим оборотом не можна підтвердити."""
        rebate = self.rebate_model.create({
            'vendor_id': self.vendor.id,
            'date_from': date(2020, 1, 1),
            'date_to': date(2020, 3, 31),   # у цьому періоді закупівель немає
        })
        with self.assertRaises(UserError):
            rebate.action_confirm()

    def test_duplicate_period_is_rejected(self):
        """Одне нарахування на вендора за період."""
        from psycopg2 import IntegrityError
        self.rebate_model.create({
            'vendor_id': self.vendor.id,
            'date_from': date(2026, 1, 1),
            'date_to': date(2026, 3, 31),
        })
        with self.assertRaises(IntegrityError):
            self.rebate_model.create({
                'vendor_id': self.vendor.id,
                'date_from': date(2026, 1, 1),
                'date_to': date(2026, 3, 31),
            })
