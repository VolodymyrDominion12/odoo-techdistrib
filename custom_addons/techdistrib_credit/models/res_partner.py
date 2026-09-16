# -*- coding: utf-8 -*-
# =============================================================================
#  Розширення res.partner: показники кредитного навантаження
# =============================================================================
#  Тут ми працюємо з ДВОМА джерелами даних:
#
#   1. СТАНДАРТНІ поля, які ми ПЕРЕВИКОРИСТОВУЄМО (не дублюємо!):
#        credit_limit       — ліміт (company_dependent!)
#        credit             — дебіторка: неоплачені рахунки
#        credit_to_invoice  — підтверджені замовлення, ще не виставлені
#
#   2. НАШІ похідні показники, яких у Odoo немає:
#        credit_used              — разом борг
#        credit_available         — вільний залишок ліміту
#        credit_overdue_amount    — прострочена понад 30 днів
#        credit_is_blocked        — чи заблокований партнер
#
#  ⚠️ НАЙВАЖЛИВІША ПАСТКА ЦЬОГО ФАЙЛУ: групи доступу на полях.
#
#  Подивись, як оголошені стандартні поля (addons/account/models/partner.py):
#
#      credit = fields.Monetary(..., groups='account.group_account_invoice,...')
#      credit_limit = fields.Float(..., groups='account.group_account_invoice,...')
#
#  Атрибут groups= робить поле НЕДОСТУПНИМ для читання користувачам без
#  цих груп — і це не лише приховування в UI, а справжня перевірка в ORM.
#
#  Наслідок: якщо звичайний менеджер із продажу відкриє замовлення, наша
#  перевірка ліміту СПРОБУЄ прочитати credit_limit і отримає AccessError.
#  Гірше: якби ми це «полагодили» через try/except, менеджер без груп
#  обходив би кредитний ліміт — бо перевірка не змогла б його прочитати.
#
#  Правильне рішення — те саме, що використовує сам Odoo в
#  sale/models/sale_order.py:
#      order.sudo()  # ensure access to `credit` & `credit_limit` fields
#
#  Тобто обчислення виконується від імені суперкористувача. Це БЕЗПЕЧНО,
#  бо ми лише ЧИТАЄМО для перевірки, а не показуємо зайвого:
#  право бачити ліміт регулюється окремо (див. views/res_partner_views.xml).
# =============================================================================

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

