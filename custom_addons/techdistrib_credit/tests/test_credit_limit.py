# -*- coding: utf-8 -*-
# =============================================================================
#  Тести кредитної політики
# =============================================================================
#  Це найважливіші тести проєкту, бо тут перевіряється те, чого в Odoo
#  НЕ БУЛО: жорстке блокування відвантаження.
#
#  Якщо ці тести зелені — бізнес-вимоги FR-C01…FR-C08 справді виконані.
# =============================================================================

from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install', 'techdistrib')
class TestCreditLimit(TransactionCase):
    """Блокування замовлень, погодження перевищень, прострочена дебіторка."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # --- 1. Бухгалтерський контекст --------------------------------------
        # Щоб тестувати прострочену дебіторку, потрібні СПРАВЖНІ проведені
        # рахунки. А для цього потрібен план рахунків — у чистій базі
        # (--without-demo=all) його немає.
        #
        # Це стандартний прийом Odoo: завантажити узагальнений план
        # рахунків 'generic_coa' на тестову компанію.
        # Подивись, як це роблять тести самого Odoo:
        #     addons/account/tests/common.py, метод _use_chart_template
        cls.env['account.chart.template'].try_loading(
            'generic_coa', company=cls.env.company, install_demo=False,
        )

        # --- 2. Права на бухгалтерські поля ----------------------------------
        # Поле res.partner.credit_limit оголошене з
        #     groups='account.group_account_invoice,account.group_account_readonly'
        # Тому без цих груп його НЕ МОЖНА ні читати, ні писати.
        #
        # Даємо тестовому користувачу потрібну групу явно — щоб тест
        # не залежав від того, які групи адміністратор має за замовчуванням.
        cls.env.user.groups_id = [
            (4, cls.env.ref('account.group_account_invoice').id),
        ]

        # --- 3. Перемикач кредитних лімітів ----------------------------------
        # Наш модуль вмикає його через data/res_company_data.xml.
        # Перевіряємо і, про всяк випадок, виставляємо явно.
        cls.env.company.account_use_credit_limit = True

        # --- 4. Довідкові дані ------------------------------------------------
        cls.tier_gold = cls.env.ref('techdistrib_base.dealer_tier_gold')

        cls.dealer = cls.env['res.partner'].create({
            'name': 'ТОВ Тестовий Дилер',
            'is_dealer': True,
            'dealer_tier_id': cls.tier_gold.id,
            'credit_limit': 100000.0,
        })

        # Окремий дилер БЕЗ ліміту — для перевірки семантики «0 = без ліміту»
        cls.dealer_no_limit = cls.env['res.partner'].create({
            'name': 'ТОВ Дилер без ліміту',
            'is_dealer': True,
        })

        cls.product = cls.env['product.product'].create({
            'name': 'Тестовий сервер',
            'type': 'consu',
            'list_price': 1000.0,
        })

        # --- 5. Користувачі з різними правами --------------------------------
        # Кредитний контролер — має право схвалювати
        cls.user_controller = cls.env['res.users'].create({
            'name': 'Кредитний контролер',
            'login': 'credit_controller_test',
            'email': 'controller@techdistrib.test',
            'groups_id': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('techdistrib_credit.group_credit_controller').id,
            ])],
        })

        # Звичайний менеджер — НЕ має права схвалювати
        cls.user_manager = cls.env['res.users'].create({
            'name': 'Менеджер з продажу',
            'login': 'sales_manager_test',
            'email': 'manager@techdistrib.test',
            'groups_id': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('sales_team.group_sale_salesman').id,
            ])],
        })

    # --- Допоміжні методи ----------------------------------------------------

    def _create_order(self, partner=None, amount=1000.0):
        """Створити ЧЕРНЕТКУ замовлення (ще не підтверджене)."""
        return self.env['sale.order'].create({
            'partner_id': (partner or self.dealer).id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_qty': 1,
                'price_unit': amount,
            })],
        })

    def _create_overdue_invoice(self, partner, amount=5000.0, days_overdue=45):
        """Створити ПРОСТРОЧЕНИЙ проведений рахунок.

        Це дає реальний борг у res.partner.credit — без жодних моків
        і підробок. Саме тому ми завантажили план рахунків.
        """
        today = fields.Date.context_today(self.env['account.move'])
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'invoice_date': today - relativedelta(days=days_overdue),
            'invoice_date_due': today - relativedelta(days=days_overdue),
            'line_ids': [(0, 0, {
                'product_id': self.product.id,
                'quantity': 1,
                'price_unit': amount,
            })],
        })
        # action_post проводить документ: створюються бухгалтерські проводки,
        # і сума потрапляє в дебіторку партнера.
        invoice.action_post()
        return invoice

    # =========================================================================
    #  ТЕСТИ: СТВОРЕННЯ ЗАПИТУ
    # =========================================================================

    def test_request_gets_sequence_number(self):
        """FR-C05: запит отримує номер із послідовності (CR-00001)."""
        request = self.env['techdistrib.credit.request'].create({
            'partner_id': self.dealer.id,
        })
        self.assertTrue(request.name.startswith('CR-'),
                        f'Очікували CR-XXXXX, отримали {request.name}')
        self.assertEqual(request.state, 'pending')

    def test_excess_amount_computed(self):
        """FR-C04: система рахує, на скільки саме перевищено ліміт."""
        request = self.env['techdistrib.credit.request'].create({
            'partner_id': self.dealer.id,
            'credit_limit': 100000.0,
            'current_debt': 80000.0,
            'order_amount': 50000.0,
        })
        self.assertAlmostEqual(request.total_exposure, 130000.0, places=2)
        self.assertAlmostEqual(request.excess_amount, 30000.0, places=2)

    def test_no_excess_when_within_limit(self):
        """Якщо ліміт не перевищено, сума перевищення = 0."""
        request = self.env['techdistrib.credit.request'].create({
            'partner_id': self.dealer.id,
            'credit_limit': 100000.0,
            'current_debt': 10000.0,
            'order_amount': 20000.0,
        })
        self.assertAlmostEqual(request.excess_amount, 0.0, places=2)

    def test_zero_limit_means_no_excess(self):
        """Ліміт 0 у Odoo означає «без ліміту» — перевищення теж 0."""
        request = self.env['techdistrib.credit.request'].create({
            'partner_id': self.dealer.id,
            'credit_limit': 0.0,
            'current_debt': 999999.0,
            'order_amount': 999999.0,
        })
        self.assertAlmostEqual(request.excess_amount, 0.0, places=2)

    # =========================================================================
    #  ТЕСТИ: БЛОКУВАННЯ ПІДТВЕРДЖЕННЯ (ядро бізнес-вимог)
    # =========================================================================

    def test_order_within_limit_is_confirmed(self):
        """Замовлення в межах ліміту підтверджується без перешкод."""
        order = self._create_order(amount=50000.0)
        self.assertFalse(order.credit_is_blocked,
                         'Замовлення на 50 000 при ліміті 100 000 '
                         'не має бути заблокованим')
        order.action_confirm()   # не має кинути помилку
        self.assertEqual(order.state, 'sale')

    def test_order_over_limit_is_blocked(self):
        """FR-C03, FR-C04: перевищення ліміту БЛОКУЄ підтвердження."""
        order = self._create_order(amount=150000.0)   # ліміт 100 000

        self.assertTrue(order.credit_is_blocked,
                        'Замовлення на 150 000 при ліміті 100 000 '
                        'має бути заблокованим')
        self.assertTrue(order.credit_block_reason,
                        'Має бути заповнена причина блокування')

        with self.assertRaises(UserError):
            order.action_confirm()

        # ⚠️ ПЕРЕВІРКА, ЩО ТРАНЗАКЦІЯ НЕ ЗМІНИЛА СТАН.
        # Це не формальність: якби ми кидали помилку ПІСЛЯ super(),
        # замовлення могло б лишитись у стані 'sale', а користувач
        # побачив би помилку. Тест ловить саме такий сценарій.
        self.assertEqual(order.state, 'draft',
                         'Після невдалого підтвердження замовлення '
                         'має лишитись чернеткою')

    def test_no_limit_means_order_always_allowed(self):
        """Дилер без ліміту може замовляти на будь-яку суму."""
        order = self._create_order(partner=self.dealer_no_limit,
                                   amount=9999999.0)
        self.assertFalse(order.credit_is_blocked)
        order.action_confirm()
        self.assertEqual(order.state, 'sale')

    def test_toggle_off_disables_blocking(self):
        """Якщо компанія вимкнула кредитні ліміти — ми поважаємо це.

        Це перевірка того, що наш модуль не «розумніший за бізнес»:
        налаштування Odoo має вищу силу, ніж наша логіка.
        """
        self.env.company.account_use_credit_limit = False
        order = self._create_order(amount=9999999.0)
        self.assertFalse(order.credit_is_blocked,
                         'При вимкненому перемикачі блокування не діє')
        order.action_confirm()
        self.assertEqual(order.state, 'sale')

    def test_blocking_applies_to_confirmed_orders_only_before(self):
        """Уже підтверджене замовлення не «розблоковується» заднім числом."""
        order = self._create_order(amount=50000.0)
        order.action_confirm()
        self.assertEqual(order.state, 'sale')
        # Після підтвердження перевірка не застосовується
        self.assertFalse(order.credit_is_blocked)

    def test_blocking_is_not_bypassable_via_mass_confirm(self):
        """Контроль стоїть у САМОМУ методі, а не в кнопці UI.

        Це найважливіший тест на архітектуру. Ми підтверджуємо замовлення
        так, як це робить МАСОВА операція зі списку (виклик методу на
        наборі записів), і перевіряємо, що блокування спрацювало.

        Якби ми додали перевірку лише в окрему кнопку, цей тест провалився б.
        """
        allowed = self._create_order(amount=1000.0)
        blocked = self._create_order(amount=150000.0)

        orders = allowed | blocked
        with self.assertRaises(UserError):
            orders.action_confirm()

        # Жодне із замовлень не має підтвердитись: ми свідомо не робимо
        # часткове підтвердження, щоб користувач не плутався.
        self.assertEqual(allowed.state, 'draft')
        self.assertEqual(blocked.state, 'draft')

    # =========================================================================
    #  ТЕСТИ: WORKFLOW ПОГОДЖЕННЯ
    # =========================================================================

    def test_approval_unblocks_confirmation(self):
        """FR-C05: схвалення контролера дозволяє підтвердити замовлення."""
        order = self._create_order(amount=150000.0)

        # Менеджер створює запит
        result = order.action_request_credit_approval()
        self.assertEqual(result['res_model'], 'techdistrib.credit.request')

        request = order.credit_request_ids
        self.assertEqual(len(request), 1)
        self.assertEqual(request.state, 'pending')

        # Доти замовлення все ще заблоковане
        with self.assertRaises(UserError):
            order.action_confirm()

        # Контролер схвалює
        request.with_user(self.user_controller).action_approve()
        self.assertEqual(request.state, 'approved')
        self.assertTrue(request.valid_until, 'Має бути встановлений строк дії')
        self.assertTrue(request.is_valid)

        # Тепер підтвердження проходить
        order.action_confirm()
        self.assertEqual(order.state, 'sale')

    def test_request_creation_is_blocked_when_already_pending(self):
        """Не можна створити два запити на одне замовлення."""
        order = self._create_order(amount=150000.0)
        order.action_request_credit_approval()

        with self.assertRaises(UserError):
            order.action_request_credit_approval()

    def test_approval_cannot_be_given_by_non_controller(self):
        """NFR-02: схвалювати може лише кредитний контролер.

        Це перевірка ДРУГОГО рубежа захисту — бізнес-перевірки в коді.
        Перший рубіж (ACL) дозволяє менеджеру писати в модель,
        бо він має створювати запити.
        """
        order = self._create_order(amount=150000.0)
        order.action_request_credit_approval()
        request = order.credit_request_ids

        with self.assertRaises(UserError):
            request.with_user(self.user_manager).action_approve()

        self.assertEqual(request.state, 'pending',
                         'Стан не має змінитись після невдалої спроби')

    def test_rejection_requires_reason(self):
        """FR-C05: відхилення без обґрунтування заборонене."""
        order = self._create_order(amount=150000.0)
        order.action_request_credit_approval()
        request = order.credit_request_ids

        controller_request = request.with_user(self.user_controller)

        # Без причини — помилка
        with self.assertRaises(UserError):
            controller_request.action_reject()

        # З причиною — працює
        controller_request.decision_note = 'Дилер має 3 прострочені рахунки.'
        controller_request.action_reject()

        self.assertEqual(request.state, 'rejected')

        # Відхилене замовлення лишається заблокованим
        with self.assertRaises(UserError):
            order.action_confirm()

    def test_expired_approval_blocks_again(self):
        """FR-C06: після закінчення строку схвалення блокування повертається."""
        order = self._create_order(amount=150000.0)
        order.action_request_credit_approval()
        request = order.credit_request_ids

        request.with_user(self.user_controller).action_approve()
        order.action_confirm()   # працює

        # --- Відтворюємо ситуацію «минув тиждень» ---------------------------
        # Для цього НЕ потрібно чекати: достатньо пересунути дату в минуле.
        # Це стандартний прийом тестування часової логіки.
        order2 = self._create_order(amount=150000.0)
        order2.action_request_credit_approval()
        request2 = order2.credit_request_ids
        request2.with_user(self.user_controller).action_approve()

        request2.valid_until = fields.Datetime.now() - relativedelta(days=1)

        # is_valid рахується на льоту — тому одразу False
        self.assertFalse(request2.is_valid,
                         'Прострочене схвалення не може бути дійсним')

        # І замовлення знову заблоковане, ХОЧА СТАН ЗАПИТУ ЩЕ 'approved' —
        # бо cron ще не встиг його перевести в 'expired'.
        # Це і є та самодостатність перевірки, про яку йде мова в коді.
        self.assertTrue(
            order2.credit_is_blocked,
            'Перевірка не має покладатись на cron: навіть зі станом '
            'approved, але простроченим valid_until, замовлення блокується',
        )
        with self.assertRaises(UserError):
            order2.action_confirm()

    def test_cron_expires_stale_approvals(self):
        """FR-C06: cron переводить прострочені схвалення у стан 'expired'."""
        order = self._create_order(amount=150000.0)
        order.action_request_credit_approval()
        request = order.credit_request_ids

        request.with_user(self.user_controller).action_approve()
        request.valid_until = fields.Datetime.now() - relativedelta(days=2)

        expired_count = self.env['techdistrib.credit.request']._cron_expire_credit_approvals()

        self.assertGreaterEqual(expired_count, 1)
        self.assertEqual(request.state, 'expired')

    def test_cron_keeps_valid_approvals(self):
        """Cron не чіпає схвалення, строк яких ще не минув."""
        order = self._create_order(amount=150000.0)
        order.action_request_credit_approval()
        request = order.credit_request_ids
        request.with_user(self.user_controller).action_approve()

        self.env['techdistrib.credit.request']._cron_expire_credit_approvals()

        self.assertEqual(request.state, 'approved')

    def test_approval_posts_message_on_order(self):
        """NFR-04: рішення фіксується в історії замовлення."""
        order = self._create_order(amount=150000.0)
        order.action_request_credit_approval()
        request = order.credit_request_ids

        before = len(order.message_ids)
        request.with_user(self.user_controller).action_approve()

        self.assertGreater(len(order.message_ids), before,
                           'Має зʼявитись повідомлення в chatter замовлення')

    # =========================================================================
    #  ТЕСТИ: ПРОСТРОЧЕНА ДЕБІТОРКА (FR-C08)
    # =========================================================================

    def test_overdue_invoice_blocks_partner(self):
        """FR-C08: прострочений понад 30 днів рахунок блокує дилера."""
        dealer = self.env['res.partner'].create({
            'name': 'ТОВ Боржник',
            'is_dealer': True,
            'credit_limit': 1000000.0,   # ліміт великий — блокує саме борг
        })

        # Спочатку боргу немає
        self.assertFalse(dealer.credit_is_blocked)
        self.assertAlmostEqual(dealer.credit_overdue_amount, 0.0, places=2)

        invoice = self._create_overdue_invoice(dealer, amount=5000.0, days_overdue=45)

        # ⚠️ ЗНОВУ УРОК ПРО ПОДАТКИ (той самий, що в Модулі 1).
        # Спочатку я написав assertAlmostEqual(..., 5000.0) і тест упав:
        #     AssertionError: 5750.0 != 5000.0
        # Бо 5000 × 1.15 (ПДВ) = 5750 — борг клієнта включає податок.
        #
        # Тому очікуване значення беремо З САМОГО РАХУНКУ. Це робить тест
        # незалежним від налаштувань податків і перевіряє саме логіку.
        expected = invoice.amount_residual
        self.assertAlmostEqual(dealer.credit_overdue_amount, expected, places=2)
        self.assertTrue(dealer.credit_is_blocked)
        self.assertTrue(dealer.credit_oldest_overdue_date)

        # І замовлення блокується навіть у межах ліміту
        order = self._create_order(partner=dealer, amount=1000.0)
        self.assertTrue(order.credit_is_blocked)
        with self.assertRaises(UserError):
            order.action_confirm()

    def test_recent_due_invoice_does_not_block(self):
        """Рахунок, термін оплати якого ще не минув, НЕ блокує дилера.

        Без цього тесту легко зробити помилку «блокувати всіх, у кого є борг»,
        що паралізувало б нормальну роботу: адже борг є майже завжди.
        """
        dealer = self.env['res.partner'].create({
            'name': 'ТОВ Сумлінний',
            'is_dealer': True,
            'credit_limit': 1000000.0,
        })
        # Рахунок прострочений лише 5 днів — це в межах норми
        self._create_overdue_invoice(dealer, amount=5000.0, days_overdue=5)

        self.assertAlmostEqual(dealer.credit_overdue_amount, 0.0, places=2)
        self.assertFalse(dealer.credit_is_blocked)

    def test_overdue_counts_toward_credit_used(self):
        """Прострочений рахунок входить у «використано ліміту»."""
        limit = 100000.0
        dealer = self.env['res.partner'].create({
            'name': 'ТОВ Рахунок-перевірка',
            'is_dealer': True,
            'credit_limit': limit,
        })
        invoice = self._create_overdue_invoice(dealer, amount=25000.0, days_overdue=40)

        # Очікувані суми беремо з рахунку (борг включає ПДВ — див. коментар
        # у тесті вище).
        debt = invoice.amount_residual

        # credit — це дебіторка з бухгалтерських проводок
        self.assertAlmostEqual(dealer.credit_used, debt, places=2)
        self.assertAlmostEqual(dealer.credit_available, limit - debt, places=2)
        self.assertAlmostEqual(
            dealer.credit_usage_percent, debt / limit * 100.0, places=2,
        )

    # =========================================================================
    #  ТЕСТИ: МЕТОДИ ПОШУКУ ДЛЯ НЕЗБЕРЕЖЕНИХ ПОЛІВ
    # =========================================================================
    #  Ці тести захищають від НАЙПІДСТУПНІШОГО баг:
    #  Odoo мовчки ігнорує фільтр по незбереженому полі без методу search
    #  (osv/expression.py:1179). Без цих тестів фільтр «Заблоковані за борг»
    #  показував би ВСІХ партнерів, і ніхто б не помітив.
    # =========================================================================

    def test_search_blocked_partners_returns_only_blocked(self):
        """Фільтр по credit_is_blocked працює, а не ігнорується."""
        blocked = self.env['res.partner'].create({
            'name': 'ТОВ Заблокований',
            'is_dealer': True,
        })
        clean = self.env['res.partner'].create({
            'name': 'ТОВ Чистий',
            'is_dealer': True,
        })
        self._create_overdue_invoice(blocked, amount=1000.0, days_overdue=60)

        found = self.env['res.partner'].search([
            ('credit_is_blocked', '=', True),
            ('id', 'in', (blocked | clean).ids),
        ])

        self.assertIn(blocked, found)
        self.assertNotIn(clean, found,
                         'Чистий партнер НЕ має потрапляти у фільтр '
                         '(якщо цей тест упав — ймовірно, поле втратило search=)')

    def test_search_not_blocked_partners(self):
        """Зворотний фільтр теж працює."""
        blocked = self.env['res.partner'].create({
            'name': 'ТОВ Заблокований 2',
            'is_dealer': True,
        })
        self._create_overdue_invoice(blocked, amount=1000.0, days_overdue=60)

        found = self.env['res.partner'].search([
            ('credit_is_blocked', '=', False),
            ('id', '=', blocked.id),
        ])
        self.assertFalse(found)

    def test_search_partners_with_pending_requests(self):
        """Фільтр по кількості запитів на погодженні працює."""
        order = self._create_order(amount=150000.0)
        order.action_request_credit_approval()

        found = self.env['res.partner'].search([
            ('credit_pending_request_count', '>', 0),
            ('id', '=', self.dealer.id),
        ])
        self.assertTrue(found, 'Дилер має знайтись: у нього є запит на погодженні')
