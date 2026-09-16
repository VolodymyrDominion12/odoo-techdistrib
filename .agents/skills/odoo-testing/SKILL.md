---
name: odoo-testing
description: Як писати й запускати тести для кастомних модулів Odoo у проєкті odoo-techdistrib — класи тестів у Odoo 18, теги, окрема тестова база, rollback транзакцій, дворівневий підхід (чиста логіка без бази + TransactionCase), типові пастки на кшталт HTTP-сервера в тестовому режимі. Використовувати при додаванні тесту, полагодженні падаючого тесту, зміні бізнес-логіки або коли треба перевірити нову модель чи computed-поле.
whenToUse: Будь-яка зміна бізнес-логіки в custom_addons/ — бо в цьому проєкті модуль без зеленого прогону тестів не вважається готовим.
---

# Тестування в Odoo 18 (проєкт odoo-techdistrib)

## 1. Запуск

```bash
./scripts/run_tests.sh techdistrib_credit                     # усі тести модуля
./scripts/run_tests.sh techdistrib_warranty:TestWarrantyClaim  # один клас
./scripts/run_tests.sh techdistrib_credit:TestCreditLimit.test_blocks_confirmation  # один метод
```

Скрипт виставляє правильні прапорці: `--test-enable` (без нього Odoo **не
запускає тести взагалі**), `-i <module>`, `--stop-after-init`,
`--without-demo=all`, `--log-level=test`.

### Пастка, через яку тести падають із «Address already in use»

У режимі тестів Odoo піднімає HTTP-сервер **завжди**, навіть із `--no-http`:

```python
# vendor/odoo/odoo/service/server.py
if test_mode or (config['http_enable'] and not stop):
    ... start http server ...
```

Тому скрипт передає власний порт (`--http-port=8099`). Якщо колізуєш цю помилку —
перевір, що робочий Odoo на 8069 не заважає, і що порт справді вільний.

## 2. Як це працює (і чому твоя робоча база в безпеці)

1. Odoo створює **окрему** тестову базу (`techdistrib_test`).
2. Встановлює туди модуль і всі залежності.
3. Проганяє класи з `tests/`.
4. **Після кожного тесту відкочує транзакцію.**

Наслідки:
- Тести **не бачать даних одне одного** — кожен починає з чистого стану.
- Робоча база `techdistrib` тестами не псується.
- **`env.cr.commit()` у тестах заборонений** — він ламає ізоляцію і залишає сміття.

## 3. Класи тестів у 18.0 (перевірено по `vendor/odoo/odoo/tests/common.py`)

| Клас | Коли використовувати |
|---|---|
| `BaseCase` | базовий, без транзакції на кожен тест |
| **`TransactionCase`** | **типовий вибір проєкту**: свіжа транзакція на кожен тест |
| `SingleTransactionCase` | одна транзакція на весь клас (швидше, але тести впливають одне на одного) |
| `HttpCase` | тести, що ходять у справжній веб-сервер (тури, контролери) |

**`SavepointCase` у 18.0 НЕ існує** — його видалено. Якщо бачиш його в
прикладі зі статті або від іншого агента — це застаріла порада.

Теги — конвенція проєкту:

```python
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

@tagged('post_install', '-at_install', 'techdistrib')
class TestWarranty(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()          # ← обов'язково
        cls.customer = cls.env['res.partner'].create({'name': 'ТОВ Клієнт'})
```

- `post_install` — тест іде **після** встановлення всіх модулів. Потрібно, коли
  довіряєш `data/`-файлам і зв'язкам між модулями.
- `-at_install` — не ганяти під час встановлення (інакше тест біжить двічі).
- `techdistrib` — власний тег, щоб можна було фільтрувати тільки своє.
- `setUpClass` **мусить** кликати `super().setUpClass()`, інакше немає `cls.env`.

`--without-demo=all` означає: **демо-даних немає**. Не покладайся на
`env.ref('base.demo_...')` — створюй потрібні записи сам у `setUpClass`.

## 4. Дворівневий підхід — головна вимога до тестів

Проєкт уже тримає цю ідею в `techdistrib_warranty/tests/test_warranty.py`
(«юніт-рівень» і «інтеграційний рівень»). Правило:

| Рівень | Що перевіряє | Ціна | Де живе |
|---|---|---|---|
| **Безбазовий** | арифметика, рішення, політики — чиста логіка | мілісекунди | `tests/test_<service>.py` звичайним `unittest` |
| **`TransactionCase`** | ORM, computed, constraints, потік документів, права | секунди–хвилини (підняття бази) | `tests/test_<модель>.py` |

