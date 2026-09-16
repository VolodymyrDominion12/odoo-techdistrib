# -*- coding: utf-8 -*-
# =============================================================================
#  Модель: techdistrib.warranty.claim — гарантійне звернення (RMA)
# =============================================================================
#  Друга (після кредитних лімітів) велика кастомна бізнес-сутність проєкту.
#
#  КЛЮЧОВА ІДЕЯ: звернення прив'язане до СЕРІЙНОГО НОМЕРА, а не до назви
#  товару. Саме це дає «ланцюжок доказів» для вендора:
#
#      номер → коли ми його відвантажили → за яким замовленням →
#      кому → чи діє гарантія
#
#  Без цього ланцюжка вендор відмовляє в компенсації, і збиток дорівнює
#  повній вартості обладнання. Це була проблема P3 у бізнес-вимогах.
# =============================================================================

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

# Скільки днів звернення може висіти «в роботі», перш ніж це стане
# проблемою і потребує нагадування (FR-E07).
CLAIM_DEADLINE_DAYS = 30


class TechdistribWarrantyClaim(models.Model):
    _name = 'techdistrib.warranty.claim'
    _description = 'Гарантійне звернення (RMA)'

    # mail.thread        — chatter з історією
    # mail.activity.mixin — «дії» з дедлайном (використовуємо для нагадувань)
    _inherit = ['mail.thread', 'mail.activity.mixin']

    _order = 'create_date desc, id desc'

    # =========================================================================
    #  ІДЕНТИФІКАЦІЯ
    # =========================================================================

    name = fields.Char(
        string='Номер RMA',
        required=True,
        readonly=True,
        copy=False,
        default=lambda self: _('Нове'),
        index=True,
    )

    # =========================================================================
    #  ОБЛАДНАННЯ — ЯДРО МОДЕЛІ
    # =========================================================================

    lot_id = fields.Many2one(
        comodel_name='stock.lot',
        string='Серійний номер',
        required=True,
        # ondelete='restrict': не можна видалити серійний номер, якщо по
        # ньому є звернення. Інакше історія гарантійних випадків зникла б
        # разом із номером — а це якраз та інформація, яку треба зберігати
        # роками для звірки з вендором.
        ondelete='restrict',
        tracking=True,
        index=True,
        help='Оберіть серійний номер. Інші поля заповняться автоматично.',
    )

    # related-поля: читають значення з серійного номера.
    # НЕ дублюємо дані — джерело істини одне.
    product_id = fields.Many2one(
        comodel_name='product.product',
        related='lot_id.product_id',
        string='Товар',
        store=True,
        index=True,
    )

    # ⚠️ ТУТ НЕМАЄ окремого поля serial_number, і це свідоме рішення.
    #
    # У першій версії я додав:
    #     serial_number = fields.Char(related='lot_id.name', store=True)
    # і Odoo попередив:
    #     Two fields (serial_number, lot_id) of techdistrib.warranty.claim()
    #     have the same label: Серійний номер
    #
    # Причина: display_name серійного номера ВЖЕ є його номером. Поле
    # дублювало те саме значення під тим самим підписом, і користувач
    # бачив би у формі два однакові поля — яке з них правити, незрозуміло.
    #
    # Для пошуку по номеру достатньо lot_id.name:
    #     [('lot_id.name', 'ilike', 'SN-123')]
    # а у списках і формі <field name="lot_id"/> показує номер як текст.
    #
    # ПРАВИЛО: перед тим як додати related-поле, спитай себе —
    # а чи не показує те саме значення вже наявне поле?

    # =========================================================================
    #  ЛАНЦЮЖОК ДОКАЗІВ
    # =========================================================================
    #  Ці поля ЗБЕРІГАЮТЬСЯ (store=True) на момент створення звернення.
    #  Причина та сама, що в модулі кредитних запитів: якщо через рік
    #  хтось змінить дані серійного номера або замовлення, звернення
    #  має лишитись юридично достовірним документом.
    # =========================================================================

    sale_order_id = fields.Many2one(
        comodel_name='sale.order',
        string='Замовлення продажу',
        readonly=True,
        copy=False,
        help='Наше замовлення, за яким номер пішов клієнту.',
    )

    sold_to_partner_id = fields.Many2one(
        comodel_name='res.partner',
        string='Продано клієнту',
        readonly=True,
        copy=False,
        help='Кому МИ відвантажили цей номер.',
    )

    delivery_date = fields.Date(
        string='Дата відвантаження',
        readonly=True,
        copy=False,
    )

    # =========================================================================
    #  УЧАСНИКИ ЗВЕРНЕННЯ
    # =========================================================================

    partner_id = fields.Many2one(
        comodel_name='res.partner',
        string='Хто звернувся',
        required=True,
        tracking=True,
        help='Дилер або кінцевий клієнт, який приніс обладнання.',
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
    #  ГАРАНТІЯ
    # =========================================================================

    warranty_start_date = fields.Date(
        string='Початок гарантії',
        related='lot_id.warranty_start_date',
        store=True,
        readonly=True,
    )

    warranty_end_date = fields.Date(
        string='Кінець гарантії',
        related='lot_id.warranty_end_date',
        store=True,
        readonly=True,
    )

    warranty_months = fields.Integer(
        string='Термін гарантії, міс.',
        related='lot_id.warranty_months_applied',
        readonly=True,
    )

    is_our_product = fields.Boolean(
        string='Наш товар',
        compute='_compute_warranty_check',
        store=True,
        help='Ми справді продавали цей серійний номер. '
             'Якщо ні — звернення підлягає відхиленню.',
    )

    is_under_warranty = fields.Boolean(
        string='Гарантія дійсна',
        compute='_compute_warranty_check',
        store=True,
        help='Чи діє гарантія на дату звернення.',
    )

    warranty_check_note = fields.Char(
        string='Результат перевірки',
        compute='_compute_warranty_check_note',
        help='Живий текст із поясненням, чому гарантія діє або не діє.',
    )

    # =========================================================================
    #  ЖИТТЄВИЙ ЦИКЛ ЗВЕРНЕННЯ
    # =========================================================================

    state = fields.Selection(
        selection=[
            ('draft', 'Чернетка'),
            ('confirmed', 'Прийнято'),
            ('diagnostics', 'Діагностика'),
            ('waiting_vendor', 'Чекає вендора'),
            ('resolved', 'Вирішено'),
            ('rejected', 'Відхилено'),
            ('closed', 'Закрито'),
        ],
        string='Стан',
        default='draft',
        required=True,
        tracking=True,
        index=True,
        group_expand='_group_expand_states',
    )

    # =========================================================================
    #  ЗМІСТ ЗВЕРНЕННЯ
    # =========================================================================

    problem_description = fields.Text(
        string='Опис несправності',
        required=True,
        tracking=True,
        help='Зі слів клієнта: що саме не працює.',
    )

    diagnosis = fields.Text(
        string='Діагностика',
        help='Що виявив сервісний інженер після перевірки.',
    )

    resolution = fields.Selection(
        selection=[
            ('repair', 'Ремонт'),
            ('replace', 'Заміна на новий'),
            ('refund', 'Повернення коштів'),
            ('reject', 'Відмова'),
        ],
        string='Рішення',
        tracking=True,
    )

    resolution_note = fields.Text(
        string='Обґрунтування рішення',
        help='Обовʼязкове при відмові — клієнт має розуміти причину.',
    )

    # =========================================================================
    #  ГРОШІ (FR-E06)
    # =========================================================================

    is_chargeable = fields.Boolean(
        string='Платний ремонт',
        compute='_compute_warranty_check',
        store=True,
        help='True, якщо гарантія не діє — ремонт виконується за кошти клієнта.',
    )

    repair_cost = fields.Monetary(
        string='Вартість ремонту',
        currency_field='currency_id',
        tracking=True,
    )

    vendor_rma_reference = fields.Char(
        string='Номер RMA у вендора',
        tracking=True,
        help='Номер, під яким вендор прийняв наше звернення. '
             'Потрібен для звірки компенсацій.',
    )

    vendor_compensation = fields.Monetary(
        string='Компенсація вендора',
        currency_field='currency_id',
        help='Скільки вендор компенсував за цим зверненням.',
    )

    # =========================================================================
    #  ДАТИ ТА КОНТРОЛЬ СТРОКІВ (FR-E07)
    # =========================================================================

    reported_date = fields.Date(
        string='Дата звернення',
        required=True,
        default=fields.Date.context_today,
        tracking=True,
        index=True,
    )

    deadline_date = fields.Date(
        string='Граничний строк',
        compute='_compute_deadline',
        store=True,
        help='Дата звернення + %s днів.' % CLAIM_DEADLINE_DAYS,
    )

    is_overdue = fields.Boolean(
        string='Прострочено',
        compute='_compute_deadline',
        store=True,
        index=True,
        help='Звернення в роботі довше граничного строку.',
    )

    resolved_date = fields.Datetime(
        string='Дата рішення',
        readonly=True,
    )

    closed_date = fields.Datetime(
        string='Дата закриття',
        readonly=True,
    )

    user_id = fields.Many2one(
        comodel_name='res.users',
        string='Відповідальний',
        default=lambda self: self.env.user,
        tracking=True,
        index=True,
    )

    # =========================================================================
    #  SQL-ОБМЕЖЕННЯ
    # =========================================================================

    _sql_constraints = [
        ('repair_cost_positive', 'check(repair_cost >= 0)',
         'Вартість ремонту не може бути відʼємною!'),
    ]

    # =========================================================================
    #  ОБЧИСЛЮВАЧІ
    # =========================================================================

    def _evaluate_warranty(self):
        """Спільна логіка перевірки гарантії.

        Повертає кортеж (is_our_product, is_under_warranty,
        is_chargeable, note).

        ⚠️ ЧОМУ ЛОГІКА У ЗВИЧАЙНОМУ МЕТОДІ, А НЕ В ОБЧИСЛЮВАЧІ —
        і це вже ТРЕТІЙ раз, коли я наступаю на цю граблину в проєкті.

        Спочатку я написав ОДИН обчислювач, який присвоював усі чотири
        поля: три зі store=True (is_our_product, is_under_warranty,
        is_chargeable) і одне без store (warranty_check_note).
        Odoo попередив:

            UserWarning: techdistrib.warranty.claim: inconsistent 'store'
            for computed fields, accessing warranty_check_note may recompute
            and update is_our_product, is_under_warranty, is_chargeable.

        Причина та сама, що в Модулі 1: незбережене поле рахується при
        КОЖНОМУ читанні, а метод заразом перезаписує збережені поля.
        Результат — несподівані UPDATE-и під час звичайного перегляду.

        РІШЕННЯ: винести спільну логіку в окремий метод, який повертає
        значення, і зробити ДВА тонкі обчислювачі — окремо для
        збережених полів і окремо для незбереженого.

        Це загальний патерн: «одна логіка — два обчислювачі».
        Дублювання коду немає: обидва викликають _evaluate_warranty().
        """
        self.ensure_one()
        lot = self.lot_id

        # Випадок 1: номер не продавали ми.
        if not lot or not lot.warranty_start_date:
            return (
                False, False, True,
                _('Цей серійний номер не значиться серед проданих нами. '
                  'Гарантійне обслуговування неможливе.'),
            )

        # Порівнюємо з датою ЗВЕРНЕННЯ, а не з сьогоднішньою.
        # Це принципово: якщо клієнт приніс обладнання 5 числа, а ми
        # оформлюємо документ 10 числа, гарантія має оцінюватись на
        # 5 число. Інакше пристрій із гарантією, що закінчилась 7 числа,
        # визнали б позагарантійним.
        check_date = self.reported_date or fields.Date.context_today(self)

        if lot.warranty_end_date and lot.warranty_end_date >= check_date:
            days_left = (lot.warranty_end_date - check_date).days
            return (
                True, True, False,
                _('Товар наш. Гарантія діє до %(date)s (ще %(days)s дн.).',
                  date=lot.warranty_end_date, days=days_left),
            )

        return (
            True, False, True,
            _('Товар наш, але гарантія закінчилась %(date)s. '
              'Ремонт виконується за кошти клієнта.',
              date=lot.warranty_end_date or _('невідомо')),
        )

    @api.depends('lot_id', 'lot_id.warranty_end_date', 'lot_id.warranty_start_date',
                 'reported_date')
    def _compute_warranty_check(self):
        """FR-E02, FR-E03: ЗБЕРЕЖЕНІ поля перевірки гарантії.

        Тут лише поля зі store=True — щоб Odoo не попереджав про
        змішування режимів збереження.
        """
        for claim in self:
            (claim.is_our_product,
             claim.is_under_warranty,
             claim.is_chargeable,
             _note) = claim._evaluate_warranty()

    @api.depends('lot_id', 'lot_id.warranty_end_date', 'lot_id.warranty_start_date',
                 'reported_date')
    def _compute_warranty_check_note(self):
        """Текстовий результат перевірки — НЕ зберігається.

        Свідомо не зберігаємо: у тексті є «ще N днів», а це значення
        змінюється щодня. Збережене поле «протухло» б за добу.
        """
        for claim in self:
            claim.warranty_check_note = claim._evaluate_warranty()[3]

    @api.depends('reported_date', 'state')
    def _compute_deadline(self):
        """FR-E07: граничний строк і позначка прострочення.

        ⚠️ Поле is_overdue ЗБЕРІГАЄТЬСЯ, хоч і залежить від дати.
        Це свідомий компроміс, і ось у чому він:

          * is_overdue потрібен у ФІЛЬТРАХ («покажи прострочені»),
            а фільтрувати по незбереженому полю без search= не можна
            (Odoo мовчки проігнорує умову — див. Модуль 2);
          * але збережене значення «протухає»: учора False, сьогодні
            має стати True, а ніщо не змінилось.

        Рішення — cron, який щодня перераховує поле (як у Модулі 1 зі
        статусом дилера). Це той самий патерн «часової залежності»:
            збережене поле + cron = швидкий пошук + актуальність.
        """
        today = fields.Date.context_today(self)
        for claim in self:
            if not claim.reported_date:
                claim.deadline_date = False
                claim.is_overdue = False
                continue

            claim.deadline_date = claim.reported_date + relativedelta(
                days=CLAIM_DEADLINE_DAYS)

            # Простроченим вважаємо лише те, що ЩЕ В РОБОТІ.
            # Закрите звернення не може бути простроченим, навіть якщо
            # його вирішили пізніше за граничний строк — інакше звіт
            # «прострочені» завжди показував би половину закритих.
            open_states = ('draft', 'confirmed', 'diagnostics', 'waiting_vendor')
            claim.is_overdue = (
                claim.state in open_states
                and claim.deadline_date < today
            )

    @api.model
    def _group_expand_states(self, states, domain):
        return [key for key, _label in self._fields['state'].selection]

    # =========================================================================
    #  СТВОРЕННЯ ТА АВТОЗАПОВНЕННЯ
    # =========================================================================

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('Нове')) == _('Нове'):
                vals['name'] = (
                    self.env['ir.sequence'].next_by_code('techdistrib.warranty.claim')
                    or _('Нове')
                )
        claims = super().create(vals_list)
        # Автозаповнення ланцюжка доказів — ПІСЛЯ створення, бо потрібен
        # запис із id (як і в синхронізації матриці знижок).
        claims._fill_evidence_chain()
        return claims

    @api.onchange('lot_id')
    def _onchange_lot_id(self):
        """Заповнити поля у ФОРМІ одразу при виборі номера.

        ⚠️ Це onchange — він працює ЛИШЕ в інтерфейсі. Для імпорту,
        API та масового створення його недостатньо. Тому та сама логіка
        викликається з create через _fill_evidence_chain().

        Це класична пастка: зробити лише onchange і потім дивуватись,
        чому при імпорті з Excel поля порожні.
        """
        for claim in self:
            if claim.lot_id:
                lot = claim.lot_id
                claim.sold_to_partner_id = lot.warranty_customer_id
                claim.sale_order_id = lot.warranty_sale_order_id
                claim.delivery_date = lot.warranty_start_date
                if not claim.partner_id:
                    claim.partner_id = lot.warranty_customer_id

    def _fill_evidence_chain(self):
        """Заповнити ланцюжок доказів із серійного номера."""
        for claim in self:
            if not claim.lot_id:
                continue
            lot = claim.lot_id
            vals = {}
            if not claim.sold_to_partner_id:
                vals['sold_to_partner_id'] = lot.warranty_customer_id.id
            if not claim.sale_order_id:
                vals['sale_order_id'] = lot.warranty_sale_order_id.id
            if not claim.delivery_date:
                vals['delivery_date'] = lot.warranty_start_date
            if vals:
                claim.write(vals)

    # =========================================================================
    #  ПЕРЕВІРКИ
    # =========================================================================

    @api.constrains('resolution', 'resolution_note')
    def _check_rejection_reason(self):
        """Відмова без обґрунтування заборонена (FR-E05)."""
        for claim in self:
            if claim.resolution == 'reject' and not claim.resolution_note:
                raise ValidationError(_(
                    'Перш ніж відхилити звернення %s, вкажіть обґрунтування '
                    'в полі «Обґрунтування рішення».\n\n'
                    'Клієнт має право знати, чому йому відмовили.',
                    claim.name,
                ))

    @api.constrains('resolution', 'is_chargeable', 'repair_cost')
    def _check_repair_cost(self):
        """Платний ремонт має мати вказану вартість (FR-E06)."""
        for claim in self:
            if claim.resolution == 'repair' and claim.is_chargeable and not claim.repair_cost:
                raise ValidationError(_(
                    'Ремонт за кошти клієнта має мати вказану вартість.\n'
                    'Інакше неможливо ні виставити рахунок, ні порахувати '
                    'збиток від позагарантійного обслуговування.',
                ))

    # =========================================================================
    #  ЖИТТЄВИЙ ЦИКЛ
    # =========================================================================

    def action_confirm(self):
        """Прийняти звернення в роботу.

        ⚠️ Тут ми ЗАБОРОНЯЄМО приймати звернення по «не нашому» номеру.
        Це різновид бізнес-правила, яке рятує від шахрайства: спроба
        здати в гарантію обладнання, яке ми не продавали.
        """
        for claim in self:
            if claim.state != 'draft':
                raise UserError(_('Прийняти можна лише звернення в чернетці.'))
            if not claim.is_our_product:
                raise UserError(_(
                    'Неможливо прийняти звернення %(name)s.\n\n'
                    'Серійний номер «%(serial)s» відсутній серед проданих '
                    'нами. Відвантаження цього номера не знайдено.\n\n'
                    'Перевірте правильність номера. Якщо обладнання справді '
                    'наше — можливо, воно було відвантажене до впровадження '
                    'системи, і дату гарантії треба проставити вручну.',
                    name=claim.name,
                    serial=claim.lot_id.name,
                ))
        self.write({'state': 'confirmed'})
        return True

    def action_start_diagnostics(self):
        self.write({'state': 'diagnostics'})
        return True

    def action_send_to_vendor(self):
        """Передати звернення вендору (для компенсації)."""
        self.write({'state': 'waiting_vendor'})
        return True

    def action_resolve(self):
        """Зафіксувати рішення за зверненням (FR-E05)."""
        for claim in self:
            if not claim.resolution:
                raise UserError(_(
                    'Вкажіть рішення за зверненням: ремонт, заміна, '
                    'повернення коштів або відмова.'
                ))
        self.write({
            'state': 'resolved',
            'resolved_date': fields.Datetime.now(),
        })
        return True

    def action_reject(self):
        self.write({'resolution': 'reject'})
        return self.action_resolve()

    def action_close(self):
        self.write({
            'state': 'closed',
            'closed_date': fields.Datetime.now(),
        })
        return True

    def action_reset_to_draft(self):
        self.write({
            'state': 'draft',
            'resolved_date': False,
            'closed_date': False,
        })
        return True

    # =========================================================================
    #  АВТОМАТИЗАЦІЯ (FR-E07)
    # =========================================================================

    @api.model
    def _cron_refresh_overdue_claims(self):
        """Щоденне оновлення позначки «прострочено» та нагадування.

        Дві дії в одній задачі:
          1. перерахувати збережене поле is_overdue (бо воно залежить
             від сьогоднішньої дати — див. коментар у _compute_deadline);
          2. створити «дію» (mail.activity) відповідальному, щоб
             звернення не «зависало» непомітно.
        """
        # 1. Перерахувати поле для всіх відкритих звернень.
        open_claims = self.search([
            ('state', 'in', ('draft', 'confirmed', 'diagnostics', 'waiting_vendor')),
        ])
        if not open_claims:
            return 0

        self.env.add_to_compute(self._fields['is_overdue'], open_claims)

        # 2. Створити нагадування для прострочених, у яких його ще немає.
        #    Перевірка на наявність активної дії — обов'язкова, інакше
        #    cron створював би нову задачу ЩОДНЯ, і через місяць у
        #    менеджера було б 30 однакових нагадувань.
        created = 0
        for claim in open_claims:
            if not claim.is_overdue:
                continue
            if claim.activity_ids:
                continue   # уже є активна дія — не дублюємо
            claim.activity_schedule(
                activity_type_id=self.env.ref('mail.mail_activity_data_todo').id,
                summary=_('Прострочене гарантійне звернення %s', claim.name),
                note=_(
                    'Звернення від %(date)s перебуває в роботі понад '
                    '%(days)s днів. Серійний номер: %(serial)s.',
                    date=claim.reported_date,
                    days=CLAIM_DEADLINE_DAYS,
                    serial=claim.lot_id.name,
                ),
                user_id=claim.user_id.id or self.env.user.id,
            )
            created += 1

        return created
