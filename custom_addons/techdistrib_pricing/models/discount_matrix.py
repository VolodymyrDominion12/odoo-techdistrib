# -*- coding: utf-8 -*-
# =============================================================================
#  Модель: techdistrib.discount.matrix — матриця дилерських знижок
# =============================================================================
#  Це «зручний фасад» для бізнесу. У базі даних реальна ціна формується
#  правилами product.pricelist.item, але показувати менеджеру таблицю
#  з 40 рядків правил прайс-листа — поганий UX.
#
#  Тому ми робимо просту таблицю «рівень × категорія → %» і АВТОМАТИЧНО
#  перетворюємо її на правила прайс-листа.
#
#  Це класичний архітектурний патерн: ЗРУЧНЕ ПОДАННЯ ДЛЯ КОРИСТУВАЧА
#  + СТАНДАРТНИЙ МЕХАНІЗМ ПІД КАПОТОМ.
#
#  Альтернатива (перевизначити розрахунок ціни в sale.order.line) гірша:
#    * довелося б дублювати логіку Odoo (податки, валюти, округлення);
#    * зламались би звіти, що беруть ціни з прайс-листів;
#    * будь-яке оновлення Odoo ламало б наш код.
# =============================================================================

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class TechdistribDiscountMatrix(models.Model):
    _name = 'techdistrib.discount.matrix'
    _description = 'Матриця дилерських знижок'
    _order = 'tier_id, categ_id'
    _rec_name = 'display_name'

    tier_id = fields.Many2one(
        comodel_name='techdistrib.dealer.tier',
        string='Рівень партнера',
        required=True,
        # ondelete='cascade': якщо рівень видалили — матриця для нього
        # більше не потрібна. Це логічно: матриця не має сенсу без рівня.
        ondelete='cascade',
        index=True,
    )

    categ_id = fields.Many2one(
        comodel_name='product.category',
        string='Категорія товару',
        required=True,
        ondelete='cascade',
        index=True,
    )

    discount = fields.Float(
        string='Знижка, %',
        required=True,
        default=0.0,
        digits=(16, 2),
        help='Відсоток знижки від базової ціни продажу. '
             'Можна вказати відʼємне значення — це буде націнка.',
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
        store=True,
        readonly=True,
    )

    # Зворотний зв'язок: яке правило прайс-листа обслуговує цей рядок.
    # Зберігаємо посилання, щоб при зміні рядка ОНОВЛЮВАТИ правило,
    # а не створювати нове (інакше прайс-лист заросте дублями).
    pricelist_item_id = fields.Many2one(
        comodel_name='product.pricelist.item',
        string='Правило прайс-листа',
        readonly=True,
        copy=False,
        # ondelete='set null': якщо хтось видалив правило вручну —
        # рядок матриці лишається живим, і його можна перегенерувати
        # кнопкою «Перебудувати прайс-лист».
        ondelete='set null',
    )

    display_name = fields.Char(
        string='Назва',
        compute='_compute_display_name',
        # store=True потрібен, щоб Odoo міг використовувати це поле
        # в name_search (автодоповнення при виборі запису у Many2one).
        store=True,
    )

    # =========================================================================
    #  ОБМЕЖЕННЯ
    # =========================================================================

    _sql_constraints = [
        # Один рядок на пару «рівень + категорія». Інакше в прайс-листі
        # з'являться два правила для однієї категорії, і Odoo застосує
        # випадкове з них — класична «плаваюча» ціна, яку важко зловити.
        ('tier_categ_uniq', 'unique(tier_id, categ_id, company_id)',
         'Для цього рівня вже є рядок матриці для цієї категорії!'),
    ]

    @api.constrains('discount')
    def _check_discount_reasonable(self):
        """Захист від очевидних помилок введення."""
        for row in self:
            # Знижка понад 90 % — майже завжди помилка (забули кому,
            # або ввели 50 замість 5.0). Ціна, близька до нуля, зруйнує маржу,
            # і виявити це в замовленні буде важко.
            if row.discount >= 90:
                raise ValidationError(_(
                    'Знижка %(discount)s%% для категорії «%(categ)s» виглядає '
                    'як помилка введення.\n\n'
                    'Максимально дозволена знижка — 90 %%. '
                    'Якщо це справді потрібно, змініть цю перевірку в коді.',
                    discount=row.discount,
                    categ=row.categ_id.display_name,
                ))

    @api.depends('tier_id', 'categ_id', 'discount')
    def _compute_display_name(self):
        for row in self:
            if row.tier_id and row.categ_id:
                row.display_name = '%s / %s: %s%%' % (
                    row.tier_id.name, row.categ_id.display_name, row.discount,
                )
            else:
                row.display_name = _('Новий рядок матриці')

    # =========================================================================
    #  СИНХРОНІЗАЦІЯ З ПРАЙС-ЛИСТОМ
    # =========================================================================

    def _prepare_pricelist_item_vals(self):
        """Підготувати значення правила прайс-листа з рядка матриці.

        Розберемо кожен ключ, бо це ядро всієї магії:

        applied_on='2_product_category'
            Правило діє на всю КАТЕГОРІЮ товарів. Саме тому матриця
            будується по категоріях: 40 рядків замість 1200 SKU.

        compute_price='percentage'
            Це ДУЖЕ важливий вибір. Є три варіанти:
              'fixed'      — фіксована ціна (не підходить: ціни змінюються);
              'formula'    — формула від базової ціни;
              'percentage' — відсоткова знижка.

            Саме 'percentage' дає ту поведінку, якої вимагає бізнес:
            Odoo підставить у рядок замовлення БАЗОВУ ціну і покаже
            ЗНИЖКУ окремим полем. Менеджер бачить «16 %» і може змінити,
            а керівник — проконтролювати. З 'formula' знижка була б
            «захована» в ціні, і контролювати її було б неможливо.

        base='list_price'
            База розрахунку — «Ціна продажу» товару (product.list_price).
            Альтернативи: 'standard_price' (собівартість) і 'pricelist'.
            Для дилерських знижок правильно саме list_price: дилерська
            ціна — це знижка від рекомендованої роздрібної.

        percent_price
            Власне відсоток.

        min_quantity=0
            Правило діє за будь-якої кількості. Можна було б зробити
            «знижка більша при обсязі» — але це вже інша вимога.
        """
        self.ensure_one()
        pricelist = self.tier_id._ensure_pricelist()
        return {
            'pricelist_id': pricelist.id,
            'applied_on': '2_product_category',
            'categ_id': self.categ_id.id,
            'compute_price': 'percentage',
            'base': 'list_price',
            'percent_price': self.discount,
            'min_quantity': 0,
            'company_id': self.company_id.id,
        }

    def _sync_pricelist_item(self, force=False):
        """Створити або оновити правило прайс-листа для рядків матриці.

        :param force: якщо True — перестворити правило навіть коли
                      посилання вже є (використовується кнопкою
                      «Перебудувати прайс-лист»).
        """
        PricelistItem = self.env['product.pricelist.item']
        for row in self:
            vals = row._prepare_pricelist_item_vals()

            if row.pricelist_item_id and not force:
                # Оновлюємо наявне правило — так ми не плодимо дублі
                # і не втрачаємо налаштування, яких немає в нашій матриці.
                row.pricelist_item_id.write(vals)
            else:
                if row.pricelist_item_id and force:
                    # force: спершу прибираємо старе правило
                    row.pricelist_item_id.unlink()
                row.pricelist_item_id = PricelistItem.create(vals)

    # =========================================================================
    #  ПЕРЕВИЗНАЧЕННЯ CRUD
    # =========================================================================

    @api.model_create_multi
    def create(self, vals_list):
        rows = super().create(vals_list)
        # Синхронізуємо ПІСЛЯ створення: до цього моменту в запису
        # немає id, а він потрібен, щоб записати pricelist_item_id.
        rows._sync_pricelist_item()
        return rows

    def write(self, vals):
        result = super().write(vals)
        # Перераховуємо правило, якщо змінилось щось, що впливає на ціну.
        # Перевіряємо конкретні поля, а не синхронізуємо завжди: зайві
        # UPDATE-и в прайс-листі уповільнюють роботу без потреби.
        if {'discount', 'categ_id', 'tier_id', 'company_id'} & set(vals):
            self._sync_pricelist_item()
        return result

    def unlink(self):
        # ⚠️ ПОРЯДОК ВАЖЛИВИЙ: спершу прибираємо правила прайс-листа,
        # потім самі рядки матриці.
        #
        # Чому: якщо видалити матрицю, а правило лишити — у прайс-листі
        # зависне «осиротіле» правило, яке продовжить давати знижку.
        # Причому зробить це ТИХО: менеджер бачить знижку, а в матриці
        # її вже немає, і ніхто не розуміє, звідки вона.
        self.mapped('pricelist_item_id').unlink()
        return super().unlink()
