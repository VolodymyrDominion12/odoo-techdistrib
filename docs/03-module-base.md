# 03. Модуль 1 — `techdistrib_base`: розширюємо стандартну модель

> **Мета уроку:** навчитися додавати власні поля в **існуючу** модель Odoo
> (`res.partner`) і показувати їх в інтерфейсі, не ламаючи стандартну логіку.
>
> **Закриває вимоги:** FR-A03 … FR-A07 з [бізнес-вимог](01-business-requirements.md).

---

## 0. Що ми будуємо

Бізнес-потреба звучить так (з BRD):

> Менеджер не бачить, хто з партнерів є дилером, який у нього рівень, коли він
> востаннє купував і чи не час його понизити. Дилерська програма ведеться
> в Excel і розходиться з реальністю.

Розв'язок — 4 нові поля та автоматика:

| Що | Навіщо |
|----|--------|
| `is_dealer` | Відрізнити B2B-дилера від звичайного клієнта |
| `dealer_code` (`DLR-00042`) | Унікальний ідентифікатор для документів і 1С-інтеграції |
| `dealer_tier_id` → новий довідник | Рівні Bronze/Silver/Gold/Platinum. На них спиратиметься матриця знижок |
| `dealer_since`, `last_sale_date`, `dealer_status` | Стаж, активність, автоматичний статус |
| cron-задача | Раз на добу понижує рівень «сплячим» дилерам |

**Ключове архітектурне рішення:** рівні — це **окрема модель**, а не
`fields.Selection`. Причина в BRD: у рівня є атрибути (мінімальний оборот,
опис умов), і він має бути редагованим без програміста.

---

## 1. Структура модуля

```
custom_addons/techdistrib_base/
├── __manifest__.py                     ← паспорт
├── __init__.py                         ← імпорт пакетів
├── models/
│   ├── __init__.py
│   ├── dealer_tier.py                  ← НОВА модель (довідник)
│   └── res_partner.py                  ← РОЗШИРЕННЯ наявної моделі
├── security/
│   ├── techdistrib_security.xml        ← групи доступу
│   └── ir.model.access.csv             ← права на модель
├── data/
│   ├── ir_sequence_data.xml            ← лічильник DLR-00001
│   ├── dealer_tier_data.xml            ← початкові рівні
│   └── ir_cron_data.xml                ← нічна задача
├── views/
│   ├── dealer_tier_views.xml
│   ├── res_partner_views.xml
│   └── menus.xml
└── tests/
    ├── __init__.py
    └── test_dealer_program.py          ← 18 тестів
```

Прочитай код у цих файлах — він прокоментований рядок за рядком.

---

## 2. Головна концепція: два види наслідування

Це найважливіше, що треба зрозуміти в цьому уроці.

### 2.1. Наслідування МОДЕЛІ (Python)

```python
class ResPartner(models.Model):
    _inherit = 'res.partner'        # ← без _name!
```

* нова таблиця **не** створюється;
* колонки додаються в `res_partner` через `ALTER TABLE`;
* усі стандартні модулі одразу бачать нові поля;
* методи можна перевизначати через `super()`.

**Пастка:** якщо написати `_inherit` **і** `_name` з однаковим значенням —
Odoo зрозуміє це як «розширення» (це нормально). Але якщо `_name` буде іншим —
це вже **делегація**: створюється нова таблиця з копією полів, і твоя модель
перестає бути тим самим `res.partner`.

### 2.2. Наслідування ПОДАННЯ (XML)

```xml
<field name="inherit_id" ref="base.view_partner_form"/>
<field name="arch" type="xml">
    <page name="sales_purchases" position="inside"> ... </page>
</field>
```

Стандартна форма **не замінюється**, а «доповнюється» — Odoo зливає XML
базового подання й нашого.

### 2.3. Ментальна модель

```
   Наш модуль                    Стандартний Odoo
   ──────────                    ────────────────
   ResPartner(_inherit)  ──────► res.partner  (одна таблиця!)
   arch (inherit_id)     ──────► view_partner_form (один злитий XML)
```

Наслідування в Odoo — це **не** копіювання. Це завжди «одна сутність,
доповнена збоку». Саме тому оновлення Odoo не ламає твій код: ти нічого
не переписував у `vendor/odoo/`.

---

## 3. Покроково: що і в якому порядку писати

### Крок 1. Довідник рівнів — `models/dealer_tier.py`

Чому модель, а не Selection:

