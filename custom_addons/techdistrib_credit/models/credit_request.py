# -*- coding: utf-8 -*-
# =============================================================================
#  Модель: techdistrib.credit.request — запит на перевищення кредитного ліміту
# =============================================================================
#  Це НОВА модель і НОВИЙ БІЗНЕС-ПРОЦЕС, якого в Odoo немає взагалі.
#
#  Навіщо окрема модель, а не просто «дозволити підтвердити»?
#  Бо бізнес вимагає (BRD, FR-C05, NFR-04):
#     * фіксувати, ХТО попросив, КОЛИ і ЧОМУ;
#     * фіксувати, ХТО схвалив і КОЛИ;
#     * мати змогу відхилити з причиною;
#     * бачити чергу запитів, що чекають рішення;
#     * мати строк дії схвалення.
#  Жодне з цього неможливо зробити «прапорцем на замовленні».
#
#  Це типовий приклад того, коли бізнес-процес вимагає ВЛАСНОЇ СУТНОСТІ.
#  Ознака: якщо в процесу є власний життєвий цикл (стани) і власні атрибути
#  (хто, коли, чому) — це модель.
# =============================================================================

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

# Технічна назва групи кредитних контролерів.
# Виносимо в константу: якщо колись перейменуємо групу, правка буде в одному місці.
CREDIT_CONTROLLER_GROUP = 'techdistrib_credit.group_credit_controller'

# Ключ системного параметра для типового строку дії схвалення.
# ir.config_parameter — стандартний спосіб зберігати налаштування в Odoo.
VALIDITY_PARAM = 'techdistrib.credit.validity_days'


