# -*- coding: utf-8 -*-
# =============================================================================
#  Розширення res.partner: умови ретро-бонусу вендора
# =============================================================================
#  FR-F04: «ретро-бонус вендора — відсоток від обороту за квартал».
#
#  Відсоток домовляється з кожним вендором окремо й змінюється раз на рік.
#  Тому він живе НА КАРТЦІ ВЕНДОРА, а не в коді й не в нарахуванні:
#  нарахування — це документ за конкретний квартал, а вендор — це сутність,
#  у якої є умови співпраці.
# =============================================================================

from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    vendor_rebate_percent = fields.Float(
        string='Ретро-бонус, %',
        digits=(16, 2),
        help='Відсоток від обороту закупівель, який вендор повертає '
             'за підсумками кварталу.\n\n'
             'Залиште 0, якщо бонус не передбачено договором — тоді '
             'автоматичне нарахування для цього вендора не створюється.',
    )

    vendor_rebate_ids = fields.One2many(
        comodel_name='techdistrib.vendor.rebate',
        inverse_name='vendor_id',
        string='Ретро-бонуси',
    )

    vendor_rebate_count = fields.Integer(
        string='Нарахувань бонусу',
        compute='_compute_vendor_rebate_count',
    )

    def _compute_vendor_rebate_count(self):
        for partner in self:
            partner.vendor_rebate_count = len(partner.vendor_rebate_ids)

    def action_view_vendor_rebates(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Ретро-бонуси: %s' % self.display_name,
            'res_model': 'techdistrib.vendor.rebate',
            'view_mode': 'list,form',
            'domain': [('vendor_id', '=', self.id)],
            'context': {'default_vendor_id': self.id},
        }