```python
# ❌ Погано: новий рівень = правка коду + перезапуск сервера
dealer_tier = fields.Selection([('bronze', 'Bronze'), ('gold', 'Gold')])

# ✅ Добре: рівень — це запис у базі, зі своїми атрибутами
class TechdistribDealerTier(models.Model):
    _name = 'techdistrib.dealer.tier'
    name = fields.Char(required=True)
    code = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    min_annual_revenue = fields.Monetary(currency_field='currency_id')
```

**Правило вибору:** якщо в сутності є хоч один атрибут або хоч найменша
ймовірність, що бізнес захоче її редагувати — роби модель.

Зверни увагу на `_sql_constraints`:

```python
_sql_constraints = [
    ('code_uniq', 'unique(code)', 'Код рівня має бути унікальним!'),
]
```

Це обмеження **на рівні бази**. Воно спрацює навіть при конкурентних
запитах, чого не гарантує `@api.constrains` (Python-перевірка між
`SELECT` і `INSERT` має вікно гонки).

### Крок 2. Розширення партнера — `models/res_partner.py`

#### Поле `dealer_code` — три атрибути, які легко забути

```python
dealer_code = fields.Char(
    readonly=True,   # заповнює система
    copy=False,      # ← БЕЗ ЦЬОГО отримаєш баг при дублюванні!
    index=True,      # по ньому шукаємо
)
```

`copy=False` — критично. Без нього `Duplicate` на картці партнера скопіює
код, і два партнери матимуть `DLR-00042`. SQL-обмеження це зловить, але
користувач побачить незрозумілу помилку замість роботи.

#### Генерація коду через послідовність

```python
@api.model
def _next_dealer_code(self):
    return self.env['ir.sequence'].next_by_code('techdistrib.dealer') or '/'
```

**Ніколи не роби так:**
```python
code = f'DLR-{self.search_count([("is_dealer", "=", True)]) + 1:05d}'   # ❌
```
Це класична гонка: два менеджери в двох вкладках отримають однаковий номер.
`ir.sequence` бере номер атомарно на рівні бази.

#### `create` і `write` — обидва потрібні

```python
@api.model_create_multi                 # ← множина, а не один vals!
def create(self, vals_list):
    for vals in vals_list:
        if vals.get('is_dealer') and not vals.get('dealer_code'):
            vals['dealer_code'] = self._next_dealer_code()
    return super().create(vals_list)

def write(self, vals):
    if vals.get('is_dealer'):           # партнера зробили дилером пізніше
        ...
    return super().write(vals)
```

Три речі, які тут легко зламати:

1. **`@api.model_create_multi`** — в Odoo 13+ `create` приймає **список**
   словників. Старий `def create(self, vals)` працює при створенні з UI,
   але падає при імпорті чи масовому дублюванні.
2. **`super()`** — забудеш, і запис просто не створиться, без помилки.
   Це найважче для діагностики.
3. **`write`** — сценарій «зробити наявного клієнта дилером» іде саме через
   `write`, а не `create`. Якщо обробити лише `create`, кнопка на формі
   залишить партнера без коду.

### Крок 3. Обчислювані поля: `store=True` чи ні

Це рішення, яке треба приймати свідомо:

| Питання | `store=True` | без `store` |
|---------|-------------|-------------|
| Чи можна шукати доменом? | ✅ так | ❌ ні (потрібен метод `search=`) |
| Чи можна групувати/сортувати? | ✅ так | ❌ ні |
| Чи завжди актуальне? | ⚠️ лише коли змінилась залежність | ✅ при кожному читанні |
| Чи займає місце? | ✅ колонка + індекс | ❌ ні |

У нас:

* `last_sale_date`, `dealer_confirmed_sales` → `store=True`, бо по
  `last_sale_date` **шукає cron-задача** доменом;
* `dealer_experience_months`, лічильники замовлень → без `store`, бо вони
  лише показуються й залежать від «сьогодні» або рахуються дешево.

### Крок 4. Безпека

Два рівні, обидва обов'язкові:

| Файл | Що визначає | Гранулярність |
|------|-------------|---------------|
| `security/techdistrib_security.xml` | **Групи** (ролі) | «хто є ким» |
| `security/ir.model.access.csv` | **Доступ до моделі** | «кому можна чіпати таблицю» |
| `ir.rule` (закоментовано в XML) | **Доступ до рядків** | «які саме записи видно» |

Формат CSV — жорсткий, 8 колонок:

```csv
id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink
access_techdistrib_dealer_tier_user,techdistrib.dealer.tier.user,model_techdistrib_dealer_tier,group_dealer_user,1,0,0,0
```

