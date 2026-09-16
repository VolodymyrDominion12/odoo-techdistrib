# -*- coding: utf-8 -*-
# =============================================================================
#  Перевизначення sale.order: контроль кредитного ліміту
# =============================================================================
#  Це ЯДРО кастомної логіки всього проєкту. Тут ми:
#
#    1. ПЕРЕВИЗНАЧАЄМО action_confirm() — метод, який Odoo викликає при
#       натисканні кнопки «Підтвердити». Додаємо перевірку ПЕРЕД тим,
#       як віддати керування стандартній логіці через super().
#
#    2. РОЗШИРЮЄМО стандартний банер попередження partner_credit_warning,
#       не замінюючи його.
#
#    3. ДОДАЄМО кнопку «Запросити погодження» і лічильник запитів.
#
#  ⚠️ ПРО ПОРЯДОК ВИКЛИКІВ І ТРАНЗАКЦІЇ — найважливіше в цьому файлі.
#
#  Ми перевіряємо ліміт ДО super(). Чому це принципово:
#
#    action_confirm() робить багато: змінює стан, створює відвантаження
#    (stock.picking), резервує товар, створює записи в журналі. Усе це —
#    одна транзакція Postgres.
#
#    Якби ми перевіряли ліміт ПІСЛЯ super() і тоді піднімали помилку,
#    Odoo відкотив би транзакцію — і всі побічні ефекти зникли б. Але:
#      * користувач побачив би помилку після «успішного» підтвердження;
#      * у логах лишились би сліди спроб;
#      * а найгірше — частина логіки могла б виконатись у СУМІЖНИХ
#        транзакціях (напр., відправка email або повідомлення в cron),
#        і відкат їх би не зачепив.
#
#    Тому правило: ПЕРЕВІРКА — ПЕРШОЮ, ДІЯ — ПОТІМ.
#    Перевіряй на «вході» в метод, а не на «виході».
# =============================================================================

