# ØNE-ARB — Гайд по сборке и запуску

Проект состоит из 4 компонентов:

- **`not-mini-app/backend/`** — FastAPI (REST API + Telegram-бот)
- **`not-mini-app/frontend/`** — React + Vite (Telegram Mini App)
- **`not-dex-monitor/`** — Python-сервис мониторинга DEX
- **`not-bot/`** — Solidity-контракт FlashLoan (Hardhat)

Все компоненты управляются из корневой папки через `Makefile` и `docker-compose.yml`.

---

## 1. Требования

| Инструмент | Минимальная версия | Зачем |
|---|---|---|
| Docker + Docker Compose | 24+ | Основной способ запуска |
| Node.js | 20+ | Frontend, контракт (только если запускать вне Docker) |
| Python | 3.14 | DEX-монитор (вне Docker) |
| ngrok | любая | Прокинуть локальный фронт в Telegram WebApp |

Опционально:
- **Foundry / anvil** — для fork-тестов монитора (`make test-monitor-integration`).
- **MongoDB** — если запускаешь без Docker (иначе Mongo поднимается в контейнере).

---

## 2. Первичная настройка

### 2.1 Клонирование репозиториев

Проект состоит из трёх Git-репозиториев, лежащих рядом в одной корневой папке:

```bash
mkdir DIPLOMA && cd DIPLOMA
git clone git@github.com:polblack7/not-mini-app.git
git clone git@github.com:polblack7/not-dex-monitor.git
git clone git@github.com:polblack7/not-bot.git
```

Дополнительно в корень кладутся файлы:
`Makefile`, `docker-compose.yml`, `docker-compose.override.yml`, `Caddyfile.dev`,
`seed.js`, `.env.example`.
Вот ссылка на них: https://disk.360.yandex.ru/d/Q6rYP6vfkpakaw

### 2.2 Создание `.env`

```bash
cp .env.example .env
```

Затем откройте `.env` и заполните значения:

| Переменная | Где взять |
|---|---|
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → создать бота |
| `MINIAPP_URL` | URL ngrok-туннеля (см. шаг 4) |
| `JWT_SECRET` | `openssl rand -hex 32` |
| `INTERNAL_API_KEY` | любая длинная строка |
| `ACCESS_TOKEN_MASTER` | любая строка (этот токен вводит пользователь на экране Connect) |
| `WALLET_ENCRYPTION_KEY` | `openssl rand -hex 16` (32 символа) |
| `MONGO_URL` | для Docker: `mongodb://mongo:27017/onearb`. Для облака — строка из MongoDB Atlas |
| `ETH_RPC_URL` | Infura / Alchemy / Tenderly Virtual TestNet |
| `CORS_ORIGINS` | тот же URL, что в `MINIAPP_URL` |
| `DEPLOY_NETWORK`, `DEPLOY_RPC_URL` | только если планируется деплой контракта |
| `TENDERLY_RPC_URL`, `TENDERLY_CHAIN_ID` | только для демо на Tenderly TestNet |
| `VITE_*` | значения по умолчанию обычно ок; `VITE_APP_URL` = `MINIAPP_URL` |

> Frontend-переменные (`VITE_*`) живут в **корневом** `.env` — Vite сконфигурирован читать их оттуда через `envDir`.

### 2.3 Создать Telegram-бота и привязать Mini App

1. У `@BotFather`: `/newbot` → получить `BOT_TOKEN`.
2. У того же бота: `/newapp` → выбрать бота → ввести URL Mini App (тот же, что `MINIAPP_URL`).
3. (опционально) `/setmenubutton` → URL Mini App — кнопка появится в чате бота.

---

## 3. Запуск через Docker (рекомендуется)

Поднимает всё разом: Mongo + API + Bot + Monitor + Frontend (за nginx) + Caddy.

```bash
docker compose up --build
```

Compose автоматически применяет `docker-compose.override.yml` — это dev-режим (plain HTTP, Mongo проброшен на хост `:27017`, Caddy в dev-режиме).

**Проверка:**
- API: `http://localhost:8000/openapi.json`
- Frontend (через Caddy): `http://localhost`
- MongoDB: `mongodb://localhost:27017`

**Остановить / посмотреть логи:**
```bash
make down                  # docker compose down
make logs                  # все логи
make logs-api              # только API
make logs-monitor          # только монитор
make rebuild               # пересобрать всё
make rebuild-web           # пересобрать только фронт
```

### 3.1 Production-режим

Для продакшна (реальный домен + TLS через Caddy):