**Як формується `model_id:id`:** префікс `model_` + назва моделі, у якій
крапки замінено на підкреслення. `techdistrib.dealer.tier` →
`model_techdistrib_dealer_tier`. Помилка тут дає
`External ID not found` — і це одна з найчастіших помилок новачків.

**Чому немає `groups` на меню:** адміністратор, який не є членом групи,
теж не побачив би пункт меню й вирішив би, що модуль не встановився.
Права перевіряються на рівні моделі — цього достатньо.

### Крок 5. `noupdate="1"` — коли обов'язково

```xml
<odoo noupdate="1">
    <record id="seq_techdistrib_dealer" model="ir.sequence"> ... </record>
</odoo>
```

| Дані | `noupdate` | Чому |
|------|-----------|------|
| Лічильники (`ir.sequence`) | **1** | Інакше оновлення модуля скине номер і видасть дубль коду |
| Довідники, які редагує бізнес | **1** | Правки бізнесу не мають затиратись |
| Views, actions, меню | 0 | Мають оновлюватись разом із кодом |
| Права доступу | 0 | Те саме |

### Крок 6. Views: як вибрати правильний `xpath`

Це місце, де помиляються найчастіше. Рецепт:

1. Увімкни режим розробника.
2. Відкрий потрібну форму → іконка 🐞 → **View Metadata**.
3. Знайди в XML «якір» — елемент з атрибутом `name`.
4. Напиши `xpath` по цьому якорю.

**Реальний приклад моєї помилки.** Я спершу написав для фільтрів пошуку:

```xml
<xpath expr="//filter[@name='salesperson']" position="after"> ... </xpath>
```

А потім прочитав базовий XML:

```xml
<search string="Search Partner">
    <field name="name"/> ... <field name="user_id"/>
    <filter name="inactive"/>                              ← звичайний фільтр
    <group name="group_by" string="Group By">
        <filter name="salesperson" .../>                   ← ЦЕ ГРУПУВАННЯ!
    </group>
</search>
```

Тобто мій xpath вставив би **фільтри** туда, де мають бути **групування**.
Правильно:

```xml
<!-- фільтри — після останнього звичайного фільтра -->
<xpath expr="//filter[@name='inactive']" position="after"> ... </xpath>
<!-- групування — усередину групи -->
<xpath expr="//group[@name='group_by']" position="inside"> ... </xpath>
```

**Висновок:** перш ніж писати `xpath` — відкрий реальний XML базового подання.
Він лежить у `vendor/odoo/odoo/addons/base/views/res_partner_views.xml`.

#### Пастка з дублюванням полів

`account` уже додає у форму партнера `<field name="currency_id" invisible="1"/>`.
Якби я додав його ще раз, отримав би
`Field 'currency_id' used in view is already present`.

Тому для грошового поля використовуємо той самий патерн, що й `account`:

```xml
<field name="dealer_confirmed_sales" widget="monetary"
       options="{'currency_field': 'currency_id'}"
       invisible="not is_dealer"/>
```

#### `attrs` більше не існує

```xml
<!-- ❌ Odoo ≤16 -->
<field name="dealer_code" attrs="{'invisible': [('is_dealer','=',False)]}"/>
<!-- ✅ Odoo 17+ -->
<field name="dealer_code" invisible="not is_dealer"/>
```

`attrs` **видалено** в Odoo 17. Вирази тепер пишуться як звичайний Python:
`invisible="not is_dealer"`, `readonly="dealer_status == 'dormant'"`.

### Крок 7. Cron-задача і проблема «часових залежностей»

Найцікавіша концептуальна частина уроку.

`dealer_status` — збережене обчислюване поле. Воно залежить від
`last_sale_date`. Але логіка каже: «сплячий = не купував 6 місяців».
**6 місяців від чого? Від сьогоднішньої дати.** А сьогоднішня дата — це не
поле, її немає в графі залежностей.

**Наслідок:** дилер, який учора був «активним», сьогодні залишиться
«активним» назавжди — бо ніщо не змінилось і перерахунок не запустився.

Стандартне рішення Odoo — cron-задача:

```python
@api.model
def _cron_update_dealer_status(self):
    dealers = self.search([('is_dealer', '=', True)])
    self.env.add_to_compute(self._fields['dealer_status'], dealers)
    # ... далі зниження рівня
```

