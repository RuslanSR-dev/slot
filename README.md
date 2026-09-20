# Slot

A small appointment-booking product built as a training ground for test automation architecture: test levels, contracts, ephemeral environments, quality gates, flaky-test management, evals for an LLM feature.

Сервис записи на услуги. Продукт намеренно тонкий: он нужен как объект тестирования, а основная работа идёт в платформе качества вокруг него.

Сейчас в системе четыре сервиса: `booking` (слоты и брони), `payments` (платежи и вебхуки внешнего провайдера), `notifier` (уведомления по событиям из очереди) и `web` (страницы, которыми пользуется человек). События ходят через Redis Streams, а наружу каждый сервис пишет через transactional outbox.

Вход — по имени, без паролей: `web` выдаёт подписанный токен, `booking` проверяет подпись и решает, что этому человеку можно. `web` — тонкий сервис на серверных шаблонах и HTMX: ни сборки фронтенда, ни Node в репозитории.

- [План проекта](docs/roadmap.md)
- [Архитектура](docs/architecture.md)
- [Стратегия тестирования](docs/test-strategy.md)
- [Архитектурные решения (ADR)](docs/adr)
- [Словарь терминов](docs/glossary.md)

## Запуск

Нужны Docker (или Colima), [uv](https://docs.astral.sh/uv/) и make.

```bash
make install   # зависимости из lock-файла
make check     # линтер, типы, unit-тесты с порогом покрытия
make up        # собрать образы и поднять стек, дождаться готовности
make smoke     # smoke-тесты против поднятого стека
make e2e       # четыре сценария в браузере (Playwright)
make down      # остановить и удалить стек
```

CI запускает те же команды: [.github/workflows/ci.yml](.github/workflows/ci.yml).

После `make up` сервисы доступны на `127.0.0.1`: `booking` — 8000, `payments` — 8001, `notifier` — 8002, сайт (`web`) — 8003.

Несколько стеков могут работать одновременно — достаточно сместить порты и назвать проект:

```bash
make up smoke down SLOT_PORT_OFFSET=20 COMPOSE_PROJECT_NAME=slot-experiment
```

## Состояние

Готовы итерации 0–3 и первая половина четвёртой: четыре сервиса, внешние провайдеры PayStub и NotifyGate в виде заглушек, сквозной сценарий «бронь → оплата → подтверждение → уведомление», контракты между сервисами и по событиям, интерфейс с авторизацией и четыре e2e на Playwright. Постоянного стенда пока нет. Статус — в [плане](docs/roadmap.md).
