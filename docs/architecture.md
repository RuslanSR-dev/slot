# Архитектура Slot

Slot — сервис записи на услуги: клиент выбирает свободное время у мастера, оплачивает и получает уведомление. Продукт намеренно тонкий. Он существует как объект тестирования для платформы качества.

## Целевая схема

Сплошная рамка — уже есть. Пунктир — запланировано, в скобках номер итерации.

```mermaid
flowchart LR
    client([Клиент])

    subgraph product[Продукт Slot]
        web["web (4)<br/>интерфейс"]:::planned
        mobile["mobile (8)<br/>клиент"]:::planned
        booking["booking<br/>слоты и брони"]
        payments["payments<br/>платежи, вебхуки"]
        pdb[("Postgres payments")]
        notifier["notifier<br/>уведомления"]
        assistant["assistant (7)<br/>запись текстом, LLM"]:::planned
        migrate["booking-migrate<br/>миграции, однократно"]
        db[("Postgres booking")]
        ndb[("Postgres notifier")]
        brelay["booking-relay<br/>outbox → очередь"]
        prelay["payments-relay<br/>outbox → HTTP"]
        queue[["Redis Streams<br/>события"]]
    end

    provider["PayStub<br/>внешний провайдер, в стеке — WireMock"]:::external
    llm["API модели<br/>внешний"]:::external
    gateway["NotifyGate<br/>внешний шлюз, в стеке — WireMock"]:::external

    subgraph platform[Платформа качества]
        ci["CI<br/>gates + эфемерный стек"]
        hub["Quality Hub (5)<br/>flaky, время обратной связи"]:::planned
    end

    client --> web & mobile
    web & mobile --> booking
    web --> assistant
    assistant --> booking
    assistant --> llm
    booking --> db
    migrate --> db
    booking -- создать платёж --> payments
    payments --> pdb
    payments --> provider
    provider -. вебхук .-> payments
    payments --> prelay
    prelay -- подтвердить бронь, возврат --> booking & provider
    db --> brelay --> queue --> notifier
    notifier --> ndb
    notifier --> gateway
    ci -- результаты прогонов --> hub

    classDef planned stroke-dasharray: 5 5
    classDef external fill:#eee,stroke:#999,color:#333
```

## Где какая архитектурная проблема

| Часть | Проблема | Чем проверяем |
|---|---|---|
| booking + Postgres | Двое бронируют один слот одновременно | Тесты на гонки, ограничения на уровне базы |
| payments | Чужой API, повторные вебхуки | Контрактные тесты, заглушка провайдера, идемпотентность |
| outbox + очередь + notifier | Потерянные и повторные сообщения, зависшие и «ядовитые» | Тесты событий: недоступный брокер, дубликат, обратный порядок, падение до и после отправки, очередь ошибок |
| assistant | Недетерминизм, prompt-инъекции | Evals: проверки кодом по структурированному ответу |
| web, mobile | Хрупкие UI-тесты | Минимум e2e, доступность, визуальная регрессия |

## Правила

- Сервисы общаются только по сети, общего кода нет (ADR-0002).
- Каждый сервис отдаёт `/health` с версией и коммитом сборки (ADR-0003).
- У каждого сервиса своя база; чужие таблицы не читаются.
- Повторы безопасны: дубликаты разводят естественные ключи в базе (ADR-0008).
- Наружу сервис сообщает через outbox: сообщение пишется той же транзакцией, что и изменение, и отправляется отдельным процессом (ADR-0010). Доменный код не знает про брокер.
- Доставка — «хотя бы один раз». Получатель идемпотентен, значит, повтор безопасен; «ровно один раз» не обещается никому.
- Правила, которые нельзя нарушать, гарантирует база, а не код (ADR-0005). Схема меняется только миграциями, их применяет отдельный одноразовый контейнер (ADR-0006).
- Решения, которые дорого отменить, записываются в `docs/adr`.