`add_to_compute(field, records)` — офіційний спосіб сказати ORM «познач ці
записи для перерахунку». **Не** викликай метод-обчислювач напряму
(`partner._compute_dealer_status()`): це оминає систему кешування ORM.

Подивись, як це зроблено в стандартному Odoo — модуль `membership`,
`addons/membership/models/partner.py`, метод `_cron_update_membership`.
Абсолютно той самий патерн.

#### Ідемпотентність cron

Задача виконується за розкладом і може запуститись двічі (перезапуск
сервера, ручний запуск, кілька воркерів). Тому:

```python
lower_tiers = tiers.filtered(lambda t: t.sequence < current.sequence)
if not lower_tiers:
    continue            # уже найнижчий — нічого не робимо
new_tier = lower_tiers[-1]      # знижуємо РІВНО на один щабель
```

Дилер не «впаде» з Platinum одразу в Bronze за одну ніч. Повторний запуск
безпечний.

### Крок 8. Тести

18 тестів у `tests/test_dealer_program.py`. Запуск:

```bash
./scripts/run_tests.sh techdistrib_base
```

Що важливо знати:

* `TransactionCase` **відкочує транзакцію** після кожного тесту →
  тому в тестах **ніколи** не викликай `env.cr.commit()`;
* `@tagged('post_install')` — тести бізнес-логіки запускають після
  встановлення всіх модулів, коли оточення реалістичне;
* методи з іменем `test_*` — це тести, решта — допоміжні.

Цікава технічна знахідка з тестів:

```python
order.action_confirm()
order.date_order = fields.Datetime.now() - relativedelta(months=8)
```

`action_confirm()` **перезаписує** `date_order` поточним часом. Тому
«створити старе замовлення наперед» не вийде — дату треба пересувати **після**
підтвердження. Поле `readonly=True` — це обмеження **інтерфейсу**, із Python
писати можна.

---

## 4. Шість реальних помилок цього уроку (і як їх читати)

Це найцінніший розділ. Усі помилки нижче я зробив по-справжньому, і всі вони
трапились у коді цього модуля.

### Помилка 1. `numbercall` і `doall` видалено в Odoo 18

```
odoo.tools.convert.ParseError: while parsing .../data/ir_cron_data.xml:24
```

**Що сталося:** я написав cron за зразком Odoo 17, де є поля `numbercall`
і `doall`. У 18 їх видалили (модель `ir.cron` тепер наслідує `ir.actions.server`).

**Як знайти правду за 5 секунд:**
```bash
grep "= fields\." vendor/odoo/odoo/addons/base/models/ir_cron.py
```

**Мораль:** найшвидший довідник по API — не документація, а вихідний код.

### Помилка 2. `--` всередині XML-коментаря

```
lxml.etree.XMLSyntaxError: Double hyphen within comment
```

Я прикрасив коментар: `<!-- --- Smart-кнопка --- -->`. У XML коментар
**не може містити `--`**. Це вимога стандарту, не примха Odoo.

**Перевірка всіх модулів одразу:**
```bash
python3 -c "
import re,pathlib
for p in pathlib.Path('custom_addons').rglob('*.xml'):
    t=p.read_text()
    for m in re.finditer(r'<!--(.*?)-->',t,re.S):
        if '--' in m.group(1): print(p, t[:m.start()].count(chr(10))+1)
"
```

### Помилка 3. Коментар каже одне, код робить інше

Я написав:
```python
    # store=True — значення ЗБЕРІГАЄТЬСЯ в колонці...
    last_sale_date = fields.Date(compute='...', readonly=True)
```
…і **забув сам параметр `store=True`**.

Наслідок підступний: модуль встановився **без помилки**, поле є у формі,
але колонки в базі немає. А cron використовує `last_sale_date` у домені —
і впав би з `Non-stored field cannot be searched`.

**Як зловити:**
```sql
SELECT column_name FROM information_schema.columns WHERE table_name='res_partner';
-- або в UI: Technical → Fields → res.partner → колонка Stored
```

### Помилка 4. Змішування stored і non-stored в одному обчислювачі

```
UserWarning: res.partner: inconsistent 'store' for computed fields,
accessing dealer_confirmed_order_count may recompute and update last_sale_date.
```

Спочатку один метод рахував і збережені, і незбережені поля. Odoo попередив.

**Чому це небезпечно:** незбережене поле перераховується при кожному читанні,
а метод заразом **перезаписує** збережені поля. Виникають несподівані
`UPDATE` під час звичайного перегляду списку.

