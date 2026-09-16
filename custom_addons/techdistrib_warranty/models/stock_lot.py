# -*- coding: utf-8 -*-
# =============================================================================
#  Розширення stock.lot: гарантійний «паспорт» серійного номера
# =============================================================================
#  stock.lot — це модель серійного номера (або партії) в Odoo.
#  Ми додаємо до неї гарантійні дати та історію звернень.
#
#  Це і є той самий «ланцюжок доказів»: усе, що потрібно довести вендору,
#  збирається навколо серійного номера.
# =============================================================================

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _


class StockLot(models.Model):
    _inherit = 'stock.lot'

    # =========================================================================
    #  ГАРАНТІЙНІ ДАТИ
    # =========================================================================

    warranty_start_date = fields.Date(
        string='Початок гарантії',
        readonly=True,
        copy=False,
        # index=True — по цьому полю шукаємо (у звітах, у фільтрах).
        index=True,
        help='Проставляється АВТОМАТИЧНО при відвантаженні клієнту.',
    )

    warranty_end_date = fields.Date(
        string='Кінець гарантії',
        readonly=True,
        copy=False,
        index=True,
        help='Початок гарантії + термін категорії товару.',
    )

    warranty_months_applied = fields.Integer(
        string='Гарантія, міс.',
        readonly=True,
        copy=False,
        help='Термін, який було застосовано на момент відвантаження. '
             'Зберігаємо окремо, бо бізнес може змінити термін категорії '
             'пізніше — і тоді для ВЖЕ ПРОДАНИХ одиниць гарантія не має '
             'перераховуватись.',
    )

    # =========================================================================
    #  ХТО КУПИВ
    # =========================================================================

    # У Odoo вже є:
    #   last_delivery_partner_id — партнер останнього відвантаження (computed)
    #   delivery_ids             — усі переміщення цього номера (computed)
    # Ми НЕ дублюємо їх, а використовуємо. Це принципово: менше полів —
    # менше розсинхрону.
    warranty_customer_id = fields.Many2one(
        comodel_name='res.partner',
        string='Продано (клієнт)',
        readonly=True,
        copy=False,
        help='Кому ми відвантажили цей серійний номер.',
    )

    warranty_sale_order_id = fields.Many2one(
        comodel_name='sale.order',
        string='Замовлення продажу',
        readonly=True,
        copy=False,
        help='Наше замовлення, за яким номер пішов клієнту.',
    )

    # =========================================================================
    #  СТАН ГАРАНТІЇ
    # =========================================================================

    warranty_state = fields.Selection(
        selection=[
            ('unknown', 'Не продано'),
            ('under', 'Гарантія дійсна'),
            ('expired', 'Гарантія закінчилась'),
        ],
        string='Стан гарантії',
        compute='_compute_warranty_state',
        help='Рахується від поточної дати, тому не зберігається.',
    )

    warranty_days_left = fields.Integer(
        string='Днів до кінця гарантії',
        compute='_compute_warranty_state',
    )

    warranty_claim_ids = fields.One2many(
        comodel_name='techdistrib.warranty.claim',
        inverse_name='lot_id',
        string='Гарантійні звернення',
    )

    warranty_claim_count = fields.Integer(
        string='Звернень',
        compute='_compute_warranty_claim_count',
    )

    # =========================================================================
    #  ОБЧИСЛЕННЯ
    # =========================================================================

    @api.depends('warranty_end_date')
    def _compute_warranty_state(self):
        """Стан гарантії на СЬОГОДНІ.

        ⚠️ Поле НЕ зберігається, і це свідомий вибір.
        Воно залежить від поточної дати, а та не є полем у базі.
        Якби ми зробили store=True, значення «протухало» б наступного
        дня, і жоден сигнал не сповістив би Odoo (це та сама проблема
        часових залежностей, що й у Модулі 1).

        Наслідок: по цьому полю НЕ МОЖНА фільтрувати доменом
        (Odoo мовчки проігнорує умову — див. Модуль 2, розділ 6).
        Тому в пошуковому поданні ми фільтруємо по ЗБЕРЕЖЕНОМУ полю
        warranty_end_date із виразом context_today() у домені.
        """
        today = fields.Date.context_today(self)
        for lot in self:
            if not lot.warranty_end_date:
                lot.warranty_state = 'unknown'
                lot.warranty_days_left = 0
            elif lot.warranty_end_date >= today:
                lot.warranty_state = 'under'
                lot.warranty_days_left = (lot.warranty_end_date - today).days
            else:
                lot.warranty_state = 'expired'
                lot.warranty_days_left = (lot.warranty_end_date - today).days

    @api.depends('warranty_claim_ids')
    def _compute_warranty_claim_count(self):
        for lot in self:
            lot.warranty_claim_count = len(lot.warranty_claim_ids)

    # =========================================================================
    #  ДОПОМІЖНІ МЕТОДИ
    # =========================================================================

    def _get_warranty_months(self):
        """Термін гарантії для цього номера.

        Пріоритет (від конкретного до загального):
          1. перевизначення на товарі (product.template.warranty_months);
          2. термін категорії товару (product.category.warranty_months).
        """
        self.ensure_one()
        if not self.product_id:
            return 0
        if self.product_id.warranty_months:
            return self.product_id.warranty_months
        return self.product_id.categ_id.warranty_months or 0

    def _apply_warranty_dates(self, delivery_partner, sale_order, delivery_date):
        """Проставити гарантійні дати при відвантаженні (FR-D06).

        :param delivery_partner: кому відвантажили
        :param sale_order:       замовлення (може бути порожнім)
        :param delivery_date:    дата відвантаження (date)

        ⚠️ ЦІКАВЕ ПИТАННЯ: що робити, якщо номер уже мав гарантію
        (напр., клієнт повернув товар, і ми продали його знову)?

        Ми ПЕРЕЗАПИСУЄМО дати — нова продажа починає новий гарантійний
        термін. Це відповідає бізнес-практиці для B2B-дистрибуції:
        гарантія для кінцевого клієнта йде від дати ЙОГО покупки.

        Але є нюанс, який варто знати: вендорська гарантія зазвичай іде
        від дати ВИРОБНИЦТВА і не перезапускається. Тому для звернень
        до вендора може знадобитись окреме поле «вендорська гарантія».
        Ми його не робимо: у BRD цього немає, а додавати поля «на всяк
        випадок» — прямий шлях до роздутої моделі.
        """
        self.ensure_one()
        months = self._get_warranty_months()
        if months <= 0:
            # Термін не задано — гарантія не ведеться. Пишемо в лог,
            # бо це найчастіша причина «чому в нас немає гарантії».
            return False

        self.write({
            'warranty_start_date': delivery_date,
            'warranty_end_date': delivery_date + relativedelta(months=months),
            'warranty_months_applied': months,
            'warranty_customer_id': delivery_partner.id if delivery_partner else False,
            'warranty_sale_order_id': sale_order.id if sale_order else False,
        })
        return True

    # =========================================================================
    #  ДІЇ
    # =========================================================================

    def action_view_warranty_claims(self):
        """Smart-кнопка «Гарантійні звернення»."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Гарантійні звернення: %s', self.name),
            'res_model': 'techdistrib.warranty.claim',
            'view_mode': 'list,form',
            'domain': [('lot_id', '=', self.id)],
            'context': {'default_lot_id': self.id},
        }

    def action_create_warranty_claim(self):
        """Швидке створення звернення просто з картки серійного номера."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Нове гарантійне звернення'),
            'res_model': 'techdistrib.warranty.claim',
            'view_mode': 'form',
            'target': 'current',
            'context': {
                'default_lot_id': self.id,
                'default_partner_id': self.warranty_customer_id.id,
            },
        }
