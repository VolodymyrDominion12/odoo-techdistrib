# 02. Архітектура Odoo: ORM, моделі, поля, наслідування

> Це довідник, а не суцільне читання. Повертайся сюди, коли зустрінеш
> незнайомий декоратор, атрибут поля або помилку `Compute method failed to assign`.
>
> **Усі твердження тут перевірені на коді Odoo 18** — із посиланнями на файли
> в `vendor/odoo/`, щоб ти міг перевірити сам.

---

## 1. `env` — середовище

Усе в Odoo починається з `env`. Це об'єкт, який знає:

| Що | Як отримати |
|----|-------------|
| Доступ до моделей | `env['res.partner']` |
| Поточний користувач | `env.user` |
| Поточна компанія | `env.company` |
| Мова, часовий пояс | `env.context.get('lang')` |
| Курсор БД | `env.cr` |
| Кеш | `env.cache` |

`env` **незмінне**. Щоб отримати інше середовище — створюють нове:

```python
env['res.partner'].with_user(other_user).create({...})   # від імені іншого
env['res.partner'].with_company(other_company).search([])
env['res.partner'].with_context(lang='en_US').search([])
env['res.partner'].sudo()                                 # суперкористувач
```

**Ключова ідея:** `env` несе з собою **права доступу**. Метод, викликаний на
`env['x']`, виконується від імені `env.user` з його правами. `sudo()` знімає
перевірки — використовуй свідомо (див. [урок 4](04-module-credit.md), розділ 8).

---

## 2. Recordset — головна абстракція

`search()` повертає не список, а **recordset**:

```python
partners = env['res.partner'].search([('is_dealer', '=', True)])
```

Recordset поводиться як список, але методи виконуються **на всіх записах одразу**:

```python
partners.write({'dealer_status': 'new'})    # один UPDATE ... WHERE id IN (...)
len(partners)                               # кількість
partners.mapped('name')                     # список значень
partners.filtered(lambda p: p.credit_used > 0)
partners.sorted(lambda p: p.name, reverse=True)
partners[0]                                 # перший (сам запис, не список)
partners[:5]                                # перші 5 (новий recordset)
partner | other_partner                     # об'єднання
partner & other_partner                     # перетин
```

> **Новачкам:** `partners[0]` повертає **запис**, а не список із одним записом.
> Це відрізняється від Python-списків і часто плутає.

### `mapped`, `filtered`, `sorted` — не косметика

```python
# ❌ N+1 запит до бази
total = 0
for p in partners:
    total += p.credit

# ✅ один запит
total = sum(partners.mapped('credit'))
```

Odoo **prefetch**-ить пов'язані поля: звернення до `p.credit` для
першого запису завантажує `credit` **для всього recordset** одним запитом.
Тому цикли по recordset часто швидші, ніж здається — але `mapped`/`filtered`
однаково кращі: вони явно виражають намір.

---

## 3. Моделі

### Три базові класи

| Клас | Таблиця в БД | Призначення |
|------|--------------|-------------|
| `models.Model` | ✅ так | Постійні дані (партнери, замовлення) |
| `models.TransientModel` | ✅ так, але чиститься | Тимчасові дані: візарди, майстри |
| `models.AbstractModel` | ❌ ні | Міксини без власних даних |

### Службові атрибути

```python
class MyModel(models.Model):
    _name = 'techdistrib.my.model'      # технічна назва → таблиця techdistrib_my_model
    _description = 'Моя модель'         # обов'язково; показується в Technical → Models
    _order = 'sequence desc, name'      # типовий ORDER BY
    _rec_name = 'name'                  # яке поле показувати як «назву» запису
    _rec_names_search = ['name', 'code'] # по яких полях шукати через name_search
    _check_company_auto = True          # автоперевірка company_id у зв'язках
```

`_rec_name` критично важливий: Odoo використовує його для `display_name` —
того тексту, який показується в полях Many2one. За замовчуванням це `name`,
але якщо в моделі немає поля `name`, Odoo візьме перше поле типу Char.

