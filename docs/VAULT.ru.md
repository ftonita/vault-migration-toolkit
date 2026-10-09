# Работа с Vault

[🇬🇧 English](VAULT.md) | 🇷🇺 **Русский**

В этом руководстве: что именно инструмент пишет в Vault, как подготовить Vault (локальная репетиция и боевой контур), какая политика нужна токену миграции, как запустить и проверить миграцию, как приложения потом читают секреты и как откатиться. Команды `vault-migrate` и `vault` ниже выполнены на Vault 1.17 в dev-режиме с файлами из [`examples/`](../examples); фрагменты для Vault Agent и Kubernetes в разделе 6 — нет.

## 1. Что оказывается в Vault

| Понятие | Значение |
|---|---|
| Secrets engine | **KV версии 2**, по одному mount'у на команду: `kv-<team>/` (префикс из `mount_prefix`) |
| Путь | `kv-<team>/<env>/<app>/<name>`, например `kv-payments/prod/orders/db_password` |
| Данные | ровно один ключ: `{"value": "<секрет>"}` |
| Пользовательские метаданные | `legacy_id`, `kind`, `last_rotated` (если известна), `migrated_by=vault-migration-toolkit` |
| Версии | первая запись — версия 1 с `cas=0` (только создание); `--overwrite` пишет следующую версию с CAS на текущую |

HTTP API KV v2 вставляет после mount'а `data/` (значения) или `metadata/` (метаданные): секрет выше читается как `GET /v1/kv-payments/data/prod/orders/db_password`. CLI `vault kv` с `-mount=` скрывает эту деталь.

## 2. Репетиция на локальном Vault

```bash
docker compose -f examples/vault/docker-compose.yml up -d     # или: vault server -dev -dev-root-token-id=dev-root-token
export VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN=dev-root-token

A="--source examples/legacy.json --rules examples/rules.yml --state rehearsal-state --backend http"
vault-migrate apply  $A --create-mounts                        # пробный прогон: would_write=8, создаст 3 mount'а
vault-migrate apply  $A --execute --create-mounts              # written=8
vault-migrate apply  $A --execute                              # skipped=8 (идемпотентно)
vault-migrate verify $A                                        # verified=8 mismatched=0 missing=0
vault kv get -mount=kv-payments prod/orders/db_password
```

Dev-сервер хранит данные в памяти и стартует распечатанным (unsealed) с root-токеном: только для синтетических данных. Репетируйте с копией своего настоящего `rules.yml` и **синтетической** выгрузкой той же структуры.

## 3. Подготовка рабочего Vault

### 3.1 Mount'ы

Есть два варианта:

- Пусть их создаст инструмент: `--create-mounts` включает `kv-<team>/` как KV v2 для каждой команды из плана.
- Создать самим (Terraform, платформенная команда). Обычно это предпочтительнее, потому что можно задать параметры вроде `max_versions` или `cas_required`:

  ```bash
  vault-migrate plan --source legacy.json --rules rules.yml | grep -o '`kv-[^/]*' | tr -d '`' | sort -u  # mount'ы из плана
  vault secrets enable -path=kv-payments -version=2 kv
  ```

Пробный прогон уже проверяет mount'ы. Без `--create-mounts` он сообщает о каждом отсутствующем строкой `missing mount: kv-x/ (N secrets; create it, or add --create-mounts)` и завершается с кодом 1. С `--create-mounts` выводит `would create mount: kv-x/`.

### 3.2 Политика для токена миграции

[`examples/vault/migration-policy.hcl`](../examples/vault/migration-policy.hcl) — минимальная политика; CI прогоняет `examples/` с не-root токеном, ограниченным ею:

| Путь | Права | Зачем |
|---|---|---|
| `sys/mounts/kv-*` | `create`, `update` | Только с `--create-mounts` |
| `kv-<team>/data/*` | `create`, `read`, `update` | `create` — новые секреты; `read` — идемпотентность и `verify`; `update` — `--overwrite` |
| `kv-<team>/metadata/*` | `create`, `read`, `update` | `custom_metadata` пишется вторым запросом |

Для миграции в существующие mount'ы доступ к `sys/*` не нужен: отсутствие mount'а распознаётся по ответу Vault на чтение самого секрета (404 `no handler for route`), поэтому инструмент вообще не обращается к `sys/mounts`.

Vault допускает `*` только в конце пути политики, поэтому `kv-*/data/*` не сработает: добавьте пару блоков `data` + `metadata` на каждый mount команды (в примере их три). Для одного только `verify` достаточно `read` на `data/*`.

