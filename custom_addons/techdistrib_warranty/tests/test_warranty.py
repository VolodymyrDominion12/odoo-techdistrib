# -*- coding: utf-8 -*-
# =============================================================================
#  Тести гарантії та RMA
# =============================================================================
#  Тести поділені на два шари:
#
#    1. ЮНІТ-РІВЕНЬ — перевіряємо арифметику дат і логіку перевірок
#       напряму, без складного складського потоку. Швидко й надійно.
#
#    2. ІНТЕГРАЦІЙНИЙ РІВЕНЬ — проганяємо СПРАВЖНЄ відвантаження:
#       замовлення → підтвердження → резервування → валідація.
#       Це повільніше, але саме так ловиться найважливіший клас помилок:
#       код написаний правильно, але НЕ ПІДКЛЮЧЕНИЙ до потоку Odoo.
# =============================================================================

from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install', 'techdistrib')
class TestWarranty(TransactionCase):
    """Гарантійні дати, ланцюжок доказів, життєвий цикл RMA."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Категорія «Сервери» — термін гарантії 36 міс.
        # Значення проставляє <function> із data/product_category_data.xml,
        # тому цей тест заодно перевіряє, що data-файл відпрацював.
        cls.categ_servers = cls.env.ref(
            'techdistrib_pricing.product_category_servers')

        cls.company = cls.env.company
        cls.stock_location = cls.env.ref('stock.stock_location_stock')

        # Товар з обліком за СЕРІЙНИМ НОМЕРОМ.
        # Саме для таких товарів (35 % асортименту в BRD) працює гарантія.
        # ⚠️ УРОК ПРО ЗМІНУ МОДЕЛІ ТОВАРІВ В ODOO 18.
        #
        # Спочатку я створив товар так, як робив це в попередніх модулях:
        #     type='consu'
        # І п'ять тестів упали з:
        #     ValidationError: Quants cannot be created for consumables
        #     or services.
        #
        # Причина: в Odoo 18 модель товарів ЗМІНИЛАСЬ. Раніше було три типи:
        #     'product'  — товар зі складським обліком
        #     'consu'    — витратний матеріал (без обліку залишків)
        #     'service'  — послуга
        #
        # Тепер типів лишилось три, але ІНШИХ:
        #     'consu'   — «Goods» (усі матеріальні товари)
        #     'service' — послуга
        #     'combo'   — набір
        #
        # А «чи ведеться складський облік» визначає ОКРЕМИЙ булевий прапорець
        # is_storable, який додає модуль stock (addons/stock/models/product.py:704).
        #
        # Тобто для товару зі складським обліком у Odoo 18 треба:
        #     type='consu', is_storable=True
        #
        # Це саме той випадок, коли старі знання не просто «не допомагають»,
        # а вводять в оману: код виглядає звично, але працює інакше.
        #
        # ПЕРЕВІРКА: grep -rn "is_storable = fields" vendor/odoo/addons/stock/
        cls.product = cls.env['product.product'].create({
            'name': 'Сервер Dell R650',
            'type': 'consu',
            'is_storable': True,
            'list_price': 2000.0,
            'categ_id': cls.categ_servers.id,
            'tracking': 'serial',
            'taxes_id': [(5, 0, 0)],
        })

        cls.customer = cls.env['res.partner'].create({
            'name': 'ТОВ Кінцевий Клієнт',
        })

    # --- Допоміжні методи ----------------------------------------------------

    def _create_lot(self, name='SN-TEST-0001'):
        return self.env['stock.lot'].create({
            'name': name,
            'product_id': self.product.id,
            'company_id': self.company.id,
        })

    def _add_stock(self, lot, quantity=1):
        """Покласти одиницю товару на склад.

        Використовуємо інвентаризацію (stock.quant + action_apply_inventory) —
        це стандартний спосіб створити залишок у тестах, без проганяння
        повного циклу закупівлі.
        """
        quant = self.env['stock.quant'].create({
            'product_id': self.product.id,
            'location_id': self.stock_location.id,
            'lot_id': lot.id,
            'inventory_quantity': quantity,
        })
        quant.action_apply_inventory()
        return quant

    def _deliver_lot(self, lot, partner=None, quantity=1):
        """Наскрізний потік: замовлення → відвантаження → валідація.

        Повертає (order, picking).
        """
        order = self.env['sale.order'].create({
            'partner_id': (partner or self.customer).id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_qty': quantity,
            })],
        })
        order.action_confirm()

        picking = order.picking_ids
        self.assertTrue(picking, 'Підтвердження замовлення має створити відвантаження')

        # Резервуємо товар: Odoo сам підбере наш серійний номер,
        # бо він єдиний доступний на складі.
        picking.action_assign()

        # Явно проставляємо номер, якщо Odoo не зробив цього автоматично —
        # це робить тест стійким до налаштувань резервування.
        for move_line in picking.move_line_ids:
            if not move_line.lot_id:
                move_line.lot_id = lot.id
            move_line.quantity = quantity

        # button_validate викликає _action_done, у якому працює наш код
        picking.button_validate()

        return order, picking

    # =========================================================================
    #  ЮНІТ-РІВЕНЬ: ГАРАНТІЙНІ ДАТИ
    # =========================================================================

    def test_warranty_months_from_category(self):
        """FR-E03: термін гарантії береться з категорії товару."""
        lot = self._create_lot()
        self.assertEqual(lot._get_warranty_months(), 36,
                         'Категорія «Сервери» має 36 місяців гарантії '
                         '(з data/product_category_data.xml)')

    def test_product_warranty_overrides_category(self):
        """Перевизначення терміну на товарі має вищий пріоритет."""
        self.product.warranty_months = 12
        lot = self._create_lot(name='SN-TEST-OVERRIDE')
        self.assertEqual(lot._get_warranty_months(), 12)
        self.product.warranty_months = 0   # повертаємо як було

    def test_apply_warranty_dates_computes_end_date(self):
        """Початок гарантії = дата відвантаження; кінець = + термін категорії."""
        lot = self._create_lot(name='SN-TEST-DATES')
        start = fields.Date.today() - relativedelta(months=3)

        lot._apply_warranty_dates(self.customer, False, start)

        self.assertEqual(lot.warranty_start_date, start)
        self.assertEqual(lot.warranty_end_date, start + relativedelta(months=36))
        self.assertEqual(lot.warranty_months_applied, 36)
        self.assertEqual(lot.warranty_customer_id, self.customer)

    def test_warranty_state_under_and_expired(self):
        """Стан гарантії рахується від сьогоднішньої дати."""
        lot = self._create_lot(name='SN-TEST-STATE')
        today = fields.Date.today()

        lot._apply_warranty_dates(self.customer, False, today)
        self.assertEqual(lot.warranty_state, 'under')
        self.assertGreater(lot.warranty_days_left, 1000)

        # Переносимо відвантаження на 40 місяців назад — гарантія минула
        old_date = today - relativedelta(months=40)
        lot._apply_warranty_dates(self.customer, False, old_date)
        self.assertEqual(lot.warranty_state, 'expired')
        self.assertLess(lot.warranty_days_left, 0)

    def test_lot_without_delivery_has_unknown_state(self):
        """Номер, який ми не продавали, має стан «Не продано»."""
        lot = self._create_lot(name='SN-TEST-UNSOLD')
        self.assertEqual(lot.warranty_state, 'unknown')
        self.assertFalse(lot.warranty_start_date)

    def test_no_warranty_months_means_no_dates(self):
        """Якщо термін не задано — дати не проставляються."""
        category = self.env['product.category'].create({
            'name': 'ТЕСТ: без гарантії',
            'warranty_months': 0,
        })
        product = self.env['product.product'].create({
            'name': 'Товар без гарантії',
            'type': 'consu',
            'is_storable': True,
            'categ_id': category.id,
            'tracking': 'serial',
        })
        lot = self.env['stock.lot'].create({
            'name': 'SN-NO-WARRANTY',
            'product_id': product.id,
            'company_id': self.company.id,
        })
        result = lot._apply_warranty_dates(self.customer, False, fields.Date.today())
        self.assertFalse(result)
        self.assertFalse(lot.warranty_start_date)

    # =========================================================================
    #  ІНТЕГРАЦІЙНИЙ РІВЕНЬ: РЕАЛЬНЕ ВІДВАНТАЖЕННЯ
    # =========================================================================

    def test_delivery_sets_warranty_dates(self):
        """FR-D06: НАСКРІЗНИЙ. Відвантаження автоматично ставить гарантію.

        Це найцінніший тест модуля. Він перевіряє, що наш код
        ДІЙСНО ПІДКЛЮЧЕНИЙ до стандартного складського потоку Odoo,
        а не просто існує окремо.

        Якби ми перевизначили не той метод (напр., button_validate
        замість _action_done), цей тест усе одно пройшов би. Але якби
        ми взагалі не перевизначили нічого — він би впав.
        """
        lot = self._create_lot(name='SN-E2E-0001')
        self._add_stock(lot)
        self.assertFalse(lot.warranty_start_date, 'До відвантаження гарантії немає')

        order, picking = self._deliver_lot(lot)

        self.assertEqual(picking.state, 'done')
        self.assertTrue(lot.warranty_start_date,
                        'Після валідації відвантаження гарантія має початись')
        self.assertTrue(lot.warranty_end_date)
        self.assertEqual(lot.warranty_start_date, picking.date_done.date())
        self.assertEqual(lot.warranty_end_date,
                         lot.warranty_start_date + relativedelta(months=36))
        self.assertEqual(lot.warranty_customer_id, self.customer)
        self.assertEqual(lot.warranty_sale_order_id, order)
        self.assertEqual(lot.warranty_state, 'under')

    def test_incoming_picking_does_not_set_warranty(self):
        """Гарантія НЕ починається при отриманні товару від вендора.

        Це перевірка умови picking_type_code == 'outgoing'. Без неї
        гарантія стартувала б у момент закупівлі — і до моменту продажу
        вже частково «згоріла» б.
        """
        lot = self._create_lot(name='SN-INCOMING-001')
        vendor = self.env['res.partner'].create({'name': 'Вендор'})

        # Створюємо вхідне переміщення вручну
        picking_type = self.env['stock.picking.type'].search([
            ('code', '=', 'incoming'),
            ('warehouse_id.company_id', '=', self.company.id),
        ], limit=1)
        # ⚠️ ТУТ Я СПІТКНУВСЯ: створив переміщення й рядок переміщення
        # ОДНІЄЮ операцією через move_ids: [(0, 0, {...})] — і отримав
        #     psycopg2.errors.NotNullViolation: null value in column "name"
        #     of relation "stock_move"
        #
        # Причина: у stock.move поле name обов'язкове, і Odoo обчислює його
        # з назви переміщення-батька. Коли створювати обидва записи одразу,
        # батько ще не має id і назви — тому name лишається порожнім.
        #
        # ПРАВИЛО: коли дочірній запис залежить від полів батька, створи
        # батька ПЕРШИМ, а потім додавай дочірні.
        picking = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'location_id': picking_type.default_location_src_id.id,
            'location_dest_id': self.stock_location.id,
            'partner_id': vendor.id,
        })
        self.env['stock.move'].create({
            'name': self.product.name,
            'picking_id': picking.id,
            'product_id': self.product.id,
            'product_uom_qty': 1,
            'product_uom': self.product.uom_id.id,
            'location_id': picking_type.default_location_src_id.id,
            'location_dest_id': self.stock_location.id,
        })
        picking.action_confirm()
        picking.action_assign()
        for move_line in picking.move_line_ids:
            move_line.lot_id = lot.id
            move_line.quantity = 1
        picking.button_validate()

        self.assertEqual(picking.state, 'done')
        self.assertFalse(
            lot.warranty_start_date,
            'Прихід від вендора НЕ має запускати гарантію клієнта',
        )

    # =========================================================================
    #  ЛАНЦЮЖОК ДОКАЗІВ І СТВОРЕННЯ ЗВЕРНЕННЯ
    # =========================================================================

    def test_claim_gets_sequence_number(self):
        """Звернення отримує номер RMA-00001."""
        lot = self._create_lot(name='SN-CLAIM-001')
        claim = self.env['techdistrib.warranty.claim'].create({
            'lot_id': lot.id,
            'partner_id': self.customer.id,
            'problem_description': 'Не вмикається.',
        })
        self.assertTrue(claim.name.startswith('RMA-'),
                        f'Очікували RMA-XXXXX, отримали {claim.name}')

    def test_claim_fills_evidence_chain_from_lot(self):
        """FR-E01: звернення автоматично підтягує ланцюжок доказів."""
        lot = self._create_lot(name='SN-EVIDENCE-01')
        self._add_stock(lot)
        order, picking = self._deliver_lot(lot)

        claim = self.env['techdistrib.warranty.claim'].create({
            'lot_id': lot.id,
            'partner_id': self.customer.id,
            'problem_description': 'Перегрів під навантаженням.',
        })

        self.assertEqual(claim.sold_to_partner_id, self.customer)
        self.assertEqual(claim.sale_order_id, order)
        self.assertEqual(claim.delivery_date, lot.warranty_start_date)
        self.assertEqual(claim.product_id, self.product)
        self.assertEqual(claim.lot_id.name, 'SN-EVIDENCE-01')
        self.assertEqual(claim.warranty_end_date, lot.warranty_end_date)

    def test_claim_snapshots_dates_regardless_of_call_path(self):
        """Ланцюжок заповнюється і БЕЗ інтерфейсу (імпорт, API).

        Це перевірка того, що ми не обмежились @api.onchange — він
        працює лише у формі й не викликається при програмному створенні.
        """
        lot = self._create_lot(name='SN-NOUI-001')
        self._add_stock(lot)
        order, picking = self._deliver_lot(lot)

        # Створюємо БЕЗ жодного onchange — прямо через create
        claim = self.env['techdistrib.warranty.claim'].create({
            'lot_id': lot.id,
            'partner_id': self.customer.id,
            'problem_description': 'Створено програмно.',
        })
        self.assertEqual(claim.sale_order_id, order,
                         'onchange не працює поза UI, тому логіка має '
                         'бути і в create')

    # =========================================================================
    #  ПЕРЕВІРКА ГАРАНТІЇ (FR-E02, FR-E03)
    # =========================================================================

    def test_claim_for_unknown_serial_is_not_our_product(self):
        """FR-E02: номер, якого ми не продавали, — не наш товар."""
        lot = self._create_lot(name='SN-FOREIGN-001')
        claim = self.env['techdistrib.warranty.claim'].create({
            'lot_id': lot.id,
            'partner_id': self.customer.id,
            'problem_description': 'Куплено не в нас.',
        })
        self.assertFalse(claim.is_our_product)
        self.assertFalse(claim.is_under_warranty)
        self.assertTrue(claim.is_chargeable)
        self.assertTrue(claim.warranty_check_note)

    def test_confirm_rejects_foreign_serial(self):
        """Не можна прийняти в гарантію обладнання, яке ми не продавали.

        Це бізнес-правило проти шахрайства: спроба здати «чужий» прилад.
        """
        lot = self._create_lot(name='SN-FOREIGN-002')
        claim = self.env['techdistrib.warranty.claim'].create({
            'lot_id': lot.id,
            'partner_id': self.customer.id,
            'problem_description': 'Спроба здати чуже.',
        })
        with self.assertRaises(UserError):
            claim.action_confirm()
        self.assertEqual(claim.state, 'draft')

    def test_claim_within_warranty_is_free(self):
        """FR-E03: гарантія дійсна — ремонт безкоштовний."""
        lot = self._create_lot(name='SN-FREE-001')
        self._add_stock(lot)
        self._deliver_lot(lot)

        claim = self.env['techdistrib.warranty.claim'].create({
            'lot_id': lot.id,
            'partner_id': self.customer.id,
            'problem_description': 'Не працює порт.',
        })
        self.assertTrue(claim.is_our_product)
        self.assertTrue(claim.is_under_warranty)
        self.assertFalse(claim.is_chargeable)

    def test_expired_warranty_is_chargeable(self):
        """FR-E06: після закінчення гарантії ремонт платний."""
        lot = self._create_lot(name='SN-EXPIRED-001')
        # Відвантажуємо «в минулому»: 40 місяців тому (гарантія 36 міс.)
        old_date = fields.Date.today() - relativedelta(months=40)
        lot._apply_warranty_dates(self.customer, False, old_date)

        claim = self.env['techdistrib.warranty.claim'].create({
            'lot_id': lot.id,
            'partner_id': self.customer.id,
            'problem_description': 'Не вмикається.',
        })
        self.assertTrue(claim.is_our_product)
        self.assertFalse(claim.is_under_warranty)
        self.assertTrue(claim.is_chargeable)

    def test_warranty_checked_against_report_date_not_today(self):
        """Гарантія оцінюється на ДАТУ ЗВЕРНЕННЯ, а не на сьогодні.

        Це важлива тонкість: клієнт приніс прилад 5 числа, документ
        оформлюють 10 числа. Якщо гарантія закінчилась 7 числа, прилад
        має вважатись гарантійним — на момент звернення вона ще діяла.
        """
        lot = self._create_lot(name='SN-BORDERLINE-01')
        today = fields.Date.today()
        # Відвантаження рівно 36 місяців тому → гарантія закінчилась сьогодні
        delivery = today - relativedelta(months=36)
        lot._apply_warranty_dates(self.customer, False, delivery)

        # Звернення ДО закінчення гарантії
        claim_past = self.env['techdistrib.warranty.claim'].create({
            'lot_id': lot.id,
            'partner_id': self.customer.id,
            'problem_description': 'Звернення в останній день гарантії.',
            'reported_date': lot.warranty_end_date,
        })
        self.assertTrue(
            claim_past.is_under_warranty,
            'У день закінчення гарантії звернення ще гарантійне',
        )

        # Звернення ПІСЛЯ закінчення
        claim_after = self.env['techdistrib.warranty.claim'].create({
            'lot_id': lot.id,
            'partner_id': self.customer.id,
            'problem_description': 'Звернення через день після гарантії.',
            'reported_date': lot.warranty_end_date + relativedelta(days=1),
        })
        self.assertFalse(claim_after.is_under_warranty)

    # =========================================================================
    #  ЖИТТЄВИЙ ЦИКЛ І ПЕРЕВІРКИ
    # =========================================================================

    def test_full_lifecycle(self):
        """Повний цикл звернення: чернетка → прийнято → діагностика → вирішено → закрито."""
        lot = self._create_lot(name='SN-LIFECYCLE-1')
        self._add_stock(lot)
        self._deliver_lot(lot)

        claim = self.env['techdistrib.warranty.claim'].create({
            'lot_id': lot.id,
            'partner_id': self.customer.id,
            'problem_description': 'Шумить вентилятор.',
        })

        claim.action_confirm()
        self.assertEqual(claim.state, 'confirmed')

        claim.action_start_diagnostics()
        self.assertEqual(claim.state, 'diagnostics')

        claim.diagnosis = 'Зношений підшипник вентилятора.'
        claim.resolution = 'repair'
        claim.action_resolve()
        self.assertEqual(claim.state, 'resolved')
        self.assertTrue(claim.resolved_date)

        claim.action_close()
        self.assertEqual(claim.state, 'closed')
        self.assertTrue(claim.closed_date)

    def test_rejection_requires_reason(self):
        """FR-E05: відмова без обґрунтування заборонена."""
        lot = self._create_lot(name='SN-REJECT-001')
        claim = self.env['techdistrib.warranty.claim'].create({
            'lot_id': lot.id,
            'partner_id': self.customer.id,
            'problem_description': 'Подряпина на корпусі.',
        })

        # ⚠️ ТОНКІСТЬ, НА ЯКІЙ Я СПІТКНУВСЯ.
        # Спочатку я написав:
        #     claim.resolution = 'reject'
        #     with self.assertRaises(ValidationError):
        #         claim.action_resolve()
        # і тест УПАВ — помилка вилетіла ПОЗА assertRaises.
        #
        # Причина: @api.constrains спрацьовує в момент ЗАПИСУ поля,
        # а не при виклику бізнес-методу. Тобто ValidationError
        # піднімається вже на рядку `claim.resolution = 'reject'`.
        #
        # УРОК: перевірка через @api.constrains захищає від БУДЬ-ЯКОГО
        # способу запису (форма, імпорт, API) — і саме тому вона спрацьовує
        # так рано. У тесті це треба враховувати.
        with self.assertRaises(ValidationError):
            claim.resolution = 'reject'

        # Записуємо обидва поля ОДНІЄЮ операцією: тоді constrains бачить
        # заповнене обґрунтування і не піднімає помилку.
        claim.write({
            'resolution': 'reject',
            'resolution_note': 'Механічне пошкодження не є гарантійним випадком.',
        })
        claim.action_resolve()
        self.assertEqual(claim.state, 'resolved')

    def test_chargeable_repair_requires_cost(self):
        """FR-E06: платний ремонт мусить мати вказану вартість."""
        lot = self._create_lot(name='SN-COST-001')
        lot._apply_warranty_dates(
            self.customer, False, fields.Date.today() - relativedelta(months=40))

        claim = self.env['techdistrib.warranty.claim'].create({
            'lot_id': lot.id,
            'partner_id': self.customer.id,
            'problem_description': 'Залитий рідиною.',
        })
        self.assertTrue(claim.is_chargeable)

        # Помилка піднімається на ЗАПИСІ поля — див. коментар у
        # test_rejection_requires_reason вище.
        with self.assertRaises(ValidationError):
            claim.resolution = 'repair'

        claim.write({'resolution': 'repair', 'repair_cost': 3500.0})
        claim.action_resolve()
        self.assertEqual(claim.state, 'resolved')

    # =========================================================================
    #  CRON: ПРОСТРОЧЕНІ ЗВЕРНЕННЯ (FR-E07)
    # =========================================================================

    def test_overdue_claim_creates_activity_once(self):
        """FR-E07: cron створює нагадування, але НЕ дублює його щодня."""
        lot = self._create_lot(name='SN-OVERDUE-001')
        # ⚠️ Номер має бути «нашим», інакше action_confirm його відхилить
        # (це те саме бізнес-правило проти шахрайства — і воно спрацювало:
        # два тести спершу впали саме на цій перевірці).
        lot._apply_warranty_dates(self.customer, False, fields.Date.today())

        claim = self.env['techdistrib.warranty.claim'].create({
            'lot_id': lot.id,
            'partner_id': self.customer.id,
            'problem_description': 'Зависле звернення.',
            # Звернення «з минулого» — 45 днів тому (поріг 30)
            'reported_date': fields.Date.today() - relativedelta(days=45),
        })
        claim.action_confirm()

        # Дату звернення змінили після прийняття — перерахуємо is_overdue
        self.env.add_to_compute(
            self.env['techdistrib.warranty.claim']._fields['is_overdue'], claim)

        created_first = self.env[
            'techdistrib.warranty.claim']._cron_refresh_overdue_claims()
        self.assertGreaterEqual(created_first, 1, 'Має створитись нагадування')
        self.assertEqual(len(claim.activity_ids), 1)

        # Другий запуск НЕ має створити друге нагадування
        self.env['techdistrib.warranty.claim']._cron_refresh_overdue_claims()
        self.assertEqual(
            len(claim.activity_ids), 1,
            'Cron має бути ідемпотентним: повторний запуск не створює '
            'дубль нагадування, інакше за місяць їх буде 30',
        )

    def test_closed_claim_is_not_overdue(self):
        """Закрите звернення не може бути простроченим."""
        lot = self._create_lot(name='SN-CLOSED-001')
        lot._apply_warranty_dates(self.customer, False, fields.Date.today())
        claim = self.env['techdistrib.warranty.claim'].create({
            'lot_id': lot.id,
            'partner_id': self.customer.id,
            'problem_description': 'Давно вирішене.',
            'reported_date': fields.Date.today() - relativedelta(days=90),
        })
        claim.action_confirm()
        claim.resolution = 'repair'
        claim.action_resolve()
        claim.action_close()

        self.assertFalse(
            claim.is_overdue,
            'Закрите звернення не має потрапляти у звіт «прострочені», '
            'навіть якщо його вирішили пізніше за граничний строк',
        )