---

## 4. Поля: повний довідник

### Типи

| Тип | Python-значення | SQL | Примітка |
|-----|-----------------|-----|----------|
| `Char` | `str` | varchar | |
| `Text` | `str` | text | без обмеження довжини |
| `Html` | `str` | text | санітизується |
| `Integer` | `int` | int4 | |
| `Float` | `float` | numeric | `digits=(16, 2)` для точності |
| `Monetary` | `float` | numeric | потребує `currency_field` |
| `Boolean` | `bool` | bool | |
| `Date` | `datetime.date` | date | |
| `Datetime` | `datetime.datetime` | timestamp | **завжди UTC** |
| `Selection` | `str` | varchar | + індекс |
| `Binary` | `bytes` | bytea/filestore | великі файли — у filestore |
| `Many2one` | recordset (0..1) | int4 + FK | |
| `One2many` | recordset | **немає колонки** | зворотний бік Many2one |
| `Many2many` | recordset | окрема таблиця-зв'язка | |

> **`Datetime` завжди в UTC.** Odoo конвертує в часову зону користувача лише
> для показу. Ніколи не зберігай локальний час — отримаєш зсув у 2–3 години
> й довгі пошуки причини.

### Атрибути, які варто знати

```python
field = fields.Char(
    string='Заголовок',        # підпис в UI (без нього — з назви змінної)
    required=True,             # NOT NULL + перевірка в UI
    readonly=True,             # ЛИШЕ UI! із Python писати можна
    index=True,                # індекс БД — для полів, по яких ФІЛЬТРУЄШ
    copy=False,                # не копіювати при дублюванні запису
    default=...,               # значення за замовчуванням
    help='...',                # підказка при наведенні
    groups='base.group_user',  # ХТО бачить поле (справжня перевірка!)
    tracking=True,             # писати зміни в chatter (потрібен mail.thread)
    compute='_compute_x',      # обчислюване
    store=True,                # зберігати в БД
    search='_search_x',        # як шукати (для незбережених!)
    inverse='_inverse_x',      # як записати (для обчислюваних)
    related='partner_id.name', # «дзеркало» поля з іншої моделі
    company_dependent=True,    # окреме значення на кожну компанію
    translate=True,            # перекладне значення (jsonb)
)
```

> **`readonly=True` — це UI-обмеження, не захист.** Через Python або API
> значення можна змінити. Для справжнього захисту — `groups=` або перевірка
> в `write()`.

### `copy=False` — коли обов'язково

Поля-ідентифікатори: коди, номери, послідовності. Без `copy=False`
дублювання запису скопіює код, і `unique`-обмеження впаде з незрозумілою
помилкою. Це реальна помилка з [уроку 3](03-module-base.md).

---

## 5. Обчислювані поля — серце ORM

### Базовий патерн

```python
total = fields.Monetary(compute='_compute_total', store=True, currency_field='currency_id')

@api.depends('line_ids.price_subtotal')
def _compute_total(self):
    for record in self:
        record.total = sum(record.line_ids.mapped('price_subtotal'))
```

**Три правила, які не можна порушувати:**

1. **Присвоїти значення КОЖНОМУ запису в `self`.** Пропустиш — отримаєш
   `Compute method failed to assign`. Навіть якщо це `False` чи `0`.
2. **`@api.depends` обов'язковий.** Без нього поле ніколи не перерахується.
3. **Ніколи не викликай обчислювач напряму.** Використовуй
   `self.env.add_to_compute(field, records)` — інакше оминеш кеш і граф
   залежностей.

### `store=True` чи ні — рішення з наслідками

| | `store=True` | без `store` |
|---|---|---|
| Колонка в БД | ✅ | ❌ |
| Пошук доменом | ✅ | ❌ **лише з `search=`** |
| Групування, сортування | ✅ | ❌ |
| Актуальність | перерахується при зміні залежності | завжди при читанні |
| Часова залежність (від «сьогодні») | ❌ протухає → потрібен cron | ✅ завжди правильне |

