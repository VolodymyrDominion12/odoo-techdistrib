# -*- coding: utf-8 -*-
# =============================================================================
#  Тести дилерської програми
# =============================================================================
#  ЯК ПРАЦЮЄ ТЕСТУВАННЯ В ODOO
#
#  Базовий клас TransactionCase — це «пісочниця»:
#    * перед кожним тестом Odoo відкриває транзакцію;
#    * після тесту — ЗАВЖДИ відкочує її (ROLLBACK);
#    * тому тести не бачать даних один одного і не псують базу.
#
#  ЗВІДСИ ГОЛОВНЕ ПРАВИЛО: у тестах НІКОЛИ не викликай env.cr.commit().
#  Він зламає відкат, і «брудні» дані потечуть у наступні тести —
#  отримаєш помилки, які неможливо відтворити.
#
#  Атрибут @tagged:
#    'post_install' — запускати ПІСЛЯ встановлення всіх модулів.
#                     Для бізнес-логіки це майже завжди правильно: усі
#                     залежності вже на місці, оточення реалістичне.
#    '-at_install'  — НЕ запускати на етапі встановлення модуля.
#    'techdistrib'  — власний тег, щоб можна було запускати
#                     ./scripts/run_tests.sh techdistrib
# =============================================================================

from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install', 'techdistrib')
class TestDealerProgram(TransactionCase):
    """Перевірка дилерської програми: коди, рівні, метрики, cron."""

    @classmethod
    def setUpClass(cls):
        """Підготовка даних ДЛЯ ВСІХ тестів класу.

        setUpClass викликається ОДИН раз на клас (а не на кожен тест),
        тому він швидший за setUp. Але пам'ятай: зміни, зроблені в одному
        тесті, не потраплять в інший — бо кожен тест має власний відкат.
        Тому в setUpClass створюємо лише те, що не змінюється.
        """
        # super() обов'язковий: TransactionCase у своєму setUpClass
        # готує env, компанію, адміністратора тощо.
        super().setUpClass()

        cls.partner_model = cls.env['res.partner']
        cls.tier_model = cls.env['techdistrib.dealer.tier']

        # env.ref() дістає запис за його XML ID.
        # Формат: '<ім'я_модуля>.<id_у_файлі_даних>'
        cls.tier_bronze = cls.env.ref('techdistrib_base.dealer_tier_bronze')
        cls.tier_silver = cls.env.ref('techdistrib_base.dealer_tier_silver')
        cls.tier_gold = cls.env.ref('techdistrib_base.dealer_tier_gold')
        cls.tier_platinum = cls.env.ref('techdistrib_base.dealer_tier_platinum')

        # Тестовий товар — потрібен для створення замовлень.
        # type='consu' — «звичайний товар» (у Odoo 18 немає окремого
        # типу 'product'; є consu / service / combo).
        cls.product = cls.env['product.product'].create({
            'name': 'Тестовий сервер',
            'type': 'consu',
            'list_price': 1000.0,
        })

    # --- Допоміжні методи (не тести: імена не починаються з test_) -----------

    def _create_dealer(self, **kwargs):
        """Створити дилера з розумними типовими значеннями."""
        vals = {
            'name': kwargs.pop('name', 'ТОВ Тестовий Дилер'),
            'is_dealer': True,
        }
        vals.update(kwargs)
        return self.partner_model.create(vals)

    def _create_confirmed_order(self, partner, months_ago=0, amount=1000.0):
        """Створити ПІДТВЕРДЖЕНЕ замовлення із заданою датою.

        Тонкість, на якій легко спіткнутися:
        sale.order.action_confirm() ПЕРЕЗАПИСУЄ date_order на поточний
        момент (метод _prepare_confirmation_values). Тому задати стару дату
        «наперед» не вийде — її затруть при підтвердженні.

        Правильний порядок: спершу підтвердити, ПОТІМ пересунути дату назад.
        Поле date_order має readonly=True, але це обмеження ЛИШЕ інтерфейсу:
        із Python його писати можна.
        """
        order = self.env['sale.order'].create({
            'partner_id': partner.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_qty': 1,
                'price_unit': amount,
            })],
        })
        order.action_confirm()

        if months_ago:
            order.date_order = fields.Datetime.now() - relativedelta(months=months_ago)
        return order

    # =========================================================================
    #  ТЕСТИ: КОД ДИЛЕРА
    # =========================================================================

    def test_dealer_code_is_generated(self):
        """FR-A04: код дилера присвоюється автоматично у форматі DLR-XXXXX."""
        dealer = self._create_dealer()
        self.assertTrue(dealer.dealer_code, 'Код дилера має бути присвоєний')
        self.assertTrue(
            dealer.dealer_code.startswith('DLR-'),
            f'Код має починатись з DLR-, отримано: {dealer.dealer_code}',
        )
        self.assertEqual(len(dealer.dealer_code), 9,  # 'DLR-' + 5 цифр
                         f'Очікували DLR-00001, отримали {dealer.dealer_code}')

    def test_dealer_codes_are_unique(self):
        """Кожен дилер отримує УНІКАЛЬНИЙ код (перевірка послідовності)."""
        dealer_a = self._create_dealer(name='Дилер А')
        dealer_b = self._create_dealer(name='Дилер Б')
        self.assertNotEqual(dealer_a.dealer_code, dealer_b.dealer_code)

    def test_non_dealer_has_no_code(self):
        """Звичайний клієнт не отримує код дилера."""
        client = self.partner_model.create({'name': 'Звичайний клієнт'})
        self.assertFalse(client.dealer_code)
        self.assertFalse(client.is_dealer)

    def test_partner_becomes_dealer_on_write(self):
        """Партнера можна зробити дилером після створення (write-гілка)."""
        partner = self.partner_model.create({'name': 'Майбутній дилер'})
        self.assertFalse(partner.dealer_code)

        # Це той самий шлях, яким іде кнопка «Зробити дилером» на формі
        partner.write({'is_dealer': True})

        self.assertTrue(partner.dealer_code, 'Код має зʼявитись після write')
        self.assertTrue(partner.dealer_since,
                        'Дата початку партнерства має проставитись автоматично')

    def test_dealer_code_sequences_across_creation(self):
        """Коди йдуть послідовно: наступний більший за попередній."""
        first = self._create_dealer(name='Перший')
        second = self._create_dealer(name='Другий')
        self.assertLess(first.dealer_code, second.dealer_code,
                        'Коди мають зростати (рядкове порівняння працює, '
                        'бо padding фіксованої довжини)')

    # =========================================================================
    #  ТЕСТИ: СТАЖ І СТАТУС
    # =========================================================================

    def test_experience_months(self):
        """FR-A05: стаж рахується від дати початку партнерства."""
        today = fields.Date.context_today(self.partner_model)
        dealer = self._create_dealer(dealer_since=today - relativedelta(months=14))
        self.assertEqual(dealer.dealer_experience_months, 14)

    def test_status_new_without_sales(self):
        """FR-A06: дилер без продажів і з недавнім стажем — «Новий»."""
        today = fields.Date.context_today(self.partner_model)
        dealer = self._create_dealer(dealer_since=today - relativedelta(months=1))
        self.assertEqual(dealer.dealer_status, 'new')

    def test_status_active_after_sale(self):
        """Дилер, який щойно купив, стає «Активним»."""
        dealer = self._create_dealer()
        self._create_confirmed_order(dealer, months_ago=0)

        # Порівнюємо лише дату, бо date_order містить і час
        self.assertTrue(dealer.last_sale_date,
                        'Підтверджене замовлення має заповнити last_sale_date')
        self.assertEqual(dealer.dealer_status, 'active')

    def test_status_dormant_after_long_silence(self):
        """Дилер без покупок понад 6 місяців — «Сплячий»."""
        dealer = self._create_dealer()
        self._create_confirmed_order(dealer, months_ago=8)
        self.assertEqual(dealer.dealer_status, 'dormant')

    def test_draft_order_does_not_count_as_sale(self):
        """Чернетка (комерційна пропозиція) НЕ вважається продажею."""
        dealer = self._create_dealer()
        self.env['sale.order'].create({
            'partner_id': dealer.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_qty': 1,
                'price_unit': 5000.0,
            })],
        })
        self.assertFalse(dealer.last_sale_date,
                         'Непідтверджене замовлення не має впливати на метрики')
        self.assertEqual(dealer.dealer_confirmed_sales, 0.0)

    def test_sales_metrics_aggregate(self):
        """Метрики сумують кілька підтверджених замовлень.

        УВАГА НА УРОК: спочатку цей тест порівнював із жорстко зашитим
        числом 3500 і ВПАВ — бо сума з податком виявилась 4025 (ПДВ 15 %).

        Правильний підхід до тестів: не зашивати магічні числа, а
        обчислювати очікуване значення з тих самих даних, які створив тест.
        Тоді тест не залежить від налаштувань податків, валюти чи локалізації
        і перевіряє саме ЛОГІКУ, а не конфігурацію бази.
        """
        dealer = self._create_dealer()
        order_a = self._create_confirmed_order(dealer, amount=1000.0)
        order_b = self._create_confirmed_order(dealer, amount=2500.0)

        expected = order_a.amount_untaxed + order_b.amount_untaxed

        self.assertEqual(dealer.dealer_confirmed_order_count, 2)
        self.assertAlmostEqual(dealer.dealer_confirmed_sales, expected, places=2)
        # І окремо перевіряємо, що це саме сума БЕЗ податку
        self.assertAlmostEqual(
            dealer.dealer_confirmed_sales,
            order_a.amount_untaxed + order_b.amount_untaxed,
            places=2,
            msg='Оборот дилера має рахуватись без ПДВ',
        )

    # =========================================================================
    #  ТЕСТИ: ПЕРЕВІРКИ ТА ОБМЕЖЕННЯ
    # =========================================================================

    def test_tier_requires_dealer_flag(self):
        """FR-A03: рівень не можна вказати партнеру, який не є дилером."""
        with self.assertRaises(ValidationError):
            self.partner_model.create({
                'name': 'Не дилер із рівнем',
                'is_dealer': False,
                'dealer_tier_id': self.tier_gold.id,
            })

    def test_tier_code_is_unique(self):
        """SQL-обмеження: код рівня унікальний.

        ⚠️ У логах ти побачиш страшний рядок:
            ERROR odoo.sql_db: bad query: INSERT INTO "techdistrib_dealer_tier" ...
            ERROR: duplicate key value violates unique constraint
        ЦЕ ОЧІКУВАНО і не є провалом тесту. Так працює перевірка обмежень
        на рівні бази: Postgres відхиляє вставку, Odoo перетворює це на
        помилку, а assertRaises її ловить. Тест при цьому — PASS.
        """
        from psycopg2 import IntegrityError
        with self.assertRaises(IntegrityError):
            self.tier_model.create({'name': 'Дубль', 'code': 'gold'})
        # Odoo обгортає такі місця в savepoint автоматично під час тестів,
        # тому транзакція не псується і тест може продовжуватись.
        # У реальному коді для «мʼякої» перевірки краще використовувати
        # @api.constrains і ValidationError — він дає зрозуміле повідомлення
        # користувачу замість технічного тексту Postgres.

    # =========================================================================
    #  ТЕСТИ: CRON-ЗАДАЧА ЗНИЖЕННЯ РІВНЯ
    # =========================================================================

    def test_cron_downgrades_dormant_dealer(self):
        """FR-A07: cron знижує рівень дилера на один щабель."""
        today = fields.Date.context_today(self.partner_model)
        dealer = self._create_dealer(
            name='Сплячий Gold',
            dealer_tier_id=self.tier_gold.id,
            dealer_since=today - relativedelta(months=18),
        )
        self._create_confirmed_order(dealer, months_ago=8)

        # Переконуємось, що передумови виконані
        self.assertEqual(dealer.dealer_status, 'dormant')
        self.assertEqual(dealer.dealer_tier_id, self.tier_gold)

        # Запускаємо задачу так само, як це робить планувальник Odoo
        self.partner_model._cron_update_dealer_status()

        self.assertEqual(
            dealer.dealer_tier_id, self.tier_silver,
            'Gold має знизитись рівно на один щабель — до Silver',
        )

    def test_cron_keeps_active_dealer(self):
        """Cron НЕ чіпає дилера зі свіжими продажами."""
        today = fields.Date.context_today(self.partner_model)
        dealer = self._create_dealer(
            name='Активний Gold',
            dealer_tier_id=self.tier_gold.id,
            dealer_since=today - relativedelta(months=18),
        )
        self._create_confirmed_order(dealer, months_ago=1)

        self.partner_model._cron_update_dealer_status()

        self.assertEqual(dealer.dealer_tier_id, self.tier_gold,
                         'Активний дилер не має втрачати рівень')

    def test_cron_does_not_downgrade_below_lowest(self):
        """Дилер на найнижчому рівні не «провалюється» нижче."""
        today = fields.Date.context_today(self.partner_model)
        dealer = self._create_dealer(
            name='Сплячий Bronze',
            dealer_tier_id=self.tier_bronze.id,
            dealer_since=today - relativedelta(months=18),
        )
        self._create_confirmed_order(dealer, months_ago=8)

        self.partner_model._cron_update_dealer_status()

        self.assertEqual(dealer.dealer_tier_id, self.tier_bronze,
                         'Нижче Bronze знижувати нема куди')

    def test_cron_downgrades_one_step_per_run(self):
        """Зниження відбувається РІВНО на один щабель за запуск.

        Це вимога ідемпотентності та передбачуваності: дилер не має
        «впасти» з Platinum одразу в Bronze за одну ніч.
        """
        today = fields.Date.context_today(self.partner_model)
        dealer = self._create_dealer(
            name='Сплячий Platinum',
            dealer_tier_id=self.tier_platinum.id,
            dealer_since=today - relativedelta(months=24),
        )
        self._create_confirmed_order(dealer, months_ago=12)

        self.partner_model._cron_update_dealer_status()
        self.assertEqual(dealer.dealer_tier_id, self.tier_gold,
                         'Після першого запуску: Platinum → Gold')

        self.partner_model._cron_update_dealer_status()
        self.assertEqual(dealer.dealer_tier_id, self.tier_silver,
                         'Після другого запуску: Gold → Silver')

    def test_cron_writes_message_in_chatter(self):
        """Зниження рівня фіксується в історії партнера (аудит)."""
        today = fields.Date.context_today(self.partner_model)
        dealer = self._create_dealer(
            name='Сплячий із повідомленням',
            dealer_tier_id=self.tier_gold.id,
            dealer_since=today - relativedelta(months=18),
        )
        self._create_confirmed_order(dealer, months_ago=8)

        messages_before = len(dealer.message_ids)
        self.partner_model._cron_update_dealer_status()

        self.assertGreater(
            len(dealer.message_ids), messages_before,
            'Має зʼявитись повідомлення в chatter із поясненням причини',
        )