# Скільки днів прострочення вважається критичним (FR-C08).
# Виносимо в константу — бізнес може попросити змінити на 45 днів.
CREDIT_OVERDUE_DAYS = 30


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # =========================================================================
    #  ПОХІДНІ ПОКАЗНИКИ
    # =========================================================================

    credit_used = fields.Monetary(
        string='Використано ліміту',
        compute='_compute_credit_usage',
        currency_field='currency_id',
        # compute_sudo=True — обчислення виконується від імені суперкористувача.
        # Без цього менеджер без бухгалтерських груп отримав би AccessError,
        # бо ми читаємо credit і credit_to_invoice (див. коментар угорі файлу).
        compute_sudo=True,
        help='Дебіторка + підтверджені замовлення, ще не виставлені в рахунок.',
    )

    credit_available = fields.Monetary(
        string='Доступний залишок',
        compute='_compute_credit_usage',
        currency_field='currency_id',
        compute_sudo=True,
        help='Ліміт мінус використано. Може бути відʼємним, якщо ліміт зменшили.',
    )

    credit_usage_percent = fields.Float(
        string='Використання ліміту, %',
        compute='_compute_credit_usage',
        compute_sudo=True,
        # aggregator=None вимикає підсумовування при групуванні в списку.
        # Для відсотків сума не має сенсу — тому вимикаємо явно.
        aggregator=None,
    )

    credit_has_limit = fields.Boolean(
        string='Має ліміт',
        compute='_compute_credit_usage',
        compute_sudo=True,
        help='Ліміт 0 у Odoo означає «без ліміту». Це поле зручне для фільтрів.',
    )

    credit_overdue_amount = fields.Monetary(
        string='Прострочено понад 30 днів',
        compute='_compute_credit_overdue',
        currency_field='currency_id',
        compute_sudo=True,
    )

    credit_oldest_overdue_date = fields.Date(
        string='Найстаріший прострочений рахунок',
        compute='_compute_credit_overdue',
        compute_sudo=True,
    )

    credit_is_blocked = fields.Boolean(
        string='Заблокований за борг',
        compute='_compute_credit_overdue',
        compute_sudo=True,
        # ⚠️ search= — НАЙВАЖЛИВІШИЙ атрибут цього поля. Пояснюю детально.
        #
        # Питання: що станеться, якщо написати фільтр
        #     [('credit_is_blocked', '=', True)]
        # для поля, яке НЕ зберігається в базі?
        #
        # Інтуїтивна відповідь: «буде помилка». ПРАВИЛЬНА ВІДПОВІДЬ: НІ.
        # Ось що робить Odoo (vendor/odoo/odoo/osv/expression.py, рядок 1179):
        #
        #     elif not field.store:
        #         # Non-stored field should provide an implementation of search.
        #         if not field.search:
        #             # field does not support search!
        #             _logger.error("Non-stored field %s cannot be searched.", ...)
        #             # Ignore it: generate a dummy leaf.
        #             domain = []
        #
        # Тобто Odoo пише помилку В ЛОГ і ПОВЕРТАЄ ПОРОЖНІЙ ДОМЕН —
        # тобто ІГНОРУЄ умову фільтра. Фільтр «показати заблокованих»
        # тихо показав би ВСІХ партнерів.
        #
        # Це набагато гірше за явну помилку: користувач бачить неправильні
        # дані й не здогадується про проблему, а в логах ніхто не читає ERROR.
        #
        # Тому для КОЖНОГО незбереженого поля, по якому ти хочеш фільтрувати
        # (у search view, у domain дії, у Python), треба реалізувати
        # метод пошуку й вказати його в атрибуті search=.
        search='_search_credit_is_blocked',
        help='True, якщо є рахунки, прострочені понад 30 днів. '
             'Таким партнерам підтвердження замовлень заборонено.',
    )

    # =========================================================================
    #  ЗАПИТИ НА КРЕДИТ
    # =========================================================================

    credit_request_ids = fields.One2many(
        comodel_name='techdistrib.credit.request',
        inverse_name='partner_id',
        string='Запити на кредит',
    )

    credit_request_count = fields.Integer(
        string='Запитів на кредит',
        compute='_compute_credit_request_count',
    )

    credit_pending_request_count = fields.Integer(
        string='Запитів на погодженні',
        compute='_compute_credit_request_count',
        # Теж незбережене поле — тому теж потребує методу пошуку,
        # інакше фільтр по ньому мовчки ігнорувався б.
        search='_search_credit_pending_request_count',
    )

    # =========================================================================
    #  ОБЧИСЛЕННЯ
    # =========================================================================

    @api.depends('credit', 'credit_to_invoice', 'credit_limit', 'currency_id')
    def _compute_credit_usage(self):
        """Використання кредитного ліміту.

        УВАГА: залежності 'credit', 'credit_to_invoice', 'credit_limit' —
        це поля З ІНШОГО МОДУЛЯ (account). Odoo дозволяє залежати від них,
        і коли вони перерахуються, перерахується й наше поле.
        """
        for partner in self:
            # .sudo() тут не потрібен: метод уже виконується як sudo
            # завдяки compute_sudo=True. Але ми все одно читаємо через
            # partner (не partner.sudo()) — так код читається природніше.
            used = partner.credit + partner.credit_to_invoice
            partner.credit_used = used

            limit = partner.credit_limit or 0.0
            partner.credit_has_limit = bool(limit)
            partner.credit_available = limit - used if limit else 0.0
            partner.credit_usage_percent = (used / limit * 100.0) if limit else 0.0

    @api.depends(
        'invoice_ids',
        'invoice_ids.state',
        'invoice_ids.move_type',
        'invoice_ids.payment_state',
        'invoice_ids.amount_residual',
        'invoice_ids.invoice_date_due',
        'commercial_partner_id',
    )
    def _compute_credit_overdue(self):
        """Прострочена дебіторка понад CREDIT_OVERDUE_DAYS днів (FR-C08).

        ЧОМУ ТУТ ПОШУК, А НЕ filtered() ПО invoice_ids:
        у дилера можуть бути дочірні контакти (філії, відділи закупівель),
        і рахунки виставляються на них. Щоб показник був однаковий і для
        головної компанії, і для її контактів, ми збираємо рахунки всієї
        юридичної особи через 'child_of'.

        ⚠️ І ЗНОВУ ПРО ГРУПИ ДОСТУПУ: поля amount_residual і payment_state
        на account.move теж можуть бути обмежені групами. Тому весь метод
        виконується під sudo (compute_sudo=True), а пошук — від імені
        комерційного партнера.
        """
        today = fields.Date.context_today(self)
        threshold = today - relativedelta(days=CREDIT_OVERDUE_DAYS)

        for partner in self:
            commercial = partner.commercial_partner_id
            if not commercial:
                partner.credit_overdue_amount = 0.0
                partner.credit_oldest_overdue_date = False
                partner.credit_is_blocked = False
                continue

            overdue_invoices = self.env['account.move'].search([
                ('partner_id', 'child_of', commercial.id),
                # Тільки виставлені клієнтські рахунки. 'out_invoice' —
                # це «customer invoice». Є ще 'out_refund' (кредит-нота),
                # 'in_invoice' (рахунок постачальника) тощо.
                ('move_type', '=', 'out_invoice'),
                # 'posted' = проведений. Чернетки не є боргом.
                ('state', '=', 'posted'),
                # Не оплачені й не сторновані
                ('payment_state', 'not in', ('paid', 'reversed')),
                ('invoice_date_due', '!=', False),
                ('invoice_date_due', '<', threshold),
                # amount_residual > 0 — захист від дивних даних,
                # коли рахунок формально «не оплачений», але залишок нульовий
                ('amount_residual', '>', 0),
            ])

            partner.credit_overdue_amount = sum(overdue_invoices.mapped('amount_residual'))

            dates = [d for d in overdue_invoices.mapped('invoice_date_due') if d]
            partner.credit_oldest_overdue_date = min(dates) if dates else False

            # Блокуємо, якщо є ХОЧ ЩОСЬ прострочене понад поріг.
            partner.credit_is_blocked = bool(overdue_invoices)

    @api.depends('credit_request_ids', 'credit_request_ids.state')
    def _compute_credit_request_count(self):
        for partner in self:
            requests = partner.credit_request_ids
            partner.credit_request_count = len(requests)
            partner.credit_pending_request_count = len(
                requests.filtered(lambda r: r.state == 'pending')
            )

    # =========================================================================
    #  МЕТОДИ ПОШУКУ для незбережених полів
    # =========================================================================
    #  Сигнатура методу пошуку завжди однакова:
    #      def _search_<назва_поля>(self, operator, value) -> domain
    #
    #  Він отримує ОПЕРАТОР і ЗНАЧЕННЯ з фільтра користувача
    #  (напр., '=', True) і має повернути ЗВИЧАЙНИЙ домен Odoo,
    #  який виражає ту саму умову через збережені поля.
    #
    #  Ми не рахуємо нічого самі: ми перекладаємо умову «заблокований»
    #  у домен на РАХУНКИ, бо саме вони — джерело істини.
    # =========================================================================

    @api.model
    def _search_credit_is_blocked(self, operator, value):
        """Перекласти 'credit_is_blocked = True' у домен по рахунках."""
        if operator not in ('=', '!='):
            raise UserError(_(
                'Поле «Заблокований за борг» підтримує лише оператори '
                '«дорівнює» та «не дорівнює».'
            ))

        # Перетворюємо (operator, value) у просте булеве «шукаємо заблокованих?»
        # Це стандартна ідіома для булевих полів:
        #   = True  → шукаємо тих, у кого True
        #   != False → теж шукаємо тих, у кого True
        want_blocked = (operator == '=' and value) or (operator == '!=' and not value)

        today = fields.Date.context_today(self)
        threshold = today - relativedelta(days=CREDIT_OVERDUE_DAYS)

        overdue_invoices = self.env['account.move'].search([
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('payment_state', 'not in', ('paid', 'reversed')),
            ('invoice_date_due', '!=', False),
            ('invoice_date_due', '<', threshold),
            ('amount_residual', '>', 0),
        ])

        blocked_commercial = overdue_invoices.mapped('commercial_partner_id')
        if not blocked_commercial:
            # Нікого не заблоковано. Для «шукаємо заблокованих» повертаємо
            # домен, що не знайде нічого; для «не заблокованих» — порожній
            # домен (тобто «усі»).
            return [('id', 'in', [])] if want_blocked else []

        # ⚠️ ВАЖЛИВО: блокуються не лише самі юридичні особи, а й їхні
        # дочірні контакти. Бо в _compute_credit_overdue ми рахуємо
        # прострочку по commercial_partner_id — отже й фільтр має
        # поводитись узгоджено, інакше список показуватиме не те,
        # що показує картка.
        #
        # active_test=False — щоб знайти й архівованих партнерів:
        # борг нікуди не зникає від того, що картку заархівували.
        blocked_partners = self.with_context(active_test=False).search([
            ('commercial_partner_id', 'in', blocked_commercial.ids),
        ]) | blocked_commercial

        if want_blocked:
            return [('id', 'in', blocked_partners.ids)]
        return [('id', 'not in', blocked_partners.ids)]

    @api.model
    def _search_credit_pending_request_count(self, operator, value):
        """Пошук по кількості активних кредитних запитів.

        Реалізуємо лише ті випадки, які реально потрібні в інтерфейсі:
        «> 0» і «= 0». Решта — прозора помилка замість тихої неправди.
        """
        if (operator, value) in (('>', 0), ('>=', 1)):
            return [('credit_request_ids.state', '=', 'pending')]
        if (operator, value) in (('=', 0), ('<=', 0)):
            return [('credit_request_ids.state', '!=', 'pending')]
        raise UserError(_(
            'Пошук по кількості кредитних запитів підтримує лише '
            'порівняння з нулем.'
        ))

    # =========================================================================
    #  ДІЇ
    # =========================================================================

    def action_view_credit_requests(self):
        """Smart-кнопка «Запити на кредит»."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Запити на кредит: %s', self.display_name),
            'res_model': 'techdistrib.credit.request',
            'view_mode': 'list,form',
            'domain': [('partner_id', 'child_of', self.commercial_partner_id.id)],
            'context': {
                'default_partner_id': self.id,
                'search_default_filter_pending': 1 if self.credit_pending_request_count else 0,
            },
        }

    def action_view_open_invoices(self):
        """Smart-кнопка «Відкриті рахунки» — щоб контролер бачив, звідки борг."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Відкриті рахунки: %s', self.display_name),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [
                ('partner_id', 'child_of', self.commercial_partner_id.id),
                ('move_type', '=', 'out_invoice'),
                ('state', '=', 'posted'),
                ('payment_state', 'not in', ('paid', 'reversed')),
            ],
            'context': {'default_move_type': 'out_invoice'},
        }
