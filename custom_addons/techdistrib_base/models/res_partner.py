# -*- coding: utf-8 -*-
# =============================================================================
#  Розширення стандартної моделі res.partner полями дилерської програми
# =============================================================================
#  Це НАСЛІДУВАННЯ (inheritance), а не нова модель.
#
#  Ключова магія рядка `_inherit = 'res.partner'`:
#    * НЕ створюється нова таблиця — усі поля додаються в наявну res_partner;
#    * НЕ треба нічого мігрувати — Odoo сам додасть колонки через ALTER TABLE;
#    * усі стандартні модулі Odoo (продажі, закупівлі, бухгалтерія) одразу
#      бачать наші нові поля, бо працюють із тим самим класом;
#    * можна перевизначати наявні методи, викликаючи оригінал через super().
#
#  Це найпотужніша і найнебезпечніша можливість Odoo одночасно: ти втручаєшся
#  в модель, яку використовують десятки модулів. Тому правило:
#      перевизначаючи метод — майже завжди викликай super().
# =============================================================================

import logging

# relativedelta — «людська» арифметика дат: вміє додавати місяці й роки,
# правильно враховуючи різну кількість днів у місяцях.
# timedelta() так не вміє: додати «6 місяців» ним неможливо.
from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

# _logger — стандартний логер. В Odoo НЕ використовуй print():
# у багатопроцесному режимі він губиться, не має рівнів і не потрапляє у файл.
# Дивись результат у консолі сервера або в data/odoo.log.
_logger = logging.getLogger(__name__)

# Бізнес-константа: скільки місяців без покупок роблять дилера «сплячим».
# Виносимо в константу, а не хардкодимо число в коді: бізнес попросить
# змінити поріг на 9 місяців — це має бути правка в одному рядку.
DORMANT_MONTHS = 6