### 🔴 ГОЛОВНА ПАСТКА: незбережені поля не можна шукати

Ось код ядра (`vendor/odoo/odoo/osv/expression.py:1179`):

```python
elif not field.store:
    # Non-stored field should provide an implementation of search.
    if not field.search:
        # field does not support search!
        _logger.error("Non-stored field %s cannot be searched.", field, exc_info=True)
        # Ignore it: generate a dummy leaf.
        domain = []
```

**Odoo не кидає помилку.** Він пише ERROR у лог і **мовчки ігнорує умову**.
Фільтр «показати заблокованих» показав би **всі** записи.

**Рішення** — метод пошуку:

```python
credit_is_blocked = fields.Boolean(compute='_compute_x', search='_search_credit_is_blocked')

@api.model
def _search_credit_is_blocked(self, operator, value):
    if operator not in ('=', '!='):
        raise UserError(_('Непідтримуваний оператор'))
    want = (operator == '=' and value) or (operator == '!=' and not value)
    blocked = self.env['account.move'].search([...]).mapped('commercial_partner_id')
    return [('id', 'in', blocked.ids)] if want else [('id', 'not in', blocked.ids)]
```

Метод отримує `(operator, value)` і повертає **звичайний домен**, виражений
через **збережені** поля.

> **Правило:** є фільтр по computed-полю → перевір, чи воно `store=True`.
> Якщо ні — тобі потрібен `search=`.

### `@api.depends` і успадкування: залежності ЗЛИВАЮТЬСЯ

Часте питання: якщо батько має свій `@api.depends`, а нащадок додає свій —
вони зливаються чи замінюються?

Відповідь — у `vendor/odoo/odoo/fields.py:574-586`:

```python
# determine the functions implementing self.compute
if isinstance(self.compute, str):
    funcs = resolve_mro(model, self.compute, callable)
...
for func in funcs:
    deps = getattr(func, '_depends', ())
    depends.extend(deps(model) if callable(deps) else deps)
```

`resolve_mro` збирає **всі** методи з таким іменем по всьому ланцюжку
успадкування, і Odoo **складає** їхні залежності.

**Отже: залежності зливаються.** Повторювати батьківські не потрібно —
але писати їх корисно для читача.

---

## 6. `related` — «дзеркальні» поля

```python
commercial_partner_id = fields.Many2one(
    'res.partner', related='partner_id.commercial_partner_id',
    store=True, index=True,
)
```

Related-поле читає значення з пов'язаного запису. Можна ланцюжком:
`related='partner_id.company_id.currency_id'`.

* без `store` — віртуальне, кожне читання робить JOIN;
* з `store=True` — **справжня колонка**, оновлюється при зміні джерела.

---

## 7. Наслідування: три різні речі з однаковою назвою

### 7.1. Класичне розширення

```python
class ResPartner(models.Model):
    _inherit = 'res.partner'      # БЕЗ _name
```

Та сама таблиця. Колонки додаються через `ALTER TABLE`. Усі модулі бачать
нові поля.

### 7.2. Делегація

```python
class MyModel(models.Model):
    _name = 'my.model'
    _inherits = {'res.partner': 'partner_id'}
```

Нова таблиця + **автоматичне** створення пов'язаного партнера. Усі поля
партнера стають доступними в `my.model`.

### 7.3. Наслідування подань

```xml
<field name="inherit_id" ref="base.view_partner_form"/>
<xpath expr="//page[@name='sales_purchases']" position="inside"> ... </xpath>
```

Подання не замінюється, а **зливається**.

### Перевизначення методів

```python
def action_confirm(self):
    # ... наша логіка ДО ...
    return super().action_confirm()      # ← майже завжди обов'язково
```

> **Без `super()` стандартна логіка не виконається** — і метод «нічого не
> зробить». Це найважче для діагностики: помилки немає, просто нічого
> не відбувається.

---

## 8. Декоратори API

