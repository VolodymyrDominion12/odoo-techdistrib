# 05. Модуль 3 — `techdistrib_pricing`: найкраща кастомізація — це конфігурація

> **Мета уроку:** навчитися задовольняти бізнес-вимогу, **не переписуючи**
> стандартний механізм Odoo, а правильно його налаштувавши.
>
> **Закриває вимоги:** FR-B02 … FR-B06.

---

## 1. Спокуса, якої треба уникнути

Вимога: «знижка залежить від рівня дилера і категорії товару».

Перше, що спадає на думку — перевизначити розрахунок ціни:

```python
# ❌ ТАК РОБИТИ НЕ МОЖНА
class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    def _compute_price_unit(self):
        price = self.product_id.list_price
        discount = self._get_matrix_discount()      # наша логіка
        return price * (1 - discount / 100)
```

Чому це погано:

| Проблема | Наслідок |
|----------|----------|
| Дублюємо логіку Odoo | Податки, валюти, округлення, одиниці виміру — усе доведеться повторити |
| Ламаємо звіти | `sale.report`, аналітика цін беруть дані з прайс-листів |
| Ламаємо інші модулі | Портал, підписки, e-commerce використовують той самий метод |
| Ламаємось на оновленні | Odoo змінить сигнатуру — наш код впаде |

**Правильний шлях:** Odoo **уже має** рушій ціноутворення. Треба лише
дати йому правильні вхідні дані.

---

## 2. Що вже є в Odoo

| Компонент | Призначення |
|-----------|-------------|
| `product.pricelist` | Прайс-лист (набір правил) |
| `product.pricelist.item` | Правило: на що діє, як рахувати |
| `res.partner.property_product_pricelist` | Прайс-лист конкретного партнера |
| `sale.order.pricelist_id` | Прайс-лист замовлення (береться з партнера) |
| `sale.order.line._compute_price_unit()` | Підставляє ціну з прайс-листа |
| `sale.order.line._compute_discount()` | Показує знижку окремим полем |

### Ключове правило прайс-листа

```python
{
    'applied_on': '2_product_category',   # діє на КАТЕГОРІЮ
    'categ_id': servers_category.id,
    'compute_price': 'percentage',        # відсоткова знижка
    'base': 'list_price',                 # від рекомендованої ціни
    'percent_price': 16.0,
}
```

**Найважливіший вибір — `compute_price='percentage'`.** Він дає ту поведінку,
якої вимагає бізнес:

```
price_unit = 1000.0     ← базова ціна
discount   = 16.0       ← знижка ОКРЕМИМ полем
subtotal   = 840.0
```

Менеджер **бачить** знижку і може її змінити, а керівник — проконтролювати.
З `compute_price='formula'` знижка була б «захована» в ціні, і контролювати
її було б неможливо.

---

## 3. Архітектура: фасад + стандартний механізм

```
   Бізнес бачить:                Під капотом:
   ┌──────────────────────┐      ┌─────────────────────────────┐
   │ Матриця знижок       │      │ product.pricelist (на рівень)│
   │ Gold / Сервери: 16 % │─────►│ product.pricelist.item       │
   │ Gold / Мережа:  18 % │      │   percentage, 16 %, категорія│
   └──────────────────────┘      └─────────────────────────────┘
```

**Чому не дати бізнесу редагувати прайс-лист напряму?** Бо в прайс-листі
правила описуються технічними полями (`applied_on`, `compute_price`, `base`),
і менеджер неминуче зробить помилку. Матриця «рівень × категорія → %» —
це звична таблиця, яку не можна зрозуміти неправильно.

Це патерн **«зручне подання + стандартний механізм під капотом»**. Він
застосовується всюди: замість того щоб вчити користувача складному
механізму, дай йому простий фасад і синхронізуй його з механізмом.

---

## 4. Синхронізація: три методи CRUD

```python
@api.model_create_multi
def create(self, vals_list):
    rows = super().create(vals_list)
    rows._sync_pricelist_item()        # ПІСЛЯ create — потрібен id
    return rows

def write(self, vals):
    result = super().write(vals)
    if {'discount', 'categ_id', 'tier_id'} & set(vals):
        self._sync_pricelist_item()    # лише якщо змінилось значуще
    return result

def unlink(self):
    self.mapped('pricelist_item_id').unlink()   # СПЕРШУ правило
    return super().unlink()
```

