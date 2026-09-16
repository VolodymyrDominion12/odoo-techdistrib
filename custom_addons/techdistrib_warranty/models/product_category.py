# -*- coding: utf-8 -*-
# =============================================================================
#  Розширення product.category: термін гарантії
# =============================================================================
#  FR-E03: «гарантія = дата продажу + термін, що залежить від КАТЕГОРІЇ».
#
#  Чому на категорії, а не на товарі?
#  Термін гарантії в вендора визначається не конкретною моделлю, а
#  класом обладнання: сервери — 36 міс., мережа — 24, аксесуари — 12.
#  Ставити термін на 1200 товарів означало б 1200 полів для ручного
#  заповнення і постійні розбіжності.
#
#  Але на товарі теж буває потрібно (напр., вендор дав подовжену гарантію
#  на конкретну партію). Тому додаємо поле і на product.template —
#  як ПЕРЕВИЗНАЧЕННЯ категорії. Див. stock_lot.py, метод _get_warranty_months.
# =============================================================================

from odoo import api, fields, models


class ProductCategory(models.Model):
    _inherit = 'product.category'

    warranty_months = fields.Integer(
        string='Гарантія, міс.',
        default=24,
        help='Типовий термін гарантії для товарів цієї категорії. '
             'Відлік іде від дати відвантаження клієнту.',
    )

    @api.model
    def _setup_default_warranty_months(self):
        """Проставити терміни гарантії для категорій TechDistrib.

        ⚠️ Знову <function>, а не <record>, і з тієї ж причини, що в
        модулі знижок: категорії створює techdistrib_pricing з
        атрибутом noupdate="1", тому звичайне оновлення запису з
        data-файлу було б ПРОПУЩЕНО.

        Викликаємо цей метод із data/product_category_data.xml.

        Значення взяті з типової практики вендорів обладнання:
            сервери та СХД        — 36 місяців
            мережеве обладнання   — 24 місяці
            промисловий IoT       — 36 місяців
            аксесуари             — 12 місяців
        """
        mapping = {
            'techdistrib_pricing.product_category_servers': 36,
            'techdistrib_pricing.product_category_network': 24,
            'techdistrib_pricing.product_category_iot': 36,
            'techdistrib_pricing.product_category_accessories': 12,
        }
        for xml_id, months in mapping.items():
            category = self.env.ref(xml_id, raise_if_not_found=False)
            if category:
                category.warranty_months = months
        return True


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    warranty_months = fields.Integer(
        string='Гарантія, міс. (перевизначення)',
        default=0,
        help='Залиште 0, щоб використати термін категорії товару. '
             'Заповніть, якщо для цієї моделі діє інший термін.',
    )