**Виправлення** (зроблено): два окремі методи —
`_compute_dealer_stored_metrics` і `_compute_dealer_live_metrics`.

> **Зверни увагу:** Odoo **не зупинив** встановлення. Він попередив.
> Якби я не читав логи, цей баг поїхав би в продакшен. **Читай warnings.**

### Помилка 5. Неправильний якір `xpath`

Описано вище в розділі «Views». Скорочено: фільтр `salesperson` лежить
усередині `group_by`, тому `position="after"` вставив би фільтри не туди.

### Помилка 6. Дублювання поля у формі

`account` уже додає `currency_id` у форму партнера. Повторне додавання →
помилка валідації подання.

**Як перевірити, хто що додає:**
```bash
grep -rl 'base.view_partner_form' vendor/odoo/addons/ --include="*.xml" \
  | xargs grep -l 'currency_id'
```

---

## 5. Як перевірити результат

### У базі
```sql
SELECT name, state FROM ir_module_module WHERE name='techdistrib_base';
SELECT column_name FROM information_schema.columns
 WHERE table_name='res_partner' AND column_name LIKE '%dealer%';
SELECT name, code, sequence FROM techdistrib_dealer_tier ORDER BY sequence;
```

### В інтерфейсі

1. Запусти сервер: `./scripts/run_odoo.sh`
2. Відкрий http://localhost:8069, логін `admin` / `admin`
3. Меню **TechDistrib → Дилерська програма → Рівні партнерів** —
   побачиш Bronze/Silver/Gold/Platinum.
4. **Дилери → Створити** — код `DLR-00001` присвоїться сам.

### Через консоль Odoo (найшвидший спосіб)
```bash
./scripts/odoo_shell.sh -d techdistrib
```
```python
>>> env['techdistrib.dealer.tier'].search([]).mapped('name')
['Platinum', 'Gold', 'Silver', 'Bronze']

>>> p = env['res.partner'].create({'name': 'ТОВ Альфа-Тех', 'is_dealer': True})
>>> p.dealer_code
'DLR-00001'

>>> env.cr.commit()      # ← не забудь, інакше зміни відкочуться
```

### Запуск cron вручну
```python
>>> env['res.partner']._cron_update_dealer_status()
```

---

## 6. Чек-ліст «модуль готовий»

- [ ] `__manifest__.py` має коректну версію `18.0.x.y.z`
- [ ] Усі підпакети імпортовано в `__init__.py` (інакше код «невидимка»)
- [ ] Порядок файлів у `data` манифесту: security → data → views → menus
- [ ] Для лічильників і редагованих довідників стоїть `noupdate="1"`
- [ ] `ir.model.access.csv` покриває **усі** нові моделі
- [ ] Усі `xpath` мають специфічні якорі (`name=` або `page[@name=...]`)
- [ ] Обчислювачі не змішують `store=True` і `store=False`
- [ ] Cron ідемпотентний
- [ ] `./scripts/run_tests.sh <модуль>` — усе зелене
- [ ] При оновленні модуля **немає WARNING-ів** у логах

---

## 7. Вправи для самостійної роботи

1. **Легка.** Додай поле `dealer_website_login` (Char) на картку дилера й
   виведи його в список дилерів. Перевір, що воно не конфліктує зі
   стандартними полями.

2. **Середня.** Додай у модель рівня поле `bonus_percent` (Float) і зроби
   так, щоб воно було **обов'язковим** і не могло бути від'ємним
   (використай `@api.constrains` і `ValidationError`).

3. **Середня.** Зроби так, щоб при зниженні рівня cron-ом створювалась
   **активність** (`mail.activity`) для менеджера дилера: «Зв'язатися з
   дилером — рівень знижено». Підказка: `partner.activity_schedule(...)`.

4. **Складна.** Додай у модель `res.partner` обчислюване **збережене** поле
   `dealer_lifetime_days` (кількість днів від `dealer_since` до сьогодні) і
   зроби так, щоб воно оновлювалось тією ж cron-задачею. Подумай, чому тут
   потрібен саме такий підхід, а не звичайний `@api.depends`.

5. **Складна.** Увімкни закоментоване правило `ir.rule` з
   `security/techdistrib_security.xml` і зроби так, щоб воно працювало
   коректно: менеджер бачить лише своїх дилерів, керівник — усіх.
   Перевір під двома різними користувачами.

---

**Далі:** [`04-module-credit.md`](04-module-credit.md) — найцікавіше:
нова бізнес-логіка, якої в Odoo немає. Кредитні ліміти та блокування
відвантаження.
