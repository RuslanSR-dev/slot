# Slot

A small appointment-booking product built as a training ground for test automation architecture: test levels, contracts, ephemeral environments, quality gates, flaky-test management, evals for an LLM feature.

Сервис записи на услуги. Продукт намеренно тонкий: он нужен как объект тестирования, а основная работа идёт в платформе качества вокруг него.

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
make down      # остановить и удалить стек
```

CI запускает те же команды: [.github/workflows/ci.yml](.github/workflows/ci.yml).

## Состояние

Готовы итерации 0–2: сервисы `booking` и `payments`, провайдер PayStub в виде заглушки, сквозной сценарий «бронь → оплата → подтверждение», контракты между сервисами. Статус — в [плане](docs/roadmap.md).
