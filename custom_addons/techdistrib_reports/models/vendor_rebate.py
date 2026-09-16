# -*- coding: utf-8 -*-
# =============================================================================
#  Модель: techdistrib.vendor.rebate — ретро-бонус вендора
# =============================================================================
#  FR-F04, FR-F05: «ретро-бонус вендора — % від обороту за квартал,
#  розрахунок запускається автоматично після закриття кварталу».
#
#  Це третій приклад нової бізнес-сутності в проєкті (після кредитного
#  запиту й гарантійного звернення), і він демонструє ІНШИЙ тип роботи
#  з даними: агрегацію ЧУЖИХ даних за період.
#
#  Тут немає власного «життєвого циклу операції» — є документ-нарахування,
#  який фіксує підсумок за минулий період.
# =============================================================================

from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class TechdistribVendorRebate(models.Model):
    _name = 'techdistrib.vendor.rebate'
    _description = 'Ретро-бонус вендора'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date_from desc, vendor_id'

    # =========================================================================
    #  ІДЕНТИФІКАЦІЯ
    # =========================================================================

    name = fields.Char(
        string='Номер',
        required=True,
        readonly=True,
        copy=False,
        default=lambda self: _('Нове'),
        index=True,
    )

    vendor_id = fields.Many2one(
        comodel_name='res.partner',
        string='Вендор',
        required=True,
        ondelete='restrict',
        tracking=True,
        index=True,
        # domain — показуємо лише постачальників. Це той самий принцип, що
        # в кредитних запитах: не давай створити документ для сутності,
        # з якою він не має сенсу.
        domain="[('supplier_rank', '>', 0)]",
    )

    company_id = fields.Many2one(
        comodel_name='res.company',
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )

    currency_id = fields.Many2one(
        comodel_name='res.currency',
        related='company_id.currency_id',
        store=True,
        readonly=True,
    )

    # =========================================================================
    #  ПЕРІОД
    # =========================================================================

    date_from = fields.Date(string='Період з', required=True, tracking=True)
    date_to = fields.Date(string='Період по', required=True, tracking=True)

    period_label = fields.Char(
        string='Період',
        compute='_compute_period_label',
        store=True,
    )

    # =========================================================================
    #  РОЗРАХУНОК
    # =========================================================================

    rebate_percent = fields.Float(
        string='Відсоток бонусу',
        digits=(16, 2),
        required=True,
        tracking=True,
        help='Типово підставляється з картки вендора, але можна змінити '
             'для конкретного періоду (напр., за домовленістю сторін).',
    )

    # ⚠️ АРХІТЕКТУРНЕ РІШЕННЯ: суми ЗБЕРІГАЮТЬСЯ, хоч і залежать від
    # даних іншої моделі (purchase.order).
    #
    # Причина — та сама, що в Модулі 2: нарахування за закритий квартал
    # має лишитись незмінним документом. Якщо через рік хтось виправить
    # суму в закупівлі минулого кварталу, бонус уже виплачено, і
    # перераховувати його не можна.
    #
    # Наслідок: значення НЕ оновиться саме собою при нових закупівлях
    # у тому ж періоді. Для цього є кнопка «Перерахувати» і cron, який
    # перераховує нарахування, поки вони в стані чернетки.
    purchase_amount = fields.Monetary(
        string='Оборот закупівель',
        currency_field='currency_id',
        compute='_compute_rebate_amount',
        store=True,
        help='Сума підтверджених закупівель у вендора за період (без ПДВ).',
    )

    rebate_amount = fields.Monetary(
        string='Сума бонусу',
        currency_field='currency_id',
        compute='_compute_rebate_amount',
        store=True,
    )

    purchase_order_count = fields.Integer(
        string='Замовлень постачальнику',
        # ОКРЕМИЙ обчислювач — див. пояснення в _compute_rebate_amount
        compute='_compute_purchase_order_count',
    )

    # =========================================================================
    #  СТАН
    # =========================================================================

    state = fields.Selection(
        selection=[
            ('draft', 'Чернетка'),
            ('confirmed', 'Підтверджено'),
            ('invoiced', 'Виставлено вендору'),
            ('received', 'Отримано'),
            ('cancelled', 'Скасовано'),
        ],
        string='Стан',
        default='draft',
        required=True,
        tracking=True,
        index=True,
        group_expand='_group_expand_states',
    )

    note = fields.Text(string='Примітки')

    # =========================================================================
    #  ОБМЕЖЕННЯ
    # =========================================================================

    _sql_constraints = [
        # Одне нарахування на вендора за період в одній компанії.
        # Без цього cron створював би дублі при кожному запуску.
        ('vendor_period_uniq', 'unique(vendor_id, date_from, date_to, company_id)',
         'Для цього вендора вже є нарахування бонусу за цей період!'),
    ]

    @api.constrains('date_from', 'date_to')
    def _check_period(self):
        for rebate in self:
            if rebate.date_from and rebate.date_to and rebate.date_from > rebate.date_to:
                raise ValidationError(_(
                    'Дата початку періоду не може бути пізніше дати закінчення.',
                ))

    # =========================================================================
    #  ОБЧИСЛЕННЯ
    # =========================================================================

    @api.depends('date_from', 'date_to')
    def _compute_period_label(self):
        for rebate in self:
            if rebate.date_from and rebate.date_to:
                rebate.period_label = '%s — %s' % (
                    rebate.date_from.strftime('%d.%m.%Y'),
                    rebate.date_to.strftime('%d.%m.%Y'),
                )
            else:
                rebate.period_label = _('Період не задано')

    # =========================================================================
    #  ⚠️ ЧЕТВЕРТИЙ РАЗ ТА САМА ГРАБЛИНА — і це найкращий доказ, наскільки
    #  вона підступна.
    #
    #  У цьому модулі я знову написав ОДИН обчислювач, який присвоював і
    #  збережені поля (purchase_amount, rebate_amount зі store=True), і
    #  незбережене (purchase_order_count). Odoo попередив:
    #
    #      UserWarning: techdistrib.vendor.rebate: inconsistent 'store' for
    #      computed fields, accessing purchase_order_count may recompute and
    #      update purchase_amount, rebate_amount.
    #
    #  Раніше ця сама помилка була:
    #      1) у techdistrib_base  — метрики дилера;
    #      2) у techdistrib_warranty — перевірка гарантії;
    #      3) тут, у techdistrib_reports.
    #
    #  ЧОМУ ЦЕ ЛЕГКО ЗРОБИТИ: обчислювач логічно «про одне» — «порахувати
    #  підсумки за період». Природно покласти в нього всі підсумки.
    #  А про те, що в частини полів store=True, а в частини ні, думаєш
    #  аж коли Odoo попередить.
    #
    #  ЧОМУ ЦЕ НЕБЕЗПЕЧНО: незбережене поле рахується при КОЖНОМУ читанні,
    #  а метод заразом перезаписує збережені. Виникають несподівані UPDATE
    #  у базі під час звичайного перегляду списку.
    #
    #  ПРАВИЛО, ЯКЕ ТРЕБА ЗАПАМ'ЯТАТИ:
    #      ОДИН МЕТОД-ОБЧИСЛЮВАЧ = ОДИН РЕЖИМ ЗБЕРЕЖЕННЯ.
    #  Спільну логіку винось у звичайний метод (див. _get_period_purchase_orders)
    #  і клич його з двох тонких обчислювачів.
    # =========================================================================

    @api.depends('vendor_id', 'date_from', 'date_to', 'company_id', 'rebate_percent')
    def _compute_rebate_amount(self):
        """ЗБЕРЕЖЕНІ суми: оборот за період і сума бонусу.

        ⚠️ ЗАЛЕЖНОСТІ ТУТ НЕПОВНІ, І ЦЕ УСВІДОМЛЕНО.
        Ми не можемо вказати @api.depends на поля purchase.order, бо між
        нашими моделями немає прямого зв'язку (немає Many2one, по якому
        Odoo міг би побудувати граф залежностей).

        Тому поле перерахується:
          * при створенні й при зміні періоду/вендора/відсотка;
          * при натисканні «Перерахувати»;
          * cron-задачею для нарахувань у стані чернетки.

        Це типовий компроміс при агрегації чужих даних. Альтернатива —
        зробити поле незбереженим і рахувати при кожному читанні, але
        тоді по ньому не можна ні фільтрувати, ні групувати у звітах
        (а нам це потрібно — див. pivot-подання).
        """
        for rebate in self:
            if not (rebate.vendor_id and rebate.date_from and rebate.date_to):
                rebate.purchase_amount = 0.0
                rebate.rebate_amount = 0.0
                continue

            orders = rebate._get_period_purchase_orders()
            # amount_untaxed — сума БЕЗ ПДВ. Для розрахунку бонусу це
            # правильна база: ПДВ не є ні нашими витратами, ні оборотом,
            # від якого вендор платить бонус.
            # (Той самий урок, що в Модулі 1 з оборотом дилера.)
            rebate.purchase_amount = sum(orders.mapped('amount_untaxed'))
            rebate.rebate_amount = (
                rebate.purchase_amount * rebate.rebate_percent / 100.0
            )

    @api.depends('vendor_id', 'date_from', 'date_to', 'company_id')
    def _compute_purchase_order_count(self):
        """НЕзбережений лічильник замовлень.

        Свідомо не зберігаємо: по ньому ніхто не шукає й не групує,
        він лише показується у формі як довідкова цифра.
        """
        for rebate in self:
            if not (rebate.vendor_id and rebate.date_from and rebate.date_to):
                rebate.purchase_order_count = 0
                continue
            rebate.purchase_order_count = len(rebate._get_period_purchase_orders())

    def _get_period_purchase_orders(self):
        """Підтверджені замовлення постачальнику за період."""
        self.ensure_one()
        return self.env['purchase.order'].search([
            ('partner_id', '=', self.vendor_id.id),
            ('company_id', '=', self.company_id.id),
            # 'purchase' — підтверджено; 'done' — замовлення закрито.
            # Чернетки та скасовані не враховуємо.
            ('state', 'in', ('purchase', 'done')),
            ('date_order', '>=', self.date_from),
            ('date_order', '<=', self.date_to),
        ])

    @api.model
    def _group_expand_states(self, states, domain):
        return [key for key, _label in self._fields['state'].selection]

    # =========================================================================
    #  СТВОРЕННЯ
    # =========================================================================

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('Нове')) == _('Нове'):
                vals['name'] = (
                    self.env['ir.sequence'].next_by_code('techdistrib.vendor.rebate')
                    or _('Нове')
                )
            # Підставляємо відсоток із картки вендора, якщо не заданий вручну
            if not vals.get('rebate_percent') and vals.get('vendor_id'):
                vendor = self.env['res.partner'].browse(vals['vendor_id'])
                vals['rebate_percent'] = vendor.vendor_rebate_percent
        return super().create(vals_list)

    # =========================================================================
    #  ДІЇ
    # =========================================================================

    def action_recalculate(self):
        """Примусово перерахувати суми.

        Це та сама «рятівна кнопка», що в матриці знижок: коли значення
        залежить від даних, які Odoo не бачить через граф залежностей,
        людині потрібен спосіб сказати «порахуй зараз».
        """
        self.env.add_to_compute(self._fields['purchase_amount'], self)
        return True

    def action_confirm(self):
        """Підтвердити нарахування.

        ⚠️ Після підтвердження документ стає НЕЗМІННИМ. Це принципово:
        бонус — це грошове зобов'язання, і воно не має «пливти» заднім
        числом. Саме тому ми й зберігаємо суми (store=True).
        """
        for rebate in self:
            if rebate.state != 'draft':
                raise UserError(_('Підтвердити можна лише нарахування в чернетці.'))
            if not rebate.purchase_amount:
                raise UserError(_(
                    'Нарахування %(name)s має нульовий оборот за період.\n\n'
                    'Перевірте, чи є в цьому періоді підтверджені '
                    'замовлення постачальнику для вендора «%(vendor)s».',
                    name=rebate.name, vendor=rebate.vendor_id.display_name,
                ))
        self.write({'state': 'confirmed'})
        return True

    def action_mark_invoiced(self):
        self.write({'state': 'invoiced'})
        return True

    def action_mark_received(self):
        self.write({'state': 'received'})
        return True

    def action_cancel(self):
        self.write({'state': 'cancelled'})
        return True

    def action_reset_to_draft(self):
        self.write({'state': 'draft'})
        return True

    # =========================================================================
    #  АВТОМАТИЗАЦІЯ (FR-F05)
    # =========================================================================

    @api.model
    def _get_previous_quarter_dates(self, reference_date):
        """Повернути (перший день, останній день) ПОПЕРЕДНЬОГО кварталу.

        Розбираємо алгоритм, бо з датами легко помилитись на одиницю:

          reference_date = 2026-05-15
          1. квартал поточного місяця: ((5 - 1) // 3) * 3 + 1 = 4 → квітень
          2. початок поточного кварталу: 2026-04-01
          3. кінець попереднього: 2026-04-01 - 1 день = 2026-03-31
          4. початок попереднього: 2026-03-01 - 2 місяці = 2026-01-01
          → Q1 2026: 01.01 — 31.03   ✓
        """
        quarter_start_month = ((reference_date.month - 1) // 3) * 3 + 1
        current_quarter_start = date(reference_date.year, quarter_start_month, 1)

        previous_quarter_end = current_quarter_start - timedelta(days=1)
        previous_quarter_start = (
            previous_quarter_end.replace(day=1) - relativedelta(months=2)
        )
        return previous_quarter_start, previous_quarter_end

    @api.model
    def _cron_generate_quarterly_rebates(self):
        """FR-F05: створити нарахування бонусів після закриття кварталу.

        Задача ідемпотентна — це головна вимога до cron:
          * SQL-обмеження unique(vendor, period, company) не дасть
            створити дубль;
          * плюс ми ЯВНО перевіряємо наявність перед створенням, щоб
            не ловити помилку цілісності й не засмічувати логи.

        Чому обидва захисти: обмеження в базі — «останній рубіж», він
        спрацює навіть при одночасному запуску двох воркерів. Явна
        перевірка — «перший рубіж», вона дає чисту роботу без помилок.
        """
        today = fields.Date.context_today(self)
        date_from, date_to = self._get_previous_quarter_dates(today)

        # Вендори, з якими є домовленість про бонус
        vendors = self.env['res.partner'].search([
            ('vendor_rebate_percent', '>', 0),
            ('supplier_rank', '>', 0),
        ])

        created = 0
        for vendor in vendors:
            existing = self.search([
                ('vendor_id', '=', vendor.id),
                ('date_from', '=', date_from),
                ('date_to', '=', date_to),
                ('company_id', '=', self.env.company.id),
            ], limit=1)
            if existing:
                # Перераховуємо лише чернетки: підтверджені документи
                # змінювати не можна (див. action_confirm).
                if existing.state == 'draft':
                    existing.action_recalculate()
                continue

            self.create({
                'vendor_id': vendor.id,
                'company_id': self.env.company.id,
                'date_from': date_from,
                'date_to': date_to,
                'rebate_percent': vendor.vendor_rebate_percent,
            })
            created += 1

        return created