class TechdistribCreditRequest(models.Model):
    _name = 'techdistrib.credit.request'
    _description = 'Запит на перевищення кредитного ліміту'

    # --- Міксини --------------------------------------------------------------
    # mail.thread        — додає chatter (історію повідомлень) і tracking полів
    # mail.activity.mixin — додає «дії» (нагадування з дедлайном)
    #
    # Це стандартні «міксини» Odoo: вони не створюють окремих таблиць, а
    # додають поля та зв'язки. Використовуй їх для будь-якої моделі, з якою
    # працюють люди: безкоштовно отримуєш історію змін і аудит.
    _inherit = ['mail.thread', 'mail.activity.mixin']

    _order = 'create_date desc, id desc'

    # =========================================================================
    #  ІДЕНТИФІКАЦІЯ
    # =========================================================================

    name = fields.Char(
        string='Номер',
        required=True,
        readonly=True,
        copy=False,
        default=lambda self: _('Новий'),
        index=True,
    )

    # =========================================================================
    #  УЧАСНИКИ
    # =========================================================================

    partner_id = fields.Many2one(
        comodel_name='res.partner',
        string='Дилер',
        required=True,
        ondelete='cascade',
        tracking=True,
        # domain обмежує вибір лише дилерами — щоб не створити запит
        # на «перевищення ліміту» для партнера, у якого ліміту немає.
        domain="[('is_dealer', '=', True)]",
    )

    # related — «дзеркальне» поле. Воно не зберігається, а читає значення
    # з пов'язаного запису. Зручно для пошуку й групування по юридичній особі
    # (у дилера можуть бути дочірні контакти).
    commercial_partner_id = fields.Many2one(
        comodel_name='res.partner',
        related='partner_id.commercial_partner_id',
        string='Юридична особа',
        store=True,
        index=True,
    )

    sale_order_id = fields.Many2one(
        comodel_name='sale.order',
        string='Замовлення',
        ondelete='cascade',
        tracking=True,
    )

    company_id = fields.Many2one(
        comodel_name='res.company',
        string='Компанія',
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )

    currency_id = fields.Many2one(
        comodel_name='res.currency',
        string='Валюта',
        related='company_id.currency_id',
        # ⚠️ store=True разом із required=True — важлива деталь:
        # related-поле зі store=True стає СПРАВЖНЬОЮ колонкою в базі й
        # оновлюється, коли змінюється джерело. Без store кожне читання
        # робило б зайвий JOIN.
        store=True,
        readonly=True,
    )

    # =========================================================================
    #  СУМИ (знімок на момент запиту)
    # =========================================================================
    #  ⚠️ КЛЮЧОВЕ АРХІТЕКТУРНЕ РІШЕННЯ: ми ЗБЕРІГАЄМО суми на момент запиту,
    #  а не перераховуємо їх щоразу.
    #
    #  Чому: якщо через місяць кредитний контролер відкриє старий схвалений
    #  запит, він має побачити ТІ САМІ цифри, на підставі яких ухвалював
    #  рішення. Інакше аудит втрачає сенс: суми «пливуть» разом із боргом
    #  дилера, і неможливо зрозуміти, що саме схвалювали.
    #
    #  Це загальний принцип для будь-якого документ-процесу: знімок стану
    #  на момент рішення зберігається окремо від поточного стану сутності.
    # =========================================================================

    order_amount = fields.Monetary(
        string='Сума замовлення',
        currency_field='currency_id',
        tracking=True,
        help='Сума замовлення у валюті компанії на момент створення запиту.',
    )

    credit_limit = fields.Monetary(
        string='Кредитний ліміт',
        currency_field='currency_id',
        help='Ліміт дилера на момент створення запиту (знімок).',
    )

    current_debt = fields.Monetary(
        string='Поточний борг',
        currency_field='currency_id',
        help='Дебіторка дилера на момент створення запиту (знімок).',
    )

    total_exposure = fields.Monetary(
        string='Разом після замовлення',
        currency_field='currency_id',
        compute='_compute_amounts',
        store=True,
        help='Борг + сума замовлення.',
    )

    excess_amount = fields.Monetary(
        string='Перевищення ліміту',
        currency_field='currency_id',
        compute='_compute_amounts',
        store=True,
        help='На скільки саме буде перевищено ліміт.',
    )

    # =========================================================================
    #  ЖИТТЄВИЙ ЦИКЛ
    # =========================================================================

    state = fields.Selection(
        selection=[
            ('pending', 'На погодженні'),
            ('approved', 'Схвалено'),
            ('rejected', 'Відхилено'),
            ('expired', 'Прострочено'),
            ('cancelled', 'Скасовано'),
        ],
        string='Стан',
        default='pending',
        required=True,
        tracking=True,
        index=True,
        # group_expand змушує канбан-подання показувати ВСІ колонки,
        # навіть порожні. Без цього колонка зникає, коли в ній немає
        # записів, і користувач не може перетягнути туди картку.
        group_expand='_group_expand_states',
    )

    request_note = fields.Text(
        string='Причина запиту',
        help='Чому менеджер просить перевищити ліміт. Обовʼязково для аудиту.',
    )

    decision_note = fields.Text(
        string='Обґрунтування рішення',
        tracking=True,
    )

    # --- Хто і коли -----------------------------------------------------------
    requested_by = fields.Many2one(
        comodel_name='res.users',
        string='Запросив',
        default=lambda self: self.env.user,
        readonly=True,
        index=True,
    )

    decided_by = fields.Many2one(
        comodel_name='res.users',
        string='Рішення ухвалив',
        readonly=True,
    )

    decided_on = fields.Datetime(
        string='Дата рішення',
        readonly=True,
    )

    valid_until = fields.Datetime(
        string='Схвалення дійсне до',
        readonly=True,
        tracking=True,
        help='Після цієї дати схвалення втрачає силу, і замовлення '
             'знову буде заблоковано. Захищає від «вічного» дозволу.',
    )

    validity_days = fields.Integer(
        string='Строк дії (днів)',
        default=lambda self: self._default_validity_days(),
        required=True,
        help='Скільки днів діє схвалення. Типове значення береться '
             'із системного параметра.',
    )

    is_valid = fields.Boolean(
        string='Дійсний зараз',
        compute='_compute_is_valid',
        help='Схвалено І строк дії ще не минув.',
    )

    # --- Пов'язані ------------------------------------------------------------
    # Показуємо всі попередні запити по цьому дилеру — щоб контролер бачив
    # історію: «цьому дилеру вже тричі схвалювали перевищення за квартал».
    partner_request_ids = fields.One2many(
        comodel_name='techdistrib.credit.request',
        inverse_name='partner_id',
        string='Інші запити дилера',
        compute='_compute_partner_request_ids',
    )

    # =========================================================================
    #  ОБЧИСЛЮВАНІ ПОЛЯ
    # =========================================================================

    @api.depends('current_debt', 'order_amount', 'credit_limit')
    def _compute_amounts(self):
        """Разом після замовлення та сума перевищення."""
        for request in self:
            request.total_exposure = request.current_debt + request.order_amount
            # max(0, ...) — щоб не показувати від'ємне перевищення.
            # Ліміт 0 у Odoo означає «без ліміту», тому перевищення теж 0.
            if not request.credit_limit:
                request.excess_amount = 0.0
            else:
                request.excess_amount = max(
                    0.0, request.total_exposure - request.credit_limit
                )

    @api.depends('state', 'valid_until')
    def _compute_is_valid(self):
        """Чи дійсне схвалення ПРЯМО ЗАРАЗ.

        Це поле залежить від поточної дати, тому воно НЕ може бути stored:
        збережене значення «протухло» б наступного дня, і жоден сигнал не
        повідомив би Odoo про необхідність перерахунку.
        """
        now = fields.Datetime.now()
        for request in self:
            request.is_valid = bool(
                request.state == 'approved'
                and request.valid_until
                and request.valid_until > now
            )

    @api.depends('partner_id')
    def _compute_partner_request_ids(self):
        # Дістаємо всі запити цього дилера, КРІМ поточного — щоб у формі
        # не було дублювання самого себе.
        for request in self:
            if request.partner_id:
                request.partner_request_ids = self.search([
                    ('partner_id', '=', request.partner_id.id),
                    ('id', '!=', request.id),
                ])
            else:
                request.partner_request_ids = False

    # =========================================================================
    #  ТИПОВІ ЗНАЧЕННЯ ТА СТВОРЕННЯ
    # =========================================================================

    @api.model
    def _default_validity_days(self):
        """Типовий строк дії схвалення із системного параметра.

        ir.config_parameter — це просте сховище «ключ → значення».
        Звертаємось через .sudo(), бо звичайний менеджер із продажу
        може не мати прав на читання системних параметрів, а отримати
        налаштування йому треба.
        """
        param = self.env['ir.config_parameter'].sudo().get_param(VALIDITY_PARAM)
        try:
            return int(param) if param else 7
        except (TypeError, ValueError):
            # Захисний код: якщо адміністратор вписав у параметр «сім»
            # замість «7», ми не падаємо, а повертаємось до типового значення.
            return 7

    @api.model
    def _group_expand_states(self, states, domain):
        """Показати в канбані всі колонки, навіть порожні."""
        return [key for key, _label in self._fields['state'].selection]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('Новий')) == _('Новий'):
                vals['name'] = (
                    self.env['ir.sequence'].next_by_code('techdistrib.credit.request')
                    or _('Новий')
                )
        return super().create(vals_list)

    # =========================================================================
    #  ПЕРЕВІРКИ
    # =========================================================================

    @api.constrains('credit_limit', 'order_amount', 'current_debt')
    def _check_amounts_not_negative(self):
        for request in self:
            if request.credit_limit < 0:
                raise ValidationError(_('Кредитний ліміт не може бути відʼємним.'))
            if request.order_amount < 0:
                raise ValidationError(_('Сума замовлення не може бути відʼємною.'))

    # =========================================================================
    #  БІЗНЕС-ЛОГІКА: СХВАЛЕННЯ ТА ВІДХИЛЕННЯ
    # =========================================================================

    def _ensure_credit_controller(self):
        """Перевірка, що рішення ухвалює саме кредитний контролер.

        ⚠️ Це ДРУГИЙ рубіж захисту, а не перший.
        Перший — права доступу в ir.model.access.csv і ir.rule.
        Але ACL грубі: вони кажуть «можна писати в модель», а не
        «можна змінювати СТАН». Тому бізнес-перевірка в коді обов'язкова.

        Правило безпеки: права доступу визначають, ЩО людина бачить;
        бізнес-логіка визначає, ЩО їй дозволено робити. Це різні речі,
        і потрібні обидві.
        """
        if not self.env.user.has_group(CREDIT_CONTROLLER_GROUP):
            raise UserError(_(
                'Лише кредитний контролер може схвалювати або відхиляти '
                'запити на перевищення кредитного ліміту.\n\n'
                'Зверніться до кредитного контролера або адміністратора.'
            ))

    def action_approve(self):
        """Схвалити перевищення ліміту на строк validity_days."""
        self._ensure_credit_controller()

        for request in self:
            if request.state != 'pending':
                raise UserError(_(
                    'Запит %(name)s перебуває у стані «%(state)s» і не може '
                    'бути схвалений. Схвалювати можна лише запити, що '
                    'очікують рішення.',
                    name=request.name,
                    state=dict(request._fields['state'].selection).get(request.state),
                ))

            # now() + relativedelta(days=...) — дата й час у UTC.
            # Odoo зберігає всі Datetime у UTC і конвертує в часову зону
            # користувача лише для показу. Ніколи не зберігай локальний час.
            from dateutil.relativedelta import relativedelta
            request.write({
                'state': 'approved',
                'decided_by': self.env.user.id,
                'decided_on': fields.Datetime.now(),
                'valid_until': fields.Datetime.now() + relativedelta(
                    days=request.validity_days
                ),
            })

            # Повідомляємо в chatter замовлення — щоб менеджер, який чекає
            # на дозвіл, побачив це на своєму замовленні, а не шукав у
            # окремому списку запитів.
            #
            # ⚠️ ЧОМУ .sudo() — реальний AccessError, який я тут зловив:
            #
            # Перша версія коду викликала request.sale_order_id.message_post()
            # напряму. У тестах це впало:
            #     odoo.exceptions.AccessError: The requested operation cannot
            #     be completed due to security restrictions.
            #
            # Причина — стандартне правило Odoo sale.sale_order_personal_rule:
            # менеджер із продажу бачить лише СВОЇ замовлення
            # ([('user_id', '=', user.id)]). Кредитний контролер не є
            # автором замовлення, тому Odoo забороняє йому створювати
            # mail.message на цьому документі.
            #
            # Чому sudo() тут ПРАВИЛЬНИЙ, а не «дірка в безпеці»:
            #   * це не дія користувача, а СИСТЕМНЕ СПОВІЩЕННЯ про подію,
            #     яка вже відбулась (рішення ухвалено й збережено);
            #   * ми не показуємо контролеру жодних даних замовлення —
            #     ми лише ДОПИСУЄМО повідомлення в історію;
            #   * без цього бізнес-процес ламається: контролер фізично
            #     не може повідомити менеджера про своє рішення.
            #
            # Загальне правило: sudo() виправданий, коли код виконує
            # СИСТЕМНУ дію від імені системи, а не обходить перевірку
            # прав заради зручності користувача.
            if request.sale_order_id:
                request.sale_order_id.sudo().message_post(body=_(
                    'Кредитне перевищення схвалено: %(name)s.\n'
                    'Схвалив: %(user)s.\n'
                    'Дійсне до: %(until)s.\n'
                    'Перевищення: %(excess)s.',
                    name=request.name,
                    user=self.env.user.name,
                    until=request.valid_until,
                    excess=request.excess_amount,
                ))

        return True

    def action_reject(self):
        """Відхилити запит. Обґрунтування обовʼязкове."""
        self._ensure_credit_controller()

        for request in self:
            if request.state != 'pending':
                raise UserError(_(
                    'Відхилити можна лише запит у стані «На погодженні».'
                ))
            # NFR-04: рішення без обґрунтування не має юридичної сили
            # і не допомагає менеджеру зрозуміти, що робити далі.
            if not request.decision_note:
                raise UserError(_(
                    'Перш ніж відхилити запит, вкажіть обґрунтування '
                    'в полі «Обґрунтування рішення».\n\n'
                    'Менеджер має розуміти, чому відмовили і що зробити, '
                    'щоб отримати схвалення наступного разу.'
                ))

            request.write({
                'state': 'rejected',
                'decided_by': self.env.user.id,
                'decided_on': fields.Datetime.now(),
                'valid_until': False,
            })

            if request.sale_order_id:
                request.sale_order_id.sudo().message_post(body=_(
                    'Кредитне перевищення ВІДХИЛЕНО: %(name)s.\n'
                    'Рішення ухвалив: %(user)s.\n'
                    'Причина: %(note)s',
                    name=request.name,
                    user=self.env.user.name,
                    note=request.decision_note,
                ))

        return True

    def action_reset_to_pending(self):
        """Повернути запит на повторний розгляд."""
        self._ensure_credit_controller()
        self.write({
            'state': 'pending',
            'decided_by': False,
            'decided_on': False,
            'valid_until': False,
        })
        return True

    def action_cancel(self):
        """Скасувати запит (напр., менеджер зменшив суму замовлення)."""
        # Скасувати може і автор запиту, і контролер
        for request in self:
            if request.state in ('approved', 'rejected', 'cancelled'):
                raise UserError(_('Цей запит уже закрито і не може бути скасований.'))
        self.write({'state': 'cancelled'})
        return True

    # =========================================================================
    #  АВТОМАТИЗАЦІЯ: ПРОСТРОЧЕННЯ СХВАЛЕНЬ
    # =========================================================================

    @api.model
    def _cron_expire_credit_approvals(self):
        """Прострочити схвалення, строк дії яких минув (FR-C06).

        Це знову приклад «часової залежності»: поле is_valid рахується
        на льоту й завжди правильне, але СТАН запису в базі сам не
        зміниться. А нам потрібно, щоб прострочені схвалення не просто
        «не працювали», а мали явний стан — інакше звіти й фільтри
        показуватимуть їх як активні дозволи.

        Тому cron переводить їх у стан expired.
        """
        now = fields.Datetime.now()
        expired = self.search([
            ('state', '=', 'approved'),
            ('valid_until', '!=', False),
            ('valid_until', '<', now),
        ])
        for request in expired:
            request.write({'state': 'expired'})
            if request.sale_order_id:
                # Тут cron і так виконується від імені __system__, але
                # явне sudo() робить намір очевидним і захищає від
                # випадкової зміни user_id у записі ir.cron.
                request.sale_order_id.sudo().message_post(body=_(
                    'Строк дії схвалення %(name)s минув (%(until)s). '
                    'Для підтвердження замовлення потрібне нове погодження.',
                    name=request.name,
                    until=request.valid_until,
                ))
        return len(expired)