```bash
docker compose -f docker-compose.yml up --build
```

Флаг `-f` отключает `override.yml`. В этом режиме Caddy использует `not-mini-app/infra/Caddyfile` (нужны прописанные DNS-записи и доступ к 80/443 портам).

---

## 4. Telegram WebApp через ngrok

Telegram требует HTTPS для Mini App. Локально это решается через ngrok:

```bash
ngrok http 80
```

Вы получите URL вида `https://random-name.ngrok-free.dev`. Поместите его в `.env`:

```env
MINIAPP_URL=https://random-name.ngrok-free.dev
CORS_ORIGINS=https://random-name.ngrok-free.dev
VITE_APP_URL=https://random-name.ngrok-free.dev
```

Также обновите URL в настройках Mini App через `@BotFather`.

Перезапустите стек:
```bash
make down && make up
```

---

## 5. Заполнение БД тестовыми данными

После первого запуска можно засеять Mongo тестовыми пользователями, операциями и opportunities:

```bash
make seed
# = node seed.js
```

Скрипт работает с `MONGO_URL` из `.env`.

---

## 6. Smart-контракт (опционально)

Только если нужно задеплоить `FlashLoan.sol`:

```bash
cd not-bot
npm install
cd ..

# Скомпилировать
make contract-compile

# Прогнать тесты Hardhat
make test-contract

# Деплой
make deploy NETWORK=sepolia
make deploy NETWORK=mainnet
make setup-tenderly         # деплой на Tenderly Virtual TestNet
```

`make deploy` автоматически парсит адрес из stdout `deploy.js` и пушит его в backend.

---

## 7. Запуск без Docker (локально, отдельные процессы)

Полезно для дебага одного компонента.

### 7.1 Установить зависимости

```bash
make install        # все зависимости разом
# или поштучно:
make install-backend
make install-frontend
make install-monitor
make install-bot
```

> DEX-монитор требует **Python 3.14**: `python3.14 -m pip install -r not-dex-monitor/requirements.txt`.

### 7.2 Запустить процессы

В разных терминалах:

```bash
make api          # FastAPI на :8000
make bot          # aiogram-бот
make frontend     # Vite dev-сервер на :5173 (или :80 при `npm run dev`)
make monitor      # DEX-монитор
```

Для этого режима нужна локальная MongoDB на `:27017` (или Mongo из Docker — поднимите отдельным контейнером).

---

## 8. Тесты

```bash
make test                       # весь тестовый прогон (монитор + контракт)
make test-monitor               # только pytest для not-dex-monitor
make test-monitor ARGS=tests/test_profitability.py   # конкретный файл
make test-monitor-integration   # fork-тесты (нужен anvil + ETH_RPC_URL)
make test-contract              # Hardhat-тесты FlashLoan
```

Для fork-тестов:
```bash
# В отдельном терминале запустить anvil с мейннет-форком
anvil --fork-url $ETH_RPC_URL
# Затем
make test-monitor-integration
```

---

## 9. Типичные проблемы

| Симптом | Причина / решение |
|---|---|
| Frontend не видит `VITE_*` | Проверьте, что переменные в **корневом** `.env`, не во `frontend/.env`. Перезапустите `make rebuild-web`. |
| `make deploy` падает на «push address» | `scripts/deploy.js` не вывел строку `DEPLOYED_ADDRESS=0x...`. Поправьте скрипт. |
| Мониторинг не видит пользователей | Проверьте `INTERNAL_API_KEY` — он должен совпадать в `.env` для api и monitor. |
| `502` через Caddy в проде | Проверьте, что `web` и `api` healthy: `docker compose ps`. |
| Mongo connection refused | В Docker используйте хост `mongo:27017` (имя сервиса), а не `localhost`. |
| Telegram «WebApp is unavailable» | URL Mini App в BotFather не совпадает с `MINIAPP_URL`/ngrok. |

---

## 10. Что лежит в корне

| Файл / папка | Назначение |
|---|---|
| `not-mini-app/`, `not-dex-monitor/`, `not-bot/` | Исходники компонентов проекта |
| `.env` | Конфиг и секреты |
| `.env.example` | Шаблон для нового окружения |
| `docker-compose.yml` | Главный compose-файл |
| `docker-compose.override.yml` | Dev-патч (auto-pickup для локалки) |
| `Caddyfile.dev` | Dev-конфиг reverse-proxy Caddy |
| `Makefile` | Команды сборки и запуска |
| `seed.js` | Скрипт сидинга MongoDB (`make seed`) |
