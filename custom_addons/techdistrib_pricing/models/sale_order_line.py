# -*- coding: utf-8 -*-
# =============================================================================
#  Розширення sale.order.line: контроль ручної знижки менеджера
# =============================================================================
#  FR-B04: менеджер може дати додаткову разову знижку, але не більше
#          ліміту свого рівня.
#  FR-B05: знижка понад ліміт потребує погодження керівника.
#
#  ⚠️ СПЕРШУ РОЗБЕРЕМОСЬ, ЯК ODOO РАХУЄ ЗНИЖКУ — інакше логіка буде хибною.
#
#  Коли дилер має прайс-лист із правилом «percentage 16 %», Odoo робить так
#  (addons/sale/models/sale_order_line.py):
#
#      _compute_price_unit()  → price_unit = базова ціна продажу (напр. 1000)
#      _compute_discount()    → discount = (base_price - pricelist_price) / base_price * 100
#                             → discount = 16
#
#  Тобто у полі discount лежить СУКУПНА знижка: і та, що дає прайс-лист,
#  і та, що менеджер дописав руками.
#
#  Отже перевіряти треба НЕ `discount <= ліміт`, а
#      (discount − знижка_прайс-листа) <= ліміт_ручної_знижки
#
#  Це найважливіша деталь модуля: якщо перевіряти повну знижку проти ліміту
#  ручної, то Gold-дилер із 16 % знижки від прайс-листа не отримав би
#  жодної ручної знижки взагалі — бо 16 > 5.
# =============================================================================

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    # Показуємо менеджеру довідкову інформацію прямо в рядку:
    # яка знижка йде від рівня, а яка — його власна.
    tier_discount = fields.Float(
        string='Знижка рівня, %',
        compute='_compute_tier_discount',
        help='Знижка, яку дає прайс-лист рівня партнера.',
    )

    manual_discount = fields.Float(
        string='Ручна знижка, %',
        compute='_compute_tier_discount',
        help='Додаткова знижка, яку вніс менеджер понад знижку рівня.',
    )

    manual_discount_limit = fields.Float(
        string='Ліміт ручної знижки, %',
        compute='_compute_tier_discount',
        help='Скільки відсотків менеджер може додати самостійно.',
    )

    manual_discount_excess = fields.Float(
        string='Перевищення ліміту, %',
        compute='_compute_tier_discount',
        help='На скільки ручна знижка перевищує ліміт. Більше нуля — '
             'потрібне погодження керівника.',
    )

    @api.depends('discount', 'product_id', 'order_id.partner_id',
                 'order_id.partner_id.dealer_tier_id')
    def _compute_tier_discount(self):
        for line in self:
            tier = line.order_id.partner_id.dealer_tier_id
            if not tier:
                line.tier_discount = 0.0
                line.manual_discount = 0.0
                line.manual_discount_limit = 0.0
                line.manual_discount_excess = 0.0
                continue

            base_discount = line._get_tier_discount()
            line.tier_discount = base_discount
            line.manual_discount = line.discount - base_discount
            line.manual_discount_limit = tier.max_manual_discount
            line.manual_discount_excess = max(
                0.0, line.manual_discount - tier.max_manual_discount,
            )

    def _get_tier_discount(self):
        """Знижка, яку дає прайс-лист рівня для цього товару.

        Читаємо з МАТРИЦІ — це джерело істини. Альтернатива (дивитись
        у правила прайс-листа) гірша: довелося б повторювати логіку
        вибору правила, яку Odoo робить у _compute_price_rule.
        """
        self.ensure_one()
        if not (self.product_id and self.product_id.categ_id):
            return 0.0

        tier = self.order_id.partner_id.dealer_tier_id
        if not tier:
            return 0.0

        matrix = self.env['techdistrib.discount.matrix'].search([
            ('tier_id', '=', tier.id),
            ('categ_id', '=', self.product_id.categ_id.id),
        ], limit=1)
        return matrix.discount if matrix else 0.0

    @api.constrains('discount')
    def _check_manual_discount_limit(self):
        """FR-B04/B05: ручна знижка не може перевищувати ліміт рівня."""
        for line in self:
            # Перевіряємо лише рядки, які ще не підтверджені.
            # Правило «ціна фіксується при підтвердженні» (FR-B06) означає,
            # що підтверджене замовлення вже не має блокуватись заднім числом.
            if line.order_id.state not in ('draft', 'sent'):
                continue

            tier = line.order_id.partner_id.dealer_tier_id
            if not tier:
                continue

            base_discount = line._get_tier_discount()
            manual = line.discount - base_discount
            limit = tier.max_manual_discount

            # Допуск 0.01 — компенсація похибки float. Без нього знижка
            # «рівно 5 %» могла б обчислитись як 5.000000001 і бути відхилена.
            if manual > limit + 0.01:
                raise ValidationError(_(
                    'Ручна знижка %(manual).2f %% перевищує ліміт для рівня '
                    '«%(tier)s» (%(limit).2f %%).\n\n'
                    'Розрахунок:\n'
                    '  Знижка рівня (з прайс-листа): %(base).2f %%\n'
                    '  Загальна знижка в рядку:      %(total).2f %%\n'
                    '  Ваша ручна надбавка:          %(manual).2f %%\n'
                    '  Дозволено:                    %(limit).2f %%\n\n'
                    'Що робити: зменште знижку або зверніться до керівника '
                    'для збільшення ліміту рівня.',
                    manual=manual,
                    tier=tier.name,
                    limit=limit,
                    base=base_discount,
                    total=line.discount,
                ))