**Безбазовий рівень з'явиться лише тоді, коли логіку винесено в `policy/`**
без `self.env` (див. скіл `odoo-architecture` §2). Це і є практична користь від
того винесення: арифметику гарантійних дат можна перевірити 20 кейсами за
мілісекунди замість 20 запусків тестової бази.

Не перенось у безбазовий рівень те, що потребує ORM: computed-поля, `@api.depends`,
`@api.constrains`, правила записів — це за визначенням інтеграційні тести.

## 5. Що обов'язково тестувати

1. **Computed-поля** — і значення, і **перерахунок** після зміни залежності.
   Найчастіший баг: `@api.depends` не покриває поле, і значення «застигає».
2. **`@api.constrains`** — валідний випадок проходить, невалідний кидає
   `ValidationError` (`with self.assertRaises(ValidationError):`).
3. **Ланцюжки між моделями** — те, що код правильний, але **не підключений** до
   потоку Odoo. Саме це ловить інтеграційний рівень і не ловить юніт-рівень.
4. **Заблоковані дії** — `UserError` там, де дія мусить бути заборонена
   (кредитний ліміт, гарантія поза терміном).
5. **Права доступу** — нова модель має рядок в ACL; критичні поля закриті групами.

Чого **не** варто тестувати: код ядра Odoo, геттери/сетери без логіки,
константи, і `_description` (лінтер це вже перевіряє).

## 6. Приватні методи в тестах — це нормально

Тести навмисно кличуть `_cron_update_dealer_status()`, `_cron_expire_credit_approvals()`
тощо. У Python це «protected access», і `pylint` це позначить (`W0212`) — але в
цьому проєкті правило вимкнено в `.pylintrc` саме тому, що перевіряти поведінку
cron-методу напряму — правильний спосіб його протестувати.

Так само вимкнено `invalid-name` (`setUpClass` — це API `unittest`, camelCase
там обов'язковий) і `too-many-public-methods` (клас тестів законно містить
багато тестів). Не «виправляй» ці спрацьовування — вони вже вирішені конфігом.

## 7. 🔴 Тести біжать під superuser — тому ACL і правила записів НЕ перевіряються

Це найважливіше, що треба знати про тести в Odoo, і воно неочевидне.

Odoo створює тестове середовище під **`SUPERUSER_ID` (uid 1)**:

```python
# vendor/odoo/odoo/tests/common.py:1070 (TransactionCase)
cls.env = api.Environment(cls.cr, odoo.SUPERUSER_ID, {})
# там же, рядок 129:
ADMIN_USER_ID = odoo.SUPERUSER_ID      # «адмін» у тестах — це літерально superuser
```

**uid 1 обходить і `ir.model.access`, і `ir.rule`.** Наслідки:

- Тести **не здатні** виявити, що модель недоступна якійсь групі, що правило
  записів забороняє не те, або що ACL рядок узагалі забули додати.
- Зелений прогін тестів **не означає**, що реальний користувач щось побачить.

Це не теорія: у цьому проєкті ACL кастомних моделей дають права лише групам
`group_dealer_user` / `group_dealer_manager` / `group_credit_controller`, а
користувач `admin` у них не перебуває — і жоден файл безпеки його туди не
додає. Тести цього не бачать; виявилось це лише через Odoo MCP (див. скіл
`odoo-mcp` §3).

### Як тестувати права доступу

Перемкни середовище на **справжнього** користувача. В Odoo 18 для цього є
готовий хелпер (`vendor/odoo/odoo/tests/common.py:191`):

```python
from odoo.tests.common import TransactionCase, new_test_user

@tagged('post_install', '-at_install', 'techdistrib')
class TestWarrantyAccess(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.plain_user = new_test_user(
            cls.env, login='plain', groups='base.group_user')
        cls.manager = new_test_user(
            cls.env, login='mgr',
            groups='base.group_user,techdistrib_base.group_dealer_manager')

    def test_plain_user_cannot_read_claims(self):
        # env(user=...) — перемикання користувача (той самий механізм,
        # що й у сеттері uid, vendor/odoo/odoo/tests/common.py:402)
        with self.assertRaises(AccessError):
            self.env['techdistrib.warranty.claim'].with_user(
                self.plain_user).search([])

    def test_manager_can_read_claims(self):
        claims = self.env['techdistrib.warranty.claim'].with_user(
            self.manager).search([])
        self.assertEqual(len(claims), 0)
```