### Пастка порядку в `unlink`

```python
# ❌ Правило залишиться «сиротою» і продовжить давати знижку
def unlink(self):
    return super().unlink()

# ✅ Спершу прибираємо залежне
def unlink(self):
    self.mapped('pricelist_item_id').unlink()
    return super().unlink()
```

Найгірше в «осиротілому» правилі те, що воно працює **тихо**: менеджер
бачить знижку, у матриці її вже немає, і ніхто не розуміє, звідки вона.

### Навіщо потрібна кнопка «Перебудувати»

Будь-яка автоматична синхронізація може розійтися з джерелом істини:
хтось виправив правило вручну, імпорт даних зламав зв'язок, помилка
в транзакції лишила слід. Тому кнопка-«рятівне коло» — **обов'язковий
елемент** такої синхронізації. Людина має мати спосіб сказати
«перебудуй усе з джерела істини».

---

## 5. Пастка: функція Odoo «Discounts» має бути увімкнена

Це найпідступніше місце модуля. У коді Odoo (`addons/sale/models/sale_order_line.py`):

```python
def _compute_discount(self):
    discount_enabled = self.env['product.pricelist.item']._is_discount_feature_enabled()
    ...
    if not (line.order_id.pricelist_id and discount_enabled):
        continue          # ← знижка НЕ буде порахована
```

А `_is_discount_feature_enabled()` перевіряє, чи суперкористувач має групу
`sale.group_discount_per_so_line`.

**Що буде, якщо її не увімкнути:** ціна в рядку стане правильною (840),
але поле `discount` лишиться **нулем**. Наслідки:
* менеджер не бачить, яку знижку отримав дилер;
* контроль ручної знижки (FR-B04) стає неможливим — контролювати нічого;
* звіти про надані знижки покажуть нулі.

Тому наш модуль вмикає функцію програмно:

```xml
<record id="base.group_user" model="res.groups">
    <field name="implied_ids" eval="[(4, ref('sale.group_discount_per_so_line'))]"/>
</record>
```

У Налаштуваннях → Продажі це перемикач **Discounts**. Він робить рівно це.

> **Урок:** перш ніж перевизначати розрахунок — перевір, чи не вимкнена
> відповідна функція в налаштуваннях. Odoo часто «не працює» саме тому.

---

## 6. Контроль ручної знижки: найтонша логіка модуля

FR-B04: менеджер може дати додаткову знижку, але не більше ліміту рівня.

### Як виглядає `discount` насправді

Odoo (`_compute_discount`) рахує так:

```python
discount = (base_price - pricelist_price) / base_price * 100
```

Тобто в полі `discount` лежить **СУКУПНА** знижка: і від прайс-листа,
і додана менеджером вручну.

### 🔴 Помилка, якої треба уникнути

```python
# ❌ НЕПРАВИЛЬНО: Gold-дилер із 16 % від прайс-листа не отримає
#    жодної ручної знижки, бо 16 > 5. Ліміт стане ЗАБОРОНОЮ.
if line.discount > tier.max_manual_discount:
    raise ValidationError(...)
```

### Правильно

```python
base_discount = line._get_tier_discount()          # з матриці
manual = line.discount - base_discount             # ← лише ручна частина
if manual > tier.max_manual_discount + 0.01:       # допуск на float
    raise ValidationError(...)
```

На це є окремий тест:

```python
def test_manual_discount_check_uses_only_manual_part(self):
    line.discount = 21.0                            # повна = 21 %
    self.assertGreater(line.discount, line.manual_discount_limit)   # 21 > 5
    self.assertLessEqual(line.manual_discount, line.manual_discount_limit)  # 5 <= 5
```

### Чому допуск `+0.01`

Знижка «рівно 5 %» через похибку float може обчислитись як `5.000000001`
і бути відхилена. Округлення сум і допуски — не «брудний хак», а
обов'язкова частина роботи з грошима.

---

## 7. «Безпека-театр»: правило, яке виглядало як захист

Реальна помилка, яку я зробив і виправив у цьому модулі.