import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    # =========================================================================
    #  ПОЛЯ
    # =========================================================================

    credit_request_ids = fields.One2many(
        comodel_name='techdistrib.credit.request',
        inverse_name='sale_order_id',
        string='Запити на кредит',
    )

    credit_request_count = fields.Integer(
        string='Запитів',
        compute='_compute_credit_request_count',
    )

    credit_valid_approval_id = fields.Many2one(
        comodel_name='techdistrib.credit.request',
        string='Дійсне схвалення',
        compute='_compute_credit_block_info',
        compute_sudo=True,
    )

    credit_block_reason = fields.Char(
        string='Причина блокування',
        compute='_compute_credit_block_info',
        compute_sudo=True,
    )

    credit_is_blocked = fields.Boolean(
        string='Заблоковано за кредитом',
        compute='_compute_credit_block_info',
        compute_sudo=True,
    )

    show_credit_approval_button = fields.Boolean(
        string='Показати кнопку запиту',
        compute='_compute_credit_block_info',
        compute_sudo=True,
        help='True, коли замовлення заблоковане І ще немає активного запиту. '
             'Керує видимістю кнопки «Запросити погодження» на формі.',
    )

    # =========================================================================
    #  ОБЧИСЛЮВАЧІ
    # =========================================================================

    @api.depends('credit_request_ids', 'credit_request_ids.state')
    def _compute_credit_request_count(self):
        for order in self:
            order.credit_request_count = len(order.credit_request_ids)

    @api.depends(
        'partner_id',
        'amount_total',
        'currency_rate',
        'state',
        'credit_request_ids.state',
        'credit_request_ids.valid_until',
        'partner_id.credit',
        'partner_id.credit_to_invoice',
        'partner_id.credit_limit',
        'partner_id.credit_overdue_amount',
    )
    def _compute_credit_block_info(self):
        for order in self:
            reason = order._get_credit_block_reason()
            order.credit_block_reason = reason or False
            order.credit_is_blocked = bool(reason)
            order.credit_valid_approval_id = order._get_valid_credit_approval()

            order.show_credit_approval_button = bool(reason) and not order.credit_request_ids.filtered(
                lambda r: r.state == 'pending'
            )

    # =========================================================================
    #  БІЗНЕС-ЛОГІКА
    # =========================================================================

    def _get_credit_exposure(self):
        """Повертає (поточний_борг, сума_замовлення) у валюті КОМПАНІЇ.

        ⚠️ ПРО ВАЛЮТИ — тут легко помилитись і отримати безглузді числа.

        res.partner.credit та credit_to_invoice — це суми у ВАЛЮТІ КОМПАНІЇ
        (вони рахуються з бухгалтерських проводок у валюті компанії).
        А sale.order.amount_total — у ВАЛЮТІ ЗАМОВЛЕННЯ (може бути USD чи EUR).

        Порівнювати їх напряму не можна: $10 000 ≠ 10 000 ₴.
        Тому суму замовлення треба сконвертувати у валюту компанії,
        поділивши на currency_rate (курс на дату замовлення).

        Саме так робить і сам Odoo у sale/models/sale_order.py:
            current_amount=(order.amount_total / order.currency_rate)

        Якщо забути про конвертацію, ліміт «працював» би неправильно
        для всіх валютних замовлень — а в дистрибуції з імпортом
        валютних замовлень більшість.
        """
        self.ensure_one()
        partner = self.partner_id.commercial_partner_id
        if not partner:
            return 0.0, 0.0

        partner = partner.sudo()
        debt = partner.credit + partner.credit_to_invoice

        # currency_rate може бути 0 у дивних випадках (напр., валюта без курсу).
        # Ділення на нуль обвалило б підтвердження замовлення — захищаємось.
        rate = self.currency_rate or 1.0
        order_amount_company_currency = self.amount_total / rate

        return debt, order_amount_company_currency

    def _get_valid_credit_approval(self):
        """Знайти ДІЙСНЕ схвалення для цього замовлення, якщо воно є.

        «Дійсне» = стан 'approved' І строк valid_until ще не минув.
        Якщо схвалення прострочене, ми його не враховуємо — навіть якщо
        cron ще не встиг перевести його в стан 'expired'.

        Це важлива деталь: ми НЕ покладаємось на cron у критичній перевірці.
        Cron може не спрацювати (сервер лежав), тож перевірка має бути
        самодостатньою. Cron лише приводить дані до ладу для звітів.
        """
        self.ensure_one()
        return self.env['techdistrib.credit.request'].search([
            ('sale_order_id', '=', self.id),
            ('state', '=', 'approved'),
            ('valid_until', '>', fields.Datetime.now()),
        ], limit=1)

    def _get_credit_block_reason(self):
        """Головна перевірка. Повертає текст причини блокування або False.

        ЧОМУ ПОВЕРТАЄМО ТЕКСТ, А НЕ КИДАЄМО ВИКЛЮЧЕННЯ:
        бо цей самий метод використовують три різні споживачі:
          1. action_confirm()  — кидає UserError із цим текстом;
          2. computed-поле     — показує текст у банері на формі;
          3. кнопка            — вирішує, чи показувати «Запросити погодження».

        Якби метод кидав виключення, він не годився б для пунктів 2 і 3:
        обчислювач поля, що кидає помилку, зламав би відкриття форми.

        Загальний принцип: метод-ПЕРЕВІРКА має повертати результат,
        а метод-ДІЯ — кидати виключення. Не змішуй ці ролі.
        """
        self.ensure_one()

        # Перевіряємо лише замовлення, які ще не підтверджені.
        # Для вже підтвердженого замовлення блокування не має сенсу:
        # воно вже у роботі, і скасовувати його заднім числом не можна.
        if self.state not in ('draft', 'sent'):
            return False

        if not self.company_id.account_use_credit_limit:
            # Глобальний перемикач Odoo «Sales Credit Limit» вимкнено.
            # Ми ПОВАЖАЄМО це налаштування: якщо компанія вирішила не
            # контролювати ліміти, наш модуль не має ламати їй роботу.
            return False

        partner = self.partner_id.commercial_partner_id
        if not partner:
            return False

        partner = partner.sudo()

        # --- Перевірка 1: прострочена дебіторка (FR-C08) ----------------------
        # Вона ПЕРША за порядком, бо вона жорсткіша: навіть якщо ліміт
        # дозволяє, дилер із боргом 60 днів не має отримувати товар.
        if partner.credit_is_blocked:
            return _(
                'Дилер «%(partner)s» заблокований через прострочену дебіторку.\n'
                'Прострочено понад 30 днів: %(amount).2f %(currency)s\n'
                'Найстаріший прострочений рахунок: %(date)s\n\n'
                'Спочатку дилер має погасити прострочену заборгованість.',
                partner=partner.display_name,
                amount=partner.credit_overdue_amount,
                currency=partner.currency_id.name or '',
                date=partner.credit_oldest_overdue_date or '—',
            )

        # --- Перевірка 2: кредитний ліміт (FR-C03, FR-C04) --------------------
        limit = partner.credit_limit or 0.0
        if not limit:
            # 0 = ліміт не встановлено = без обмежень.
            # Це стандартна семантика Odoo, і ми її дотримуємось.
            return False

        debt, order_amount = self._get_credit_exposure()

        # ⚠️ Дрібниця, яка має велике значення: округлення.
        # Ми порівнюємо суми з точністю до копійки (round(..., 2)).
        # Без цього замовлення, що перевищує ліміт рівно на 0.0000001
        # через похибку float, блокувалося б — і менеджер би не зрозумів,
        # чому «все сходиться», а система не пускає.
        total_after = round(debt + order_amount, 2)
        limit_rounded = round(limit, 2)

        if total_after <= limit_rounded:
            return False

        # --- Перевірка 3: можливо, є дійсне схвалення ------------------------
        if self._get_valid_credit_approval():
            return False

        # --- Формуємо зрозуміле повідомлення ----------------------------------
        # Гарне повідомлення про помилку — це частина UX, а не «текст для логів».
        # Воно має казати: ЩО сталося, НА СКІЛЬКИ, і ЩО РОБИТИ ДАЛІ.
        return _(
            'Перевищено кредитний ліміт дилера «%(partner)s».\n\n'
            'Кредитний ліміт:        %(limit).2f %(currency)s\n'
            'Поточний борг:          %(debt).2f %(currency)s\n'
            'Сума цього замовлення:  %(amount).2f %(currency)s\n'
            '─────────────────────────────────────\n'
            'Разом буде:             %(total).2f %(currency)s\n'
            'Перевищення:            %(excess).2f %(currency)s\n\n'
            'Що робити:\n'
            '  1. Натисніть «Запросити погодження» на цьому замовленні.\n'
            '  2. Кредитний контролер розгляне запит і схвалить або відхилить.\n'
            '  3. Після схвалення підтвердження стане доступним '
            '(схвалення діє %(days)s днів).\n\n'
            'Або зменште суму замовлення, або дочекайтесь оплати від дилера.',
            partner=partner.display_name,
            limit=limit_rounded,
            debt=round(debt, 2),
            amount=round(order_amount, 2),
            total=total_after,
            excess=round(total_after - limit_rounded, 2),
            currency=partner.currency_id.name or self.currency_id.name,
            days=self.env['techdistrib.credit.request']._default_validity_days(),
        )

    # =========================================================================
    #  ПЕРЕВИЗНАЧЕННЯ action_confirm
    # =========================================================================

    def action_confirm(self):
        """Підтвердження замовлення з перевіркою кредитного ліміту.

        ⚠️ ЦЕЙ МЕТОД ВИКЛИКАЄТЬСЯ ЗВІДУСІЛЬ:
          * кнопка «Підтвердити» на формі;
          * масове підтвердження зі списку;
          * через API/імпорт;
          * з іншого коду (напр., підтвердження з веб-порталу).

        Оскільки ми перевизначаємо САМ МЕТОД, а не додаємо кнопку,
        перевірка діє в усіх цих випадках. Якби ми зробили окрему кнопку,
        ліміт можна було б обійти масовим підтвердженням — типова помилка.

        ПРАВИЛО: контроль бізнес-правила став у САМ МЕТОД, а не в UI.
        UI — це лише один зі способів викликати метод.
        """
        # super() викликаємо РІВНО ОДИН раз і лише після перевірки всіх
        # замовлень у наборі. Якщо хоч одне заблоковане — не підтверджуємо
        # жодного. Це важливо для масових операцій: часткове підтвердження
        # заплутало б користувача.
        blocked_orders = self.before_after_confirm_filter()

        if blocked_orders:
            messages = []
            for order in blocked_orders:
                messages.append(
                    '• %s: %s' % (order.name, order.credit_block_reason)
                )
            raise UserError(_(
                'Не вдалося підтвердити замовлення.\n\n%s',
                '\n\n'.join(messages),
            ))

        return super().action_confirm()

    def before_after_confirm_filter(self):
        """Повертає замовлення з набору, які заблоковані кредитною політикою.

        Назва методу обрана так, щоб він читався як «фільтр перед
        підтвердженням». Виносимо логіку в окремий метод, щоб:
          * її можна було перевизначити в іншому модулі;
          * її можна було протестувати окремо;
          * action_confirm лишався коротким і читабельним.
        """
        return self.filtered(lambda order: order._get_credit_block_reason())

    # =========================================================================
    #  РОЗШИРЕННЯ СТАНДАРТНОГО БАНЕРА ПОПЕРЕДЖЕННЯ
    # =========================================================================

    @api.depends(
        'partner_id',
        'amount_total',
        'currency_rate',
        'state',
        'credit_request_ids.state',
        'credit_request_ids.valid_until',
    )
    def _compute_partner_credit_warning(self):
        """Розширюємо стандартне попередження Odoo, не замінюючи його.

        ЯК ЦЕ ПРАЦЮЄ (перевірено в коді ORM):
        Метод _compute_partner_credit_warning оголошений у модулі sale.
        Ми перевизначаємо його тут і кличемо super() — стандартний текст
        зберігається, ми лише ДОПИСУЄМО своє.

        ⚠️ ПРО @api.depends ПРИ ПЕРЕВИЗНАЧЕННІ — неочевидна деталь,
        через яку легко отримати баг «банер не оновлюється».

        Питання: якщо батько має свій @api.depends, а ми додаємо свій —
        вони ЗЛИВАЮТЬСЯ чи наш ЗАМІНЮЄ батьківський?

        Відповідь у коді Odoo (vendor/odoo/odoo/fields.py, метод get_depends):

            # determine the functions implementing self.compute
            if isinstance(self.compute, str):
                funcs = resolve_mro(model, self.compute, callable)
            ...
            for func in funcs:
                deps = getattr(func, '_depends', ())
                depends.extend(deps(model) if callable(deps) else deps)

        resolve_mro збирає ВСІ методи з таким іменем по всьому ланцюжку
        успадкування, і Odoo СКЛАДАЄ їхні залежності.

        Отже: залежності ЗЛИВАЮТЬСЯ, і повторювати батьківські не потрібно.
        Але писати їх усе одно корисно — щоб читач коду бачив повну картину
        і не мусив лізти в ядро Odoo перевіряти.
        """
        super()._compute_partner_credit_warning()

        for order in self:
            # Стандартний метод уже виставив або текст, або порожній рядок.
            # Ми ДОПИСУЄМО свій рядок, якщо є що сказати.
            extra = order._credit_warning_suffix()
            if extra:
                existing = order.partner_credit_warning or ''
                order.partner_credit_warning = (existing + '\n' + extra).strip()

    def _credit_warning_suffix(self):
        """Додатковий текст для банера попередження."""
        self.ensure_one()
        reason = self._get_credit_block_reason()
        if reason:
            return _('⛔ ПІДТВЕРДЖЕННЯ ЗАБЛОКОВАНО: %s', reason.split('\n')[0])

        approval = self._get_valid_credit_approval()
        if approval:
            return _(
                '✅ Діє схвалення %(name)s до %(until)s. '
                'Перевищення: %(excess).2f %(currency)s.',
                name=approval.name,
                until=approval.valid_until,
                excess=approval.excess_amount,
                currency=approval.currency_id.name,
            )
        return False

    # =========================================================================
    #  ДІЇ ДЛЯ КОРИСТУВАЧА
    # =========================================================================

    def action_request_credit_approval(self):
        """Створити запит на перевищення кредитного ліміту.

        ⚠️ ЧОМУ ЦЕ ОКРЕМА КНОПКА, А НЕ АВТОМАТИЧНЕ СТВОРЕННЯ ЗАПИТУ
        ПРИ НЕВДАЛІЙ СПРОБІ ПІДТВЕРДЖЕННЯ:

        Спокусливо зробити так: action_confirm бачить перевищення,
        створює запит і кидає помилку. Але це НЕ ПРАЦЮЄ, і ось чому:

        Коли метод кидає UserError, Odoo ВІДКОЧУЄ ВСЮ ТРАНЗАКЦІЮ.
        Разом із нею зникне і щойно створений запит — користувач побачить
        помилку, а запиту не буде. Класична пастка «дані зникають після
        помилки».

        Тому створення запиту — ОКРЕМА ДІЯ, яку користувач запускає явно.
        Вона завершується успішно, транзакція фіксується, і запит живе.
        """
        self.ensure_one()

        # Захист від дублювання: якщо вже є запит на погодженні,
        # створювати другий не можна — контролер заплутається.
        existing = self.credit_request_ids.filtered(lambda r: r.state == 'pending')
        if existing:
            raise UserError(_(
                'Для замовлення %(order)s вже існує запит на погодженні: '
                '%(request)s.\n\n'
                'Дочекайтесь рішення кредитного контролера.',
                order=self.name,
                request=existing[0].name,
            ))

        debt, order_amount = self._get_credit_exposure()
        partner = self.partner_id.commercial_partner_id.sudo()

        request = self.env['techdistrib.credit.request'].create({
            'partner_id': self.partner_id.id,
            'sale_order_id': self.id,
            'company_id': self.company_id.id,
            # Знімок сум на момент запиту — див. коментар у моделі запиту
            'order_amount': order_amount,
            'credit_limit': partner.credit_limit or 0.0,
            'current_debt': debt,
        })

        self.message_post(body=_(
            'Створено запит на перевищення кредитного ліміту: %s',
            request.name,
        ))
        _logger.info(
            'TechDistrib: створено кредитний запит %s для замовлення %s '
            '(перевищення %.2f)',
            request.name, self.name, request.excess_amount,
        )

        # Відкриваємо створений запит, щоб менеджер одразу міг дописати причину
        return {
            'type': 'ir.actions.act_window',
            'name': _('Запит на кредит'),
            'res_model': 'techdistrib.credit.request',
            'res_id': request.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_view_credit_requests(self):
        """Smart-кнопка «Запити на кредит» на замовленні."""
        self.ensure_one()
        action = {
            'type': 'ir.actions.act_window',
            'name': _('Запити на кредит: %s', self.name),
            'res_model': 'techdistrib.credit.request',
            'view_mode': 'list,form',
            'domain': [('sale_order_id', '=', self.id)],
            'context': {'default_sale_order_id': self.id,
                        'default_partner_id': self.partner_id.id},
        }
        if self.credit_request_count == 1:
            action.update({
                'view_mode': 'form',
                'res_id': self.credit_request_ids.id,
            })
        return action
