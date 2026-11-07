# Proxy Manager API

Микросервис на FastAPI для выдачи списка прокси, фильтрации и временного закрепления прокси за пользователями. Список загружается из [monosans/proxy-list](https://github.com/monosans/proxy-list.git), кэшируется в памяти API, а аренды хранятся в Redis.

## Возможности

- получение списка прокси с фильтрами и пагинацией;
- выбор случайного прокси;
- аренда прокси с ограниченным временем действия (TTL), продление и освобождение;
- статистика пула и ручное обновление кэша;
- проверка внешнего IP через выбранный прокси;
- интерактивная документация OpenAPI (Swagger и ReDoc).

## Состав проекта

- [server.py](./server.py) — FastAPI-приложение и API;
- [start.sh](./start.sh) — загрузка и обновление списка прокси, HTTP-раздача файлов на порту `8080`;
- [docker-compose.yml](./docker-compose.yml) — запуск Redis, загрузчика списка, API и отдельного контейнера админки;
- [Dockerfile.admin](./Dockerfile.admin) — образ для раздачи веб-интерфейсов админки;
- [.env.example](./.env.example) — пример переменных окружения для Docker Compose;
- [admin.html](./admin.html) и [admin2.html](./admin2.html) — веб-интерфейсы для работы с API;
- [requirements.txt](./requirements.txt) — Python-зависимости.

## Быстрый запуск через Docker Compose

Понадобятся Docker Engine и Docker Compose.

Перед запуском создайте локальный `.env` на основе примера:

```bash
cp .env.example .env
```

Замените `API_KEY` на свой длинный случайный ключ. Compose автоматически читает `.env`; сам файл с настройками не добавляйте в репозиторий.

```bash
docker compose up --build -d
```

Compose запускает четыре сервиса:

1. `proxy-source` скачивает список прокси и раздаёт `proxies.json` на внутреннем порту `8080`. Обновление выполняется каждые 10 минут.
2. `redis` хранит аренды прокси.
3. `proxy-api` загружает список в кэш и предоставляет API на порту `8000`. Кэш обновляется каждые 300 секунд.
4. `proxy-admin` раздаёт веб-интерфейсы через Nginx на порту `8081`. Админка запускается отдельно и остаётся доступной, даже если API, Redis или загрузчик прокси остановлены.

Веб-интерфейсы после запуска Compose:

- Основная админка: <http://localhost:8081/>
- Упрощённый API-тестер: <http://localhost:8081/admin2.html>

Кнопки и формы админки требуют работающий API. Если открываете админку не на том же компьютере, укажите в настройках адрес API, доступный из браузера.

Проверить состояние:

```bash
curl http://localhost:8000/health
```

При готовых сервисах ответ содержит `"status": "ok"`, `"redis": true` и количество прокси в `"proxy_count"`. При первом запуске загрузка списка может занять некоторое время.

Документация API:

- Swagger UI: <http://localhost:8000/docs>
- ReDoc: <http://localhost:8000/redoc>

Остановить сервисы:

```bash
docker compose down
```

Данные Redis и загруженные версии списка находятся в именованных Docker volumes и сохраняются после остановки контейнеров. Чтобы удалить и контейнеры, и эти данные:

```bash
docker compose down -v
```

## Локальный запуск без Compose

Нужны Python 3.12+, Redis и Git. Запустите Redis, затем в первом терминале из каталога проекта выполните:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./start.sh
```

Скрипт загрузит список из GitHub и начнёт раздавать его на `http://localhost:8080`. Во втором терминале запустите API:

```bash
source .venv/bin/activate
uvicorn server:app --host 0.0.0.0 --port 8000
```

По умолчанию API подключается к источнику `http://localhost:8080/proxies.json` и Redis `redis://localhost:6379/0`. Эти адреса можно переопределить переменными окружения, описанными ниже.

## Конфигурация

| Переменная | Значение по умолчанию | Назначение |
|---|---|---|
| `PROXY_SOURCE_URL` | `http://localhost:8080/proxies.json` | URL JSON-списка прокси |
| `REDIS_URL` | `redis://localhost:6379/0` | URL Redis |
| `API_KEY` | пусто | Если задан, запросы к `/api/v1/*` должны содержать заголовок `x-api-key` |
| `CACHE_UPDATE_INTERVAL` | `300` | Интервал обновления кэша API в секундах |
| `TZ` | `Europe/Moscow` в Compose | Часовой пояс контейнеров |

В [.env.example](./.env.example) указаны значения по умолчанию для Docker Compose. URL источника и Redis используют внутренние имена Compose-сервисов; при обычном запуске менять их не нужно. После изменения `.env` пересоздайте контейнеры, чтобы применить конфигурацию:

```bash
docker compose up -d --force-recreate
```

Проверка ключа применяется к маршрутам `/api/v1/*`; `/health`, `/docs` и `/redoc` остаются общедоступными. Введите тот же ключ в настройках админки. Не выставляйте сервис напрямую в публичную сеть без подходящей сетевой защиты и настройки доступа.

## API

Базовый префикс маршрутов API: `/api/v1`. Если `API_KEY` задан, передавайте заголовок `x-api-key`.

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/health` | Состояние API и Redis, число прокси, время последнего обновления |
| `GET` | `/api/v1/proxies` | Список с фильтрами и пагинацией |
| `GET` | `/api/v1/proxies/random` | Случайный прокси по фильтрам |
| `POST` | `/api/v1/proxies/lease` | Создать аренду или вернуть существующую для пользователя |
| `GET` | `/api/v1/proxies/lease/{user_id}` | Получить прокси пользователя |
| `GET` | `/api/v1/proxies/lease/{user_id}/status` | Проверить состояние аренды |
| `POST` | `/api/v1/proxies/lease/{user_id}/renew` | Продлить аренду |
| `DELETE` | `/api/v1/proxies/lease/{user_id}` | Освободить прокси |
| `GET` | `/api/v1/proxies/stats` | Статистика по протоколам и странам |
| `POST` | `/api/v1/proxies/refresh` | Запросить обновление кэша |
| `POST` | `/api/v1/proxies/check-ip` | Проверить IP и сведения о подключении через прокси |
| `GET` | `/api/v1/proxies/check-ip/simple` | Проверить только внешний IP через прокси |

Для списка поддерживаются параметры `country`, `protocol`, `min_timeout`, `max_timeout`, `limit` и `offset`. Для случайного выбора — `country`, `protocol` и `max_timeout`. Код страны — ISO-код, например `US` или `DE`; протоколы источника включают `http`, `socks4` и `socks5`.

Аренда принимает `user_id` (1–128 символов), необязательные `country`, `protocol` и `max_timeout`, а также `ttl` в секундах (от 60 до 3600; по умолчанию 600). Например:

```bash
curl -X POST http://localhost:8000/api/v1/proxies/lease \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"user-123","country":"US","protocol":"http","ttl":600}'
```

Проверить состояние аренды:

```bash
curl http://localhost:8000/api/v1/proxies/lease/user-123/status
```

Освободить прокси:

```bash
curl -X DELETE http://localhost:8000/api/v1/proxies/lease/user-123
```

Проверка IP обращается к внешним сервисам: `/check-ip` — к `httpbin.org` и `ip-api.com`, `/check-ip/simple` — к `api.ipify.org`. Для этих маршрутов необходим исходящий доступ из контейнера/API.

## Веб-интерфейсы

`admin.html` — расширенная консоль для списка, аренды, статистики и проверки IP; `admin2.html` — упрощённый тестер API. При запуске через Docker Compose оба файла доступны через отдельный контейнер Nginx по адресу <http://localhost:8081/> и <http://localhost:8081/admin2.html>. Контейнер админки не зависит от API, но для выполнения запросов из интерфейса API должен быть запущен и доступен браузеру. Укажите базовый URL API (`http://localhost:8000`) и, если настроен, API-ключ.

При локальном запуске без Compose откройте нужный файл в браузере или раздайте каталог проекта отдельным локальным HTTP-сервером, например:

```bash
python -m http.server 9000
```

Затем откройте `http://localhost:9000/admin.html` или `http://localhost:9000/admin2.html`.