Мета: «видаляти рядки матриці може лише адміністратор». Я написав:

```xml
<!-- ❌ ВИГЛЯДАЄ як захист, АЛЕ НЕ ЗАХИЩАЄ -->
<record id="rule_discount_matrix_no_delete" model="ir.rule">
    <field name="name">Матриця знижок: видаляти може лише адміністратор</field>
    <field name="domain_force">[(1, '=', 1)]</field>
    <field name="groups" eval="[(4, ref('base.group_system'))]"/>
    <field name="perm_unlink" eval="True"/>
</record>
```

**Чому це не працює:**

* `ir.rule` обмежує **РЯДКИ**, а не **ОПЕРАЦІЇ**;
* `[(1, '=', 1)]` означає «усі рядки» — правило нічого не звужує;
* користувачі з `group_dealer_manager` під це правило взагалі не підпадають,
  тому могли б видаляти так само, як адміністратор.

Правило було **security theater**: назва обіцяла захист, механізм його
не давав. Найгірший різновид помилки в безпеці — той, що виглядає як захист.

### Правильно

```csv
id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink
access_discount_matrix_user,....,group_dealer_user,1,0,0,0
access_discount_matrix_manager,....,group_dealer_manager,1,1,1,0
access_discount_matrix_admin,....,base.group_system,1,1,1,1
```

Остання колонка `perm_unlink=0` у менеджера і `1` в адміністратора.

> **ЗАПАМ'ЯТАЙ РОЗПОДІЛ ВІДПОВІДАЛЬНОСТІ:**
> * `ir.model.access.csv` → **ХТО і ЩО МОЖЕ РОБИТИ** (read/write/create/unlink)
> * `ir.rule` → **ЯКІ РЯДКИ** це стосується
>
> Для «хто може видаляти» — тільки ACL.

---

## 8. `noupdate` і `<function>`: коли запис не оновлюється

Рівні Bronze/Silver/Gold/Platinum створює `techdistrib_base` з `noupdate="1"`.
Тому `max_manual_discount` для них **не можна** проставити звичайним записом:

```xml
<!-- ❌ Буде ПРОПУЩЕНО: запис позначений noupdate -->
<record id="techdistrib_base.dealer_tier_gold" model="techdistrib.dealer.tier">
    <field name="max_manual_discount">5.0</field>
</record>
```

Odoo побачить, що запис має `noupdate=True`, і не оновить його.

**Рішення — `<function>`**, яка викликає Python-метод поза цим механізмом:

```xml
<function model="techdistrib.dealer.tier"
          name="_setup_default_discount_limits"/>
```

```python
@api.model
def _setup_default_discount_limits(self):
    for code, limit in {'bronze': 0.0, 'silver': 2.0,
                        'gold': 5.0, 'platinum': 10.0}.items():
        tier = self.search([('code', '=', code)], limit=1)
        if tier and not tier.max_manual_discount:   # ідемпотентно
            tier.max_manual_discount = limit
```

Зверни увагу на `if tier and not tier.max_manual_discount` — метод
**ідемпотентний**: повторне оновлення модуля не загубить налаштувань
адміністратора.

---

## 9. Помилки цього уроку

| # | Помилка | Симптом | Урок |
|---|---------|---------|------|
| 1 | `--` у коментарі (двічі!) | `XMLSyntaxError: Double hyphen within comment` | Перевіряй XML скриптом, а не оком |
| 2 | Дія оголошена після подання | `External ID not found` | Порядок: дії → подання → меню |
| 3 | `ir.rule` замість ACL для заборони видалення | Правило не працює, але виглядає як захист | Рядки — `ir.rule`, операції — ACL |
| 4 | Тести створювали засіяні пари | `UniqueViolation` | Якщо модуль наповнює базу — тести мають це враховувати |
| 5 | `mapped('_ensure_pricelist')` | `KeyError` | `mapped` приймає назву поля, не метод |
| 6 | Тест перевіряв значення поля замість контракту | `AssertionError: pricelist(1) is not false` | Перевіряй **свій код**, а не поведінку Odoo за замовчуванням |
| 7 | `default_group_by` на `<list>` | Помилка валідації подання | Цей атрибут існує лише в `<kanban>` |

Розберемо помилку №6 докладніше — вона найповчальніша.

