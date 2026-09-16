# -*- coding: utf-8 -*-
# =============================================================================
#  Розширення res.partner: автоматичне призначення прайс-листа рівня
# =============================================================================
#  FR-B02: знижка визначається РІВНЕМ дилера. Отже, коли рівень змінюється,
#  має автоматично змінюватись і прайс-лист партнера.
#
#  Тут ми перевизначаємо create і write у ДРУГОМУ модулі (перший —
#  techdistrib_base, де ми додали поля дилера). Це нормально: кожен модуль
#  розширює ту саму модель і додає свою реакцію на зміни.
# =============================================================================

from odoo import api, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    @api.model_create_multi
    def create(self, vals_list):
        partners = super().create(vals_list)
        partners._sync_dealer_pricelist()
        return partners

    def write(self, vals):
        result = super().write(vals)
        # Реагуємо лише на зміни, що впливають на прайс-лист. Це важливо:
        # write викликається на КОЖНЕ збереження форми, і робити тут
        # зайву роботу означало б помітно сповільнити інтерфейс.
        if {'dealer_tier_id', 'is_dealer'} & set(vals):
            self._sync_dealer_pricelist()
        return result

    def _sync_dealer_pricelist(self):
        """Призначити дилеру прайс-лист його рівня.

        Використовуємо СТАНДАРТНЕ поле Odoo
        res.partner.property_product_pricelist — саме його читає
        sale.order при створенні замовлення, щоб узяти ціну.

        ⚠️ Це поле — company_dependent: значення зберігається ОКРЕМО
        для кожної компанії. У мультикомпанійному середовищі дилер може
        мати різні прайс-листи в різних компаніях, і це правильно.

        Ми НЕ переписуємо прайс-лист, якщо він уже правильний: зайвий
        write спричинив би зайвий запис у базу й у chatter.
        """
        for partner in self:
            if not (partner.is_dealer and partner.dealer_tier_id):
                continue
            pricelist = partner.dealer_tier_id._ensure_pricelist()
            if partner.property_product_pricelist != pricelist:
                partner.property_product_pricelist = pricelist

    def action_reset_to_tier_pricelist(self):
        """Кнопка «Повернути прайс-лист рівня».

        Навіщо: менеджер може вручну поставити дилеру індивідуальний
        прайс-лист. Ця кнопка повертає стандартний прайс-лист рівня —
        зручно після експериментів і для виправлення помилок.
        """
        self.ensure_one()
        self.property_product_pricelist = False
        self._sync_dealer_pricelist()
        return True