```bash
vault policy write vault-migration examples/vault/migration-policy.hcl
export VAULT_TOKEN=$(vault token create -policy=vault-migration -ttl=8h \
                     -display-name=vault-migration -field=token)
```

Берите короткий TTL на время окна миграции и отзовите токен после (раздел 7).

### 3.3 Параметры подключения

| Переменная | Смысл |
|---|---|
| `VAULT_ADDR` | **Обязательна.** Только `https://...`; обычный `http` разрешён лишь для `127.0.0.1` / `localhost`. |
| `VAULT_TOKEN` | **Обязательна.** Токен миграции. Другие методы аутентификации (AppRole, OIDC, ...): войдите через CLI `vault` и экспортируйте полученный токен. |
| `VAULT_NAMESPACE` | Необязательна, namespace Vault Enterprise (передаётся как `X-Vault-Namespace`). Тестами не покрыта. |
| `SSL_CERT_FILE` | Набор CA для корпоративного центра сертификации. Клиент использует стандартное хранилище доверия Python; **`VAULT_CACERT` не читается.** |

Таймаут запроса — 10 с; ответы 5xx и сетевые ошибки повторяются 3 раза с нарастающей задержкой.

## 4. Запуск миграции

```bash
export VAULT_ADDR=https://vault.example.com VAULT_TOKEN=...      # токен миграции из 3.2

vault-migrate inventory --source legacy.json --state migration-state --out inventory.md   # без Vault
vault-migrate plan      --source legacy.json --rules rules.yml --out plan.md            # без Vault

A="--source legacy.json --rules rules.yml --state migration-state --backend http"
vault-migrate apply  $A              # пробный прогон против настоящего Vault: would_write / skipped / conflicts
vault-migrate apply  $A --execute    # запись
vault-migrate verify $A              # чтение обратно и сравнение отпечатков
```

Перед `--execute` согласуйте `inventory.md` и `plan.md` с владельцами секретов.

Коды возврата: `0` — успех; `1` — конфликты, сбои, расхождения или отсутствующие секреты (а также `inventory --fail-on` при находке высокой критичности); `2` — некорректный вход или ошибка Vault/конфигурации (сообщение в stderr).

### Каталог состояния

`migration-state/` содержит `fingerprint.key` (случайный HMAC-ключ, `0600`) и `ledger.jsonl` (по строке на действие: id, путь, статус, 8 символов отпечатка, время; значений нет). Используйте **один и тот же** каталог состояния для всех запусков одной миграции: отпечатки с разными ключами несравнимы.

```bash
jq -r 'select(.status=="failed" or .status=="conflict") | [.secret_id, .target, .detail] | @tsv' migration-state/ledger.jsonl
```

### Сбои, продолжение, конфликты

- **Продолжение после сбоя:** просто запустите `apply --execute` ещё раз. Уже перенесённые секреты сравниваются с содержимым Vault и получают статус `skipped`; записываются только недостающие.
- **Конфликт:** по целевому пути уже лежит другое значение (кто-то создал его вручную или две записи выгрузки указывают на один путь). Ничего не перезаписывается. Выясните, какое значение верное; если из старого хранилища — запустите `apply --execute --overwrite` (CAS на текущую версию, старая остаётся в истории).
- **Метаданные:** `custom_metadata` пишутся вторым запросом. Если он не прошёл, значение уже в Vault, а секрет помечается как `failed: <id> (value written as version N, but ...; re-run to repair the metadata)`. После устранения причины повторный запуск находит совпадающее значение и дописывает недостающие метаданные (`metadata_repaired=N`, статус в журнале `metadata_written`). Уже существующие ключи сохраняются.
- **Мягко удалённый секрет** (`vault kv delete`): `apply` считает его конфликтом, `verify` — отсутствующим (`missing`); `--overwrite` пишет следующую версию.
- **Ручная очередь:** записи, которые `plan` не смог сопоставить. Исправьте данные или `rules.yml` (алиасы, `app_owners`, `allowed_envs`) и снова запустите `plan`; не правьте целевые пути вручную.

## 5. Проверка результата в Vault

```bash
vault secrets list | grep kv-                                         # mount'ы
vault kv list -mount=kv-payments prod/orders                          # секреты одного приложения
vault kv get -mount=kv-payments prod/orders/db_password               # значение + метаданные (выводит значение!)
vault kv get -field=value -mount=kv-payments prod/orders/db_password  # только значение
vault kv metadata get -mount=kv-payments prod/orders/db_password      # версии, custom_metadata.legacy_id
```