### Тест перевіряв не те, що треба

Мета тесту: «не-дилер не отримує прайс-лист рівня». Я написав:

```python
self.assertFalse(client.property_product_pricelist)   # ❌
```
Упало: `AssertionError: product.pricelist(1,) is not false`.

Друга спроба:
```python
self.assertNotIn(client.property_product_pricelist.id, tier_pricelist_ids)  # ❌
```
Упало знову: `AssertionError: 1 unexpectedly found in [4, 3, 2, 1]`.

**Причина:** у тестовій базі (`--without-demo=all`) немає стандартного
«Public Pricelist», тому Odoo підставляє партнеру **перший активний**
прайс-лист — а ним виявився наш же Bronze.

Тест перевіряв не нашу логіку, а поведінку Odoo за замовчуванням.

**Правильно — перевіряти КОНТРАКТ свого коду:**

```python
before = client.property_product_pricelist
client._sync_dealer_pricelist()
self.assertEqual(client.property_product_pricelist, before)
```

Це загальний принцип: **тест має перевіряти твій код, а не оточення.**
Якщо тест падає через особливості середовища, у продакшені він не
захистить ні від чого.

---

## 10. Перевірка результату

```bash
./scripts/odoo_shell.sh -d techdistrib
```
```python
>>> tier = env.ref('techdistrib_base.dealer_tier_gold')
>>> tier.pricelist_id.name
'TechDistrib Gold'
>>> tier.matrix_ids.mapped(lambda r: (r.categ_id.name, r.discount))
[('Сервери та СХД', 16.0), ('Мережеве обладнання', 18.0), ...]

>>> # Перевірка ціни в замовленні
>>> dealer = env['res.partner'].create({'name': 'Тест', 'is_dealer': True, 'dealer_tier_id': tier.id})
>>> product = env['product.product'].search([('categ_id.name', 'ilike', 'Сервери')], limit=1)
>>> order = env['sale.order'].create({'partner_id': dealer.id, 'order_line': [(0,0,{'product_id': product.id, 'product_uom_qty': 1})]})
>>> order.order_line.price_unit, order.order_line.discount, order.order_line.price_subtotal
(1000.0, 16.0, 840.0)
>>> env.cr.commit()
```

У браузері: **TechDistrib → Дилерська програма → Матриця знижок** —
таблиця 4×4, яку можна редагувати прямо у списку.

---

## 11. Чек-ліст

- [ ] Перш ніж перевизначати розрахунок — перевірено, чи немає стандартного механізму
- [ ] Синхронізація покриває `create`, `write` **і** `unlink`
- [ ] Синхронізація ідемпотентна (повторний виклик не плодить дублі)
- [ ] Є «рятівна» дія для відновлення розсинхронізації
- [ ] Перевірено, чи потрібна функція Odoo увімкнена в налаштуваннях
- [ ] Порівняння грошей має допуск на похибку float
- [ ] Обмеження **операцій** — в ACL, а не в `ir.rule`
- [ ] Для записів з `noupdate` використано `<function>`, а не `<record>`
- [ ] `./scripts/run_tests.sh techdistrib_pricing` — усе зелене
- [ ] Немає WARNING-ів при оновленні модуля

---

## 12. Вправи

1. **Легка.** Додай у матрицю поле `min_quantity` і передавай його в
   правило прайс-листа. Перевір, що при купівлі 10 одиниць застосовується
   інша знижка.

2. **Середня.** Зроби так, щоб матриця підтримувала знижку не лише по
   категорії, а й по конкретному товару (поле `product_tmpl_id`,
   `applied_on='1_product'`). Продумай пріоритет: що перемагає — правило
   товару чи категорії?

3. **Середня.** Додай звіт «Надані знижки за період»: скільки відсотків
   знижки віддано кожному рівню і скільки це грошей.

4. **Складна.** Реалізуй **погодження** знижки понад ліміт — за зразком
   модуля `techdistrib_credit`: модель запиту, workflow, строк дії.
   Перевикористай наявний код, не дублюй його.

---

**Далі:** [`06-module-warranty.md`](06-module-warranty.md) — гарантія та RMA:
наскрізний ланцюжок доказів від серійного номера до компенсації вендора.