| Декоратор | Призначення |
|-----------|-------------|
| `@api.depends(...)` | залежності обчислюваного поля |
| `@api.depends_context('lang')` | залежність від контексту |
| `@api.constrains('field')` | перевірка після create/write |
| `@api.onchange('field')` | перерахунок у формі (до збереження) |
| `@api.model` | метод рівня моделі, без конкретного запису |
| `@api.model_create_multi` | `create` зі **списком** словників (Odoo 13+) |
| `@api.autovacuum` | метод для очищення TransientModel |

### `constrains` vs `onchange` — різниця принципова

| | `@api.constrains` | `@api.onchange` |
|---|---|---|
| Де працює | на сервері, завжди | **лише в UI**, у формі |
| Коли | після create/write | при зміні поля у формі |
| Чи спрацює при імпорті | ✅ так | ❌ ні |
| Що робити при порушенні | `raise ValidationError` | показати попередження |

> **Ніколи не покладайся на `onchange` для бізнес-правил.** Імпорт, API та
> масові операції його не викликають. `onchange` — лише для UX
> (підставити значення, показати підказку).

---

## 9. Домени

Домен — список умов. Виглядає як Python-список кортежів, і це не випадково:
у Python-коді домени пишуться точно так само, як у XML.

```python
[('is_dealer', '=', True), ('credit_limit', '>', 0)]
```

Логічні оператори пишуться **префіксно** (польська нотація):

```python
# A OR B
['|', ('a', '=', 1), ('b', '=', 2)]

# A AND (B OR C)
[('a', '=', 1), '|', ('b', '=', 2), ('c', '=', 3)]

# NOT A
['!', ('a', '=', 1)]
```

> ⚠️ Оператор діє на **наступні N умов**, тому `'|'` ставиться **перед**
> умовами, а не між ними. Це найчастіша помилка в доменах.

Корисні оператори: `=`, `!=`, `>`, `<`, `>=`, `<=`, `in`, `not in`,
`like`, `ilike` (без урахування регістру), `=like`, `child_of` (ієрархія),
`parent_of`, `any`/`not any` (для One2many/Many2many).

---

## 10. CRUD: що і коли перевизначати

```python
@api.model_create_multi
def create(self, vals_list):
    # ← тут: підстановка значень за замовчуванням, генерація кодів
    return super().create(vals_list)

def write(self, vals):
    # ← тут: реакція на зміну конкретних полів
    return super().write(vals)

def unlink(self):
    # ← тут: перевірка «а чи можна видаляти»
    return super().unlink()

@api.model
def search(self, domain, offset=0, limit=None, order=None):
    # ← перевизначають РІДКО, зазвичай для глобальної фільтрації
    return super().search(...)
```

**Три правила:**

1. `create` — **тільки** `@api.model_create_multi` зі списком.
2. `super()` — обов'язковий.
3. Не роби важку роботу в `write()`: він викликається на кожне збереження
   форми. Для важкого — cron або окрема кнопка.

### Порядок у `create` vs `write`

Класична помилка: обробити логіку лише в `create`. Сценарій «зробити
наявний запис таким, що відповідає умові» йде через `write` — і логіка
не спрацьовує.

---

## 11. Транзакції та помилки

```python
from odoo.exceptions import UserError, ValidationError, AccessError
```

| Виключення | Коли | Що бачить користувач |
|-----------|------|---------------------|
| `ValidationError` | дані некоректні | спливаюче вікно з текстом |
| `UserError` | бізнес-правило порушено | спливаюче вікно з текстом |
| `AccessError` | немає прав | стандартне повідомлення |

### 🔴 Виключення ВІДКОЧУЄ всю транзакцію

```python
# ❌ Запис ЗНИКНЕ
def action_confirm(self):
    self.env['my.log'].create({'note': 'спроба'})
    raise UserError('Не можна!')          # ← відкат знищить і лог
```

Усе, створене в тій самій транзакції до виключення, буде відкочено.
Якщо потрібно щось зберегти — це має бути **окрема дія** або окрема
транзакція.

### Перевірка — першою, дія — потім