Основная проверка — `verify`. Команды выше нужны для выборочного контроля; `kv get` выводит значение в терминал.

## 6. Переключение приложений на Vault

Дайте каждому потребителю политику только на чтение своего пути, как в [`examples/vault/app-read-policy.hcl`](../examples/vault/app-read-policy.hcl):

```hcl
path "kv-payments/data/prod/orders/*" {
  capabilities = ["read"]
}
```

Чтение перенесённого значения (всегда ключ `value`):

```bash
# CLI
vault kv get -field=value -mount=kv-payments prod/orders/db_password
# HTTP API: обратите внимание на data/ в пути и .data.data в ответе
curl -s -H "X-Vault-Token: $VAULT_TOKEN" "$VAULT_ADDR/v1/kv-payments/data/prod/orders/db_password" | jq -r .data.data.value
```

Шаблон Vault Agent:

```
{{ with secret "kv-payments/data/prod/orders/db_password" }}{{ .Data.data.value }}{{ end }}
```

Kubernetes с Vault Agent Injector (аннотации пода):

```yaml
vault.hashicorp.com/agent-inject: "true"
vault.hashicorp.com/role: "orders-prod"
vault.hashicorp.com/agent-inject-secret-db_password: "kv-payments/data/prod/orders/db_password"
vault.hashicorp.com/agent-inject-template-db_password: |
  {{ with secret "kv-payments/data/prod/orders/db_password" }}{{ .Data.data.value }}{{ end }}
```

Поле `consumers` в выгрузке показывает, кого нужно переключить. Держите старое хранилище в режиме только для чтения, пока все потребители не перейдут на Vault.

## 7. Откат и очистка

```bash
# вернуть один секрет к предыдущей версии (создаёт новую версию со старыми данными)
vault kv rollback -mount=kv-payments -version=1 prod/orders/db_password

# отменить миграцию целиком: удалить всё, что по журналу было записано (НЕОБРАТИМО, все версии)
jq -r 'select(.status=="written") | .target' migration-state/ledger.jsonl | sort -u |
  while read -r t; do vault kv metadata delete "$t"; done

# после успешного verify и переключения потребителей
vault token revoke "$VAULT_TOKEN"     # токен миграции
shred -u legacy.json                  # выгрузка в открытом виде
rm migration-state/fingerprint.key    # отпечатки в журнале становятся несопоставимыми; ledger.jsonl оставьте для аудита
```

Миграция переносит значения **как есть**. Секреты, которые `inventory` пометил как `weak` или `shared_across_envs`, нужно ротировать в Vault после переключения потребителей.

## 8. Разбор ошибок

Ошибки конфигурации останавливают запуск с `error: ...` в stderr и кодом 2. Внутри `apply` ошибка по отдельному секрету выводится как `failed: <id> (<причина>)`, и запуск продолжается. Та же причина есть в поле `detail` журнала (команда `jq` в разделе 4). Секреты, заблокированные отсутствующим mount'ом, сводятся в одну строку `missing mount:`.

| Сообщение | Причина | Что делать |
|---|---|---|
| `VAULT_ADDR and VAULT_TOKEN must be set` | переменные не экспортированы | `export VAULT_ADDR=... VAULT_TOKEN=...` |
| `refusing to talk to Vault over plain http (except localhost)` | `VAULT_ADDR=http://удалённый-хост` | используйте `https://` |
| `cannot reach Vault: ... CERTIFICATE_VERIFY_FAILED` | корпоративный CA не в доверенных | `export SSL_CERT_FILE=/path/to/ca-bundle.pem` |
| `missing mount: kv-x/ (N secrets; create it, or add --create-mounts)` | нет mount'а | создайте (3.1) или добавьте `--create-mounts` |
| `missing mount: kv-x/ (N secrets; create mount kv-x: HTTP 403)` | нет `create`/`update` на `sys/mounts/kv-*` | расширьте политику или создайте mount'ы заранее |
| `write kv-x/...: HTTP 403` | нет `create` на `kv-x/data/*` | добавьте mount команды в политику |
| `value written as version N, but metadata kv-x/...: HTTP 403` | нет `update` на `kv-x/metadata/*` | значение **уже** в Vault. Исправьте политику и снова запустите `apply --execute`: он покажет `metadata_repaired=N` |
| `conflict: <id>` | по пути уже другое значение | см. раздел 4, *Конфликт* |
| `mismatch: <id>` в verify | значение в Vault изменили после миграции | выясните, кто изменил; `apply --execute --overwrite` вернёт значение из выгрузки |
