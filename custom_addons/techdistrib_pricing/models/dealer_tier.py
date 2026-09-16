# -*- coding: utf-8 -*-
# =============================================================================
#  Розширення techdistrib.dealer.tier: прайс-лист і ліміт ручної знижки
# =============================================================================
#  Ідея: кожен РІВЕНЬ партнера має власний прайс-лист Odoo.
#  Дилер отримує прайс-лист свого рівня — і стандартний механізм Odoo
#  сам рахує ціни. Ми не пишемо жодного рядка розрахунку ціни.
# =============================================================================

from odoo import api, fields, models, _


class TechdistribDealerTier(models.Model):
    _inherit = 'techdistrib.dealer.tier'

    pricelist_id = fields.Many2one(
        comodel_name='product.pricelist',
        string='Прайс-лист',
        # readonly=True — прайс-лист створюється й обслуговується системою.
        # Користувач не має його «перепризначати»: інакше матриця знижок
        # перестане відповідати реальним цінам, і ніхто цього не помітить.
        readonly=True,
        copy=False,
        ondelete='restrict',
        help='Створюється автоматично. Правила прайс-листа формуються '
             'з матриці знижок цього рівня.',
    )

    # FR-B04/B05: скільки відсотків менеджер може додати РУЧНО,
    # понад знижку, яку вже дає прайс-лист рівня.
    max_manual_discount = fields.Float(
        string='Макс. ручна знижка, %',
        default=0.0,
        digits=(16, 2),
        help='Максимальна ДОДАТКОВА знижка, яку менеджер із продажу може '
             'надати вручну понад знижку рівня. Перевищення потребує '
             'погодження керівника.',
    )

    matrix_ids = fields.One2many(
        comodel_name='techdistrib.discount.matrix',
        inverse_name='tier_id',
        string='Матриця знижок',
    )

    matrix_count = fields.Integer(
        string='Категорій у матриці',
        compute='_compute_matrix_count',
    )

    @api.depends('matrix_ids')
    def _compute_matrix_count(self):
        for tier in self:
            tier.matrix_count = len(tier.matrix_ids)

    # =========================================================================
    #  АВТОМАТИЧНЕ СТВОРЕННЯ ПРАЙС-ЛИСТА
    # =========================================================================

    def _ensure_pricelist(self):
        """Повернути прайс-лист рівня, створивши його за потреби.

        Метод ідемпотентний: скільки разів не виклич — прайс-лист буде один.
        Це важливо, бо метод викликається і з create матриці, і з write
        партнера, і з даних модуля.
        """
        self.ensure_one()
        if self.pricelist_id:
            return self.pricelist_id

        pricelist = self.env['product.pricelist'].create({
            'name': _('TechDistrib %s', self.name),
            'company_id': self.env.company.id,
            'currency_id': self.env.company.currency_id.id,
            # active=True — прайс-лист має бути активним, інакше Odoo
            # не застосує його до замовлень.
            'active': True,
        })
        # Присвоєння через запис, а не через create: поле є в цій же моделі,
        # тому простий write — найпряміший спосіб.
        self.pricelist_id = pricelist
        return pricelist

    # =========================================================================
    #  ДІЇ ДЛЯ ІНТЕРФЕЙСУ
    # =========================================================================

    def action_open_matrix(self):
        """Кнопка «Матриця знижок» на формі рівня."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Матриця знижок: %s', self.name),
            'res_model': 'techdistrib.discount.matrix',
            'view_mode': 'list,form',
            'domain': [('tier_id', '=', self.id)],
            'context': {'default_tier_id': self.id},
        }

    def action_open_pricelist(self):
        """Перейти до згенерованого прайс-листа (щоб переконатись, що магія працює)."""
        self.ensure_one()
        pricelist = self._ensure_pricelist()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Прайс-лист %s', pricelist.name),
            'res_model': 'product.pricelist',
            'res_id': pricelist.id,
            'view_mode': 'form',
        }

    def action_regenerate_pricelist(self):
        """Примусово перебудувати правила прайс-листа з матриці.

        Навіщо потрібна ця кнопка, якщо синхронізація автоматична?
        Бо існують стани, які синхронізація не бачить:
          * хтось вручну виправив правило прямо в прайс-листі;
          * правила «загубились» через помилку або ручне видалення;
          * після імпорту даних матриця й прайс-лист розійшлись.

        Кнопка-«рятівне коло» — обов'язковий елемент будь-якої
        автоматичної синхронізації. Людина має мати спосіб сказати
        «перебудуй усе з джерела істини».
        """
        self.ensure_one()
        self.matrix_ids._sync_pricelist_item(force=True)
        return True

    # =========================================================================
    #  ТИПОВІ ЗНАЧЕННЯ ЛІМІТІВ РУЧНИХ ЗНИЖОК
    # =========================================================================

    @api.model
    def _setup_default_discount_limits(self):
        """Проставити ліміти ручних знижок за кодом рівня.

        ⚠️ ЧОМУ ЦЕ ОКРЕМИЙ МЕТОД, А НЕ ЗВИЧАЙНИЙ ЗАПИС У XML:

        Рівні (Bronze/Silver/Gold/Platinum) створює модуль techdistrib_base
        з атрибутом noupdate="1". Це означає, що Odoo БІЛЬШЕ НЕ ОНОВЛЮЄ
        ці записи з data-файлів — щоб правки бізнесу не затирались.

        Отже, звичайний <record id="techdistrib_base.dealer_tier_gold"> з
        полем max_manual_discount у нашому модулі ПРОСТО НЕ ЗАСТОСУЄТЬСЯ:
        Odoo пропустить оновлення, бо запис позначений як noupdate.

        Тому ми викликаємо метод із data-файлу через <function>.
        Метод виконується ПОЗА механізмом noupdate й може встановити
        значення там, де це справді потрібно.

        Сам метод ідемпотентний: він змінює ліміт лише якщо той ще нульовий.
        Повторне встановлення модуля НЕ загубить налаштувань бізнесу.
        """
        default_limits = {
            'bronze': 0.0,
            'silver': 2.0,
            'gold': 5.0,
            'platinum': 10.0,
        }
        for code, limit in default_limits.items():
            tier = self.search([('code', '=', code)], limit=1)
            # Ставимо лише якщо ліміт ще не налаштований (0) — щоб не
            # перезаписати свідомий вибір адміністратора.
            if tier and not tier.max_manual_discount:
                tier.max_manual_discount = limit
        return True