class ResPartner(models.Model):
    # Ось воно — наслідування. Нова таблиця НЕ створюється.
    _inherit = 'res.partner'

    # _inherit без _name = «розширюємо наявну модель».
    # Якби ми написали ще й _name = 'res.partner', це була б «делегація»
    # (окрема таблиця з копією полів) — нам це не потрібно.
    #
    # ⚠️ ЧОГО ТУТ СВІДОМО НЕМАЄ: ми НЕ перевизначаємо _order.
    #
    # Спокуса велика:
    #     _order = 'dealer_code desc, name'      # ← ТАК РОБИТИ НЕ МОЖНА
    #
    # Чому це погано:
    #   * _order впливає на ВСІ списки партнерів у ВСІЙ системі — не лише
    #     на наш список дилерів, а й на вибір клієнта в замовленні,
    #     на довідник постачальників, на контакти у формах;
    #   * dealer_code порожній (NULL) у переважної більшості партнерів,
    #     тому сортування стане безглуздим і непередбачуваним;
    #   * зламається сортування за релевантністю в автодоповненні.
    #
    # Правильний підхід: задавати order там, де він потрібен — у конкретному
    # action'і (`context="{'default_...'}"`) або через атрибут `default_order`
    # на конкретному <list>. Змінюючи спільну модель — думай про всіх,
    # хто нею користується.

    # --- 1. Ознака дилера та ідентифікація -----------------------------------

    is_dealer = fields.Boolean(
        string='Дилер',
        default=False,
        # index=True створює індекс у Postgres. Для полів, за якими ти
        # ФІЛЬТРУЄШ (а не просто показуєш), індекс обов'язковий.
        index=True,
        # tracking=True означає: зміна значення автоматично пише рядок
        # у chatter картки («Дилер: Ні → Так»). Безкоштовний аудит-лог.
        tracking=True,
        help='Познач, якщо партнер є учасником дилерської програми.',
    )

    dealer_code = fields.Char(
        string='Код дилера',
        # readonly=True — поле заповнює система, а не користувач.
        # Але readonly у UI ≠ захист: через імпорт або API значення все ще
        # можна змінити. Для справжнього захисту потрібне поле з
        # groups= або перевірка в write().
        readonly=True,
        # copy=False КРИТИЧНО ВАЖЛИВО: без нього при дублюванні партнера
        # Odoo скопіює код дилера, і в тебе буде два партнери з однаковим
        # кодом. SQL-обмеження нижче зробить це неможливим — і ти отримаєш
        # незрозумілу помилку при дублюванні. Тому copy=False.
        copy=False,
        index=True,
        help='Унікальний код дилера, присвоюється автоматично (DLR-00001).',
    )

    dealer_tier_id = fields.Many2one(
        comodel_name='techdistrib.dealer.tier',
        string='Рівень партнера',
        # ondelete — що робити, якщо хтось спробує видалити рівень,
        # який використовується. 'restrict' = заборонити видалення.
        # Це захищає від «я видалив Gold, і всі дилери втратили рівень».
        ondelete='restrict',
        tracking=True,
    )

    dealer_since = fields.Date(
        string='Дата початку партнерства',
        tracking=True,
        help='Від цієї дати рахується стаж дилера в партнерській програмі.',
    )

    dealer_experience_months = fields.Integer(
        string='Стаж (міс.)',
        compute='_compute_dealer_experience_months',
        # store=False (типове) означає: значення рахується при кожному
        # читанні й НЕ зберігається в базі. Це правильно для величин, які
        # залежать від СЬОГОДНІШНЬОЇ дати: якби ми зберегли «стаж 24 міс.»,
        # завтра він став би неправдою, і жоден сигнал не сповістив би Odoo.
        help='Скільки місяців партнер є дилером. Рахується від поточної дати.',
    )

    # --- 2. Метрики продажів --------------------------------------------------

    last_sale_date = fields.Date(
        string='Остання продажа',
        compute='_compute_dealer_stored_metrics',
        # store=True — значення ЗБЕРІГАЄТЬСЯ в колонці res_partner.last_sale_date.
        # Це потрібно, бо ми хочемо шукати по ньому доменом:
        #     [('last_sale_date', '<', '2024-01-01')]
        # Для НЕзбережених computed-полів такий пошук працює інакше
        # (потрібен окремий метод search=), а часто взагалі неможливий.
        #
        # ⚠️ РЕАЛЬНА ПОМИЛКА, ЯКУ Я ДОПУСТИВ У ЦЬОМУ ФАЙЛІ:
        # спочатку я написав цей коментар про store=True, але САМ ПАРАМЕТР
        # store=True не додав. Модуль встановився без єдиної помилки.
        # Наслідок був би такий: поле існує, у формі показується, але
        # колонки в базі немає → пошук `[('last_sale_date','<',...)]`
        # у cron-задачі падає з помилкою «Non-stored field cannot be searched».
        #
        # Мораль: уважно перевіряй, що параметри поля відповідають коментарям.
        # Найшвидша перевірка — зазирнути в базу:
        #   SELECT column_name FROM information_schema.columns
        #    WHERE table_name='res_partner';
        # Або в Odoo: Technical → Fields → вибрати модель → колонка «Stored».
        store=True,
        readonly=True,
    )

    dealer_confirmed_sales = fields.Monetary(
        string='Сума продажів',
        compute='_compute_dealer_stored_metrics',
        store=True,
        # currency_field вказує, звідки брати валюту для форматування.
        # Ми використовуємо стандартне res.partner.currency_id. Чому воно
        # завжди заповнене? Бо модуль account (який входить у наші залежності
        # через sale → account_payment → account) перевизначає це поле як
        # обчислюване з відкатом на валюту компанії.
        currency_field='currency_id',
        readonly=True,
    )

    dealer_confirmed_order_count = fields.Integer(
        string='Підтверджених замовлень',
        compute='_compute_dealer_live_metrics',
    )

    dealer_open_order_count = fields.Integer(
        string='Відкритих замовлень',
        compute='_compute_dealer_live_metrics',
    )

    # --- 3. Статус дилера -----------------------------------------------------

    dealer_status = fields.Selection(
        selection=[
            ('new', 'Новий'),
            ('active', 'Активний'),
            ('dormant', 'Сплячий'),
        ],
        string='Статус дилера',
        compute='_compute_dealer_status',
        store=True,
        index=True,
        help='Новий — ще не було жодної продажі.\n'
             'Активний — купував протягом останніх 6 місяців.\n'
             'Сплячий — не купував понад 6 місяців.',
    )

    # --- SQL-обмеження --------------------------------------------------------
    # Унікальність коду дилера гарантує САМА БАЗА, а не Python.
    # Це важливо: два менеджери, які одночасно створюють дилерів у двох
    # вкладках, отримають помилку від Postgres — а Python-перевірка
    # (@api.constrains) у такій гонці може пропустити дубль.
    #
    # Примітка про NULL: в SQL два NULL вважаються різними, тому звичайні
    # партнери (dealer_code = NULL) не конфліктують між собою. Саме тому
    # copy=False вище + це обмеження = надійна пара.
    _sql_constraints = [
        ('dealer_code_uniq', 'unique(dealer_code)',
         'Код дилера має бути унікальним! Схоже, партнера було скопійовано.'),
    ]

    # =========================================================================
    #  ОБЧИСЛЮВАНІ ПОЛЯ
    # =========================================================================

    @api.depends('dealer_since')
    def _compute_dealer_experience_months(self):
        """Стаж дилера в місяцях від дати початку партнерства."""
        today = fields.Date.context_today(self)
        for partner in self:
            # Завжди присвоюй значення КОЖНОМУ запису в self — навіть якщо
            # це 0 або False. Пропущений запис = помилка
            # "Compute method failed to assign".
            if not partner.dealer_since:
                partner.dealer_experience_months = 0
                continue
            delta = relativedelta(today, partner.dealer_since)
            partner.dealer_experience_months = delta.years * 12 + delta.months

    # ⚠️ ЧОМУ ТУТ ДВА МЕТОДИ, А НЕ ОДИН (реальна історія цього файлу)
    #
    # Спочатку я написав ОДИН метод, який обчислював усі чотири поля одразу:
    # два зі store=True (last_sale_date, dealer_confirmed_sales) і два без
    # store (лічильники замовлень). Модуль встановився — але Odoo видав
    # попередження:
    #
    #   UserWarning: res.partner: inconsistent 'store' for computed fields,
    #   accessing dealer_confirmed_order_count may recompute and update
    #   last_sale_date, dealer_confirmed_sales.
    #
    # ЧОМУ ЦЕ НЕБЕЗПЕЧНО:
    #   * збережені поля перераховуються ЛИШЕ тоді, коли змінюється їхня
    #     залежність (Odoo веде граф залежностей);
    #   * незбережені перераховуються при КОЖНОМУ читанні;
    #   * якщо вони в одному методі, то кожне читання лічильника (напр.,
    #     при відкритті списку) виконує метод, який ПЕРЕЗАПИСУЄ і збережені
    #     поля. Так з'являються несподівані UPDATE-и в базі під час
    #     звичайного перегляду даних, а в гіршому разі — конфлікти записів.
    #
    # ПРАВИЛО: один метод-обчислювач — однаковий режим збереження для всіх
    # його полів. Odoo прямо радить це у попередженні: «Use distinct compute
    # methods for stored and non-stored fields».
    #
    # Така сама історія з compute_sudo: якщо частина полів у методі має
    # compute_sudo=True, а частина — ні, Odoo не може вибрати режим і теж
    # попереджає. Розділення методів лікує обидві проблеми.

    @api.depends(
        # Залежності вказуються по «ланцюжку» через крапку:
        # зміниться стан, дата або сума будь-якого замовлення партнера —
        # Odoo автоматично перерахує поля цього методу.
        'sale_order_ids.state',
        'sale_order_ids.date_order',
        'sale_order_ids.amount_untaxed',
    )
    def _compute_dealer_stored_metrics(self):
        """ЗБЕРЕЖЕНІ метрики: остання продажа і сума обороту.

        store=True тут потрібен, бо по last_sale_date ми ШУКАЄМО доменом
        (його використовує cron-задача).
        """
        for partner in self:
            # filtered() — метод recordset'а. Виконується в пам'яті над уже
            # завантаженими записами і НЕ робить окремий SQL-запит на кожен
            # елемент (Odoo підвантажує sale_order_ids одним запитом).
            confirmed = partner.sale_order_ids.filtered(lambda o: o.state == 'sale')

            # ⚠️ ЧОМУ amount_untaxed, А НЕ amount_total — реальний урок із тестів.
            #
            # Спочатку я використав amount_total і тест упав:
            #     AssertionError: 4025.0 != 3500.0
            # Різниця 525.0 — це ПДВ 15 %: 3500 × 1.15 = 4025.
            #
            # Для ОБОРОТУ ДИЛЕРА правильна сума — БЕЗ ПДВ, і ось чому:
            #   * ПДВ не є виручкою компанії: вона його збирає для держави
            #     і передає далі. У звіті про прибутки це не дохід;
            #   * ретро-бонуси вендора рахуються від чистої суми угоди;
            #   * пороги рівнів партнера (min_annual_revenue) — теж чистий
            #     оборот, інакше дилер, який продає товари з різною ставкою
            #     ПДВ, мав би несправедливу перевагу.
            #
            # А ось для КРЕДИТНОГО ЛІМІТУ (наступний модуль) ситуація
            # протилежна: там потрібна сума ДО СПЛАТИ, тобто amount_total —
            # бо борг клієнта перед нами включає ПДВ.
            #
            # Висновок: немає «правильного» поля взагалі — є правильне поле
            # для конкретної бізнес-задачі. Уточнюй у бізнесу.
            partner.dealer_confirmed_sales = sum(confirmed.mapped('amount_untaxed'))

            # date_order — це Datetime. У полі Date зберігаємо лише дату.
            # Фільтруємо порожні значення: max() на наборі з False впаде.
            dates = [d for d in confirmed.mapped('date_order') if d]
            partner.last_sale_date = max(dates).date() if dates else False

    @api.depends('sale_order_ids.state')
    def _compute_dealer_live_metrics(self):
        """НЕзбережені лічильники замовлень.

        Рахуються на льоту при кожному читанні. Це свідомий вибір:
        лічильники показуються лише на картці та в smart-кнопці, по них
        ніхто не шукає, а зберігати їх означало б тримати зайвий індекс
        і ризикувати розсинхроном даних.
        """
        for partner in self:
            orders = partner.sale_order_ids
            partner.dealer_confirmed_order_count = len(
                orders.filtered(lambda o: o.state == 'sale')
            )
            partner.dealer_open_order_count = len(
                orders.filtered(lambda o: o.state in ('draft', 'sent'))
            )

    @api.depends('is_dealer', 'dealer_since', 'last_sale_date')
    def _compute_dealer_status(self):
        """Статус дилера: новий / активний / сплячий."""
        today = fields.Date.context_today(self)
        dormant_threshold = today - relativedelta(months=DORMANT_MONTHS)

        for partner in self:
            if not partner.is_dealer:
                # Для недилерів статус не має сенсу. False (а не '') —
                # правильне «порожнє» значення для Selection.
                partner.dealer_status = False
            elif partner.last_sale_date and partner.last_sale_date >= dormant_threshold:
                partner.dealer_status = 'active'
            elif not partner.last_sale_date:
                # Жодної продажі ще не було. Поки дилер «молодий» — він новий,
                # але якщо партнерство триває довше за поріг — уже сплячий.
                if partner.dealer_since and partner.dealer_since < dormant_threshold:
                    partner.dealer_status = 'dormant'
                else:
                    partner.dealer_status = 'new'
            else:
                partner.dealer_status = 'dormant'

    # =========================================================================
    #  ПЕРЕВИЗНАЧЕННЯ СТАНДАРТНИХ МЕТОДІВ (create / write)
    # =========================================================================

    # В Odoo 13+ створення записів іде через create() зі СПИСКОМ словників.
    # Декоратор @api.model_create_multi — вимога сучасного API: він дозволяє
    # Odoo створювати багато записів одним запитом (швидше, менше транзакцій).
    #
    # Якщо написати старий `def create(self, vals)` — модуль встановиться,
    # але при масовому створенні (імпорт, дублювання) буде помилка.
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('is_dealer'):
                if not vals.get('dealer_code'):
                    vals['dealer_code'] = self._next_dealer_code()
                if not vals.get('dealer_since'):
                    vals['dealer_since'] = fields.Date.context_today(self)
        return super().create(vals_list)

    def write(self, vals):
        # Сценарій: партнер існував як звичайний клієнт, і менеджер поставив
        # галочку «Дилер». Треба присвоїти код, якого в нього ще немає.
        if vals.get('is_dealer') and not vals.get('dealer_code'):
            for partner in self:
                if not partner.dealer_code:
                    partner.dealer_code = self._next_dealer_code()
                if not partner.dealer_since:
                    partner.dealer_since = fields.Date.context_today(self)

        # super() ОБОВ'ЯЗКОВИЙ: без нього стандартна логіка Odoo (запис у базу,
        # зв'язки, відправка повідомлень) не виконається, і метод «нічого не
        # зробить» — найважче для діагностики.
        return super().write(vals)

    @api.model
    def _next_dealer_code(self):
        """Взяти наступний код із послідовності.

        next_by_code — атомарна операція на рівні бази: навіть якщо два
        процеси звернуться одночасно, вони отримають РІЗНІ номери.
        Ніколи не рахуй номер як `search_count() + 1` — це класична гонка.
        """
        return self.env['ir.sequence'].next_by_code('techdistrib.dealer') or '/'

    # =========================================================================
    #  ПЕРЕВІРКИ
    # =========================================================================

    @api.constrains('is_dealer', 'dealer_tier_id')
    def _check_dealer_tier_consistency(self):
        """Рівень партнера можна вказати лише для дилерів."""
        for partner in self:
            if partner.dealer_tier_id and not partner.is_dealer:
                raise ValidationError(_(
                    'Партнер «%s» має рівень «%s», але не позначений як дилер.\n'
                    'Поставте галочку «Дилер» або приберіть рівень.',
                    partner.display_name,
                    partner.dealer_tier_id.name,
                ))

    # =========================================================================
    #  ДІЇ ДЛЯ ІНТЕРФЕЙСУ
    # =========================================================================

    def action_make_dealer(self):
        """Кнопка «Зробити дилером» на картці партнера."""
        self.ensure_one()
        self.write({'is_dealer': True})
        # Повертаємо дію перезавантаження форми, щоб користувач одразу
        # побачив новий код дилера і вкладку — інакше форма лишиться старою.
        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }

    def action_view_dealer_orders(self):
        """Smart-кнопка «Замовлення» — перехід до замовлень дилера."""
        self.ensure_one()
        # child_of + commercial_partner_id: замовлення може бути оформлене як
        # на контактну особу, так і на компанію. child_of збирає всі дочірні
        # контакти — тому менеджер побачить повну картину, а не частину.
        return {
            'type': 'ir.actions.act_window',
            'name': _('Замовлення дилера %s', self.display_name),
            'res_model': 'sale.order',
            'view_mode': 'list,form',
            'domain': [('partner_id', 'child_of', self.commercial_partner_id.id)],
            'context': {'default_partner_id': self.id},
        }

    # =========================================================================
    #  АВТОМАТИЗАЦІЯ (cron)
    # =========================================================================

    @api.model
    def _cron_update_dealer_status(self):
        """Нічна задача: оновити статуси дилерів і знизити рівень сплячим.

        НАВІЩО ЦЕ ПОТРІБНО, якщо є computed-поле?
        Тому що computed-поле перераховується лише тоді, коли змінюється
        його ЗАЛЕЖНІСТЬ. А статус дилера залежить ще й від СЬОГОДНІШНЬОЇ ДАТИ,
        яка ніде не зберігається і не може бути залежністю.
        Отже, без cron запис, що вчора був «активним», сьогодні залишиться
        «активним» назавжди, бо нічого не змінилося.

        Це фундаментальна проблема часових залежностей. Стандартне рішення
        Odoo — саме такий cron. Подивись, як це зроблено в модулі membership:
        addons/membership/models/partner.py, метод _cron_update_membership.
        """
        dealers = self.search([('is_dealer', '=', True)])
        if not dealers:
            return

        # add_to_compute — офіційний спосіб сказати ORM: «познач ці записи
        # для перерахунку поля». Після цього Odoo перерахує його сам
        # і запише результат у базу (бо поле stored).
        #
        # ЧОМУ НЕ `partner._compute_dealer_status()` напряму: прямий виклик
        # обчислювача оминає систему кешування й залежностей ORM, і може
        # призвести до неузгодженого стану кеша.
        self.env.add_to_compute(self._fields['dealer_status'], dealers)

        # --- Зниження рівня сплячим дилерам (FR-A07) --------------------------
        today = fields.Date.context_today(self)
        threshold = today - relativedelta(months=DORMANT_MONTHS)

        # Домен: дилер, у якого є рівень, і який або ніколи не купував,
        # або остання покупка була давно.
        # Оператор '|' застосовується до НАСТУПНИХ ДВОХ умов у списку —
        # саме тому він стоїть перед ними, а не між ними.
        dormant = self.search([
            ('is_dealer', '=', True),
            ('dealer_tier_id', '!=', False),
            ('dealer_since', '!=', False),
            ('dealer_since', '<', threshold),
            '|',
            ('last_sale_date', '=', False),
            ('last_sale_date', '<', threshold),
        ])

        if not dormant:
            _logger.info('TechDistrib: сплячих дилерів для зниження рівня немає.')
            return

        # Всі рівні, відсортовані за зростанням sequence (від Bronze до Platinum)
        tiers = self.env['techdistrib.dealer.tier'].search([], order='sequence')

        downgraded = 0
        for partner in dormant:
            current = partner.dealer_tier_id
            # Усі рівні, що НИЖЧЕ поточного
            lower_tiers = tiers.filtered(lambda t: t.sequence < current.sequence)
            if not lower_tiers:
                # Дилер уже на найнижчому рівні — знижувати нікуди.
                continue

            # [-1] — останній елемент, тобто НАЙВИЩИЙ із нижчих.
            # Знижуємо рівно на один щабель за один запуск, а не одразу вниз.
            new_tier = lower_tiers[-1]
            partner.write({'dealer_tier_id': new_tier.id})

            # message_post пише в chatter. Це не «логування для галочки»:
            # менеджер бачить на картці, чому дилер втратив рівень.
            partner.message_post(body=_(
                'Рівень дилера автоматично знижено: %(old)s → %(new)s.\n'
                'Причина: немає підтверджених продажів понад %(months)s місяців '
                '(остання продажа: %(date)s).',
                old=current.name,
                new=new_tier.name,
                months=DORMANT_MONTHS,
                date=partner.last_sale_date or _('ніколи'),
            ))
            downgraded += 1

        _logger.info(
            'TechDistrib: знижено рівень %s дилерам(ів) через %s місяців без покупок.',
            downgraded, DORMANT_MONTHS,
        )