```python
def action_confirm(self):
    self._check_preconditions()      # ← кидає помилку ДО будь-яких змін
    return super().action_confirm()
```

---

## 12. Безпека: три шари

| Шар | Файл | Питання, на яке відповідає |
|-----|------|---------------------------|
| Групи | `security/*.xml` | Хто є ким |
| Доступ до моделі | `security/ir.model.access.csv` | Чи можна чіпати таблицю |
| Доступ до рядків | `ir.rule` | Які саме записи видно |

```csv
id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink
access_x_user,X user,model_my_model,group_my_user,1,0,0,0
```

`model_id:id` = `model_` + назва моделі з `_` замість крапок.
`my.model` → `model_my_model`.

### Пастка OR у `ir.rule`

* **глобальні** правила (без `groups`) — об'єднуються через **AND**;
* **правила груп** — через **OR** між собою.

Тому для привілейованої групи потрібне **окреме дозвільне правило**
`[(1, '=', 1)]` — інакше вона потрапить під обмеження нижчої групи
(детально в [уроці 4](04-module-credit.md), розділ 9).

---

## 13. Продуктивність

| Антипатерн | Як правильно |
|-----------|--------------|
| `for r in records: r.field` | Odoo prefetch-ить автоматично, але краще `records.mapped('field')` |
| `search` у циклі | один `search` з доменом `('id', 'in', ids)` |
| `len(records)` для перевірки | `if records:` (не робить COUNT) |
| `search_count` у циклі | один `read_group` |
| Обчислюване поле без `store` у списку | зроби `store=True` |
| `filtered` на великому наборі | фільтруй доменом у `search` |

```python
# ❌ N+1: search виконується для кожного партнера
for p in partners:
    orders = env['sale.order'].search([('partner_id', '=', p.id)])

# ✅ один запит
orders = env['sale.order'].search([('partner_id', 'in', partners.ids)])
orders.grouped('partner_id')     # групування в пам'яті
```

---

## 14. Як читати код ядра Odoo

Це найцінніша навичка. Рецепти:

```bash
# Де оголошено поле?
grep -rn "credit_limit = fields" vendor/odoo/addons/ --include="*.py"

# Які поля є в моделі?
grep -n "= fields\." vendor/odoo/odoo/addons/base/models/ir_cron.py

# Хто успадковує модель?
grep -rln "_inherit = 'sale.order'" vendor/odoo/addons/ --include="*.py"

# Як виглядає базове подання (для xpath)?
sed -n '142,330p' vendor/odoo/odoo/addons/base/views/res_partner_views.xml

# Де визначено XML ID?
grep -rn 'id="view_partner_form"' vendor/odoo/ --include="*.xml"
```

Через UI в режимі розробника:

* **Technical → Models** — усі моделі й поля з атрибутами (`Stored`, `Groups`);
* **Technical → Fields** — фільтр по моделі;
* **🐞 → View Metadata / Edit View** — фінальний злитий XML подання.

---

## 15. Шпаргалка помилок

| Помилка | Причина |
|---------|---------|
| `Compute method failed to assign` | обчислювач не присвоїв значення всім записам |
| `External ID not found` | посилання на запис, оголошений **нижче** у файлі, або неправильний `model_id` |
| `Invalid field X on model Y` | поля не існує (або модуль не оновлено) |
| `Field 'X' used in view is already present` | поле вже є в базовому поданні |
| `ParseError` у XML | синтаксис; часто `--` у коментарі або видалене поле |
| `Non-stored field X cannot be searched` | у **логах**, а не у виключенні — фільтр мовчки ігнорується |
| `AccessError` при читанні поля | поле має `groups=`, а користувач не в групі |
| `Address already in use` при тестах | тестовий режим піднімає HTTP-сервер — дай інший `--http-port` |
| `inconsistent 'store' for computed fields` | змішав `store=True` і `store=False` в одному обчислювачі |

---

**Далі:** [`03-module-base.md`](03-module-base.md) — практика: пишемо перший модуль.