Правило проєкту: **кожна нова модель отримує тест на доступ** — окрім тесту
логіки. Без нього дірка в правах лишається невидимою доти, доки хтось не
спробує нею скористатись.

## 7.1. Форма, кеш і середовище записів — три речі, які легко зробити неправильно

### `Form` — єдиний спосіб протестувати `@api.onchange`

Звичайне присвоєння `record.field = x` **не викликає** `onchange` — ані в тесті,
ані в скрипті. Для цього в Odoo є серверна реалізація форми,
`odoo.tests.Form` (`vendor/odoo/odoo/tests/form.py`): вона викликає onchange при
створенні й на кожне встановлення поля, а також обробляє `default_*`.

У проєкті є `@api.onchange('lot_id')` у `techdistrib_warranty` — і в
`test_warranty.py` вже правильно зазначено, що покладатись лише на onchange не
можна. Але щоб перевірити **сам** onchange (наприклад, що він підставляє
`reported_date`), потрібен саме `Form`:

```python
from odoo.tests import Form

def test_onchange_fills_dates(self):
    with Form(self.env['techdistrib.warranty.claim']) as f:
        f.lot_id = self.lot                     # ← спрацьовує @api.onchange
        self.assertEqual(f.reported_date, fields.Date.context_today(f.record))
    claim = f.record                            # збережений запис
```

Для x2many всередині форми: `with f.order_line.new() as line:` (створити рядок)
або `with f.order_line.edit(0) as line:` (редагувати наявний).

### Кеш ORM: `flush_all`, `invalidate_model` — і жодного `invalidate_cache`

ORM кешує значення полів, тому щойно записане може не перерахуватись, доки не
відбудеться flush. У 18.0 API такий:

- `self.env.flush_all()` · `recordset.flush_model()` · `recordset.flush_recordset()`
- **`recordset.invalidate_model()`** — а `invalidate_cache()` **у 18.0 не існує**.
  Будь-яка порада його викликати — застаріла.

Обов'язково перед сирим SQL у тесті (інакше читаєш старі рядки) і тоді, коли
перевіряєш результат «обхідним» шляхом, а не через ORM.

### Пастка середовища записів: `setUpClass` запам'ятовує `env`

Запис, створений у `setUpClass`, **назавжди зберігає своє `env`** — uid, стан
`sudo` і контекст. Тому в тесті прав, де ти перемикаєшся на звичайного
користувача, фікстура з `setUpClass` поводитиметься як superuser, і ти
отримаєш **хибно-зелений** тест.

Лікування — `with_env`:

```python
def test_manager_sees_own_claims(self):
    claim = self.claim.with_env(self.env)   # ← привести до поточного env
    ...
```

Те саме стосується `with_user`/`with_company`: вони створюють **новий** env, і
якщо всередині покладаєшся на фікстуру з `setUpClass`, її env не зміниться.


## 8. Типові пастки

- **`env.cr.commit()`** у тесті → ламає ізоляцію. Заборонено.
- **Забути `super().setUpClass()`** → `cls.env` не існує.
- **Покладатися на демо-дані** → їх немає (`--without-demo=all`).
- **Спільний стан між тестами одного класу** через атрибути класу — кожен тест
  має свою транзакцію, але створені в `setUpClass` записи спільні й **можуть
  змінюватись** тестами. Не мутуй фікстури з `setUpClass`.
- **Порядок тестів** залежить від імен методів — не будуй тест на тому, що
  «попередній уже щось створив».
- **`cls.env.ref('інший_модуль.запис')`** падає, якщо той модуль не в
  `depends`. Міжмодульні посилання — це зайвий привід перевірити `depends`.
- **Тест «зелений», бо нічого не перевіряє.** `create()` без `assert` — не тест.
  Мінімум: ствердження на значення **і** на очікуваний виняток.

## 9. Definition of Done для зміни в модулі

```bash
./scripts/run_tests.sh <module>     # зелено
./scripts/lint.sh <module>          # без НОВИХ зауважень відносно baseline
```

Плюс: якщо додав модель — рядок в `ir.model.access.csv` **і тест на доступ**;
якщо додав файл у `data` — він існує; якщо змінив бізнес-правило — є тест,
який падає без цієї зміни; якщо модель має читати `admin` — він справді в
потрібній групі (перевіряй через MCP, а не через тести).
