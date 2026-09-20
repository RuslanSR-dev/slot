"""What the pages need to decide before anyone is called: no HTTP, no templates.

Two things live here that are easy to get wrong and cheap to test:

1. The session token. The web service is the only place that signs one;
   booking only verifies. The format is written down in
   contracts/auth/token.v1.json, which the contract test regenerates from
   this module - if the bytes change, the change is visible in review.
2. The local time of the browser. Everything inside Slot is UTC, a person
   types the time they see on their own wall. A test that runs in UTC
   cannot tell a correct conversion from a missing one, so the e2e runs in
   another timezone on purpose.

Error messages are marked `pragma: no mutate`: they are read by people, and
the tests pin the behaviour, not the wording.
"""

import base64
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum

# The token format. The same three constants exist in booking's verifier; they
# are not shared, because services share no code (ADR-0002) - they are pinned
# by the recorded vectors instead.
VERSION_PREFIX = "v1"
SEPARATOR = "."
DIGEST = hashlib.sha256
SUBJECT_CLAIM = "sub"
ROLE_CLAIM = "role"
EXPIRES_CLAIM = "exp"

# How long a session lasts. Short enough that a stolen cookie goes stale on
# its own, long enough not to throw a person out in the middle of booking.
SESSION_LIFETIME = timedelta(hours=12)
# The alphabet of every identifier in Slot. A name outside it is refused here,
# with a readable message, instead of coming back as a 401 from booking.
NAME = re.compile(r"^[A-Za-z0-9._:@-]+$")
MAX_NAME_LENGTH = 64
# No place on Earth is further than 14 hours from UTC.
MAX_OFFSET_MINUTES = 14 * 60
# Money is kept in minor units everywhere; people type and read roubles.
MINOR_UNITS = 100


class Role(StrEnum):
    CLIENT = "client"
    MASTER = "master"


class InvalidInputError(Exception):
    """Something a person typed. The page says so instead of failing."""


def clean_name(name: str) -> str:
    """The name from the login form, or a refusal a person can act on."""
    name = name.strip()
    if not name:
        raise InvalidInputError("Введите имя")  # pragma: no mutate
    if len(name) > MAX_NAME_LENGTH:
        raise InvalidInputError(f"Имя длиннее {MAX_NAME_LENGTH} символов")  # pragma: no mutate
    if not NAME.fullmatch(name):
        raise InvalidInputError("Имя может содержать буквы, цифры и . _ - : @")  # pragma: no mutate
    return name


def parse_role(value: str) -> Role:
    try:
        return Role(value)
    except ValueError as error:
        raise InvalidInputError("Неизвестная роль") from error  # pragma: no mutate


def _b64(raw: bytes) -> str:
    """base64url without padding.

    An "=" inside a cookie value makes the server wrap the whole value in
    quotes, and every client that is not a browser then sends the quotes
    back as part of the token. Found by the smoke test against the stack.
    """
    return base64.urlsafe_b64encode(raw).replace(b"=", b"").decode()


def issue_token(subject: str, role: Role, secret: str, now: datetime) -> str:
    """Sign who this is and until when. The secret never leaves the server."""
    # Written in the order a person would, sorted by json.dumps: the bytes
    # must not depend on how this dictionary happens to be spelled.
    claims = {
        SUBJECT_CLAIM: subject,
        ROLE_CLAIM: str(role),
        EXPIRES_CLAIM: int((now + SESSION_LIFETIME).timestamp()),
    }
    payload = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    message = f"{VERSION_PREFIX}{SEPARATOR}{payload}"
    signature = _b64(hmac.new(secret.encode(), message.encode(), DIGEST).digest())
    return f"{message}{SEPARATOR}{signature}"


def to_utc(local: datetime, offset_minutes: int) -> datetime:
    """The moment a person meant, from the time on their own clock.

    `offset_minutes` is what the browser reports: minutes to add to local
    time to get UTC (Date.getTimezoneOffset), negative east of Greenwich.
    """
    if abs(offset_minutes) > MAX_OFFSET_MINUTES:
        raise InvalidInputError("Часовой пояс браузера неправдоподобен")  # pragma: no mutate
    if local.tzinfo is not None:
        raise InvalidInputError("Время из формы приходит без часового пояса")  # pragma: no mutate
    return (local + timedelta(minutes=offset_minutes)).replace(tzinfo=UTC)


def price_to_minor(roubles: str) -> int:
    """Roubles as typed into the form, kopecks as the API wants them."""
    try:
        amount = round(float(roubles.replace(",", ".")) * MINOR_UNITS)
    except ValueError as error:
        raise InvalidInputError("Цена должна быть числом") from error  # pragma: no mutate
    if amount <= 0:
        raise InvalidInputError("Цена должна быть больше нуля")  # pragma: no mutate
    return amount


def price_to_roubles(minor: int) -> str:
    """What the page shows: 150000 kopecks is 1500 roubles."""
    return f"{minor / MINOR_UNITS:.2f}"


# What a refusal from booking looks like to a person. The API answers with a
# code, the page has to answer with a sentence: a white screen or a raw
# `slot_already_booked` is a defect, and there is an e2e test for exactly that.
ERROR_MESSAGES: dict[str, str] = {
    "slot_already_booked": "Этот слот только что заняли. Выберите другое время.",
    "slot_in_past": "Это время уже прошло.",
    "slot_not_found": "Такого слота больше нет.",
    "booking_not_found": "Такой брони нет.",
    "booking_not_payable": "Эту бронь уже нельзя оплатить.",
    "invalid_transition": "Бронь уже в другом состоянии. Обновите страницу.",
    "invalid_slot": "Слот должен начинаться в будущем и заканчиваться после начала.",
    "payments_unavailable": "Оплата сейчас недоступна. Попробуйте через минуту.",
    "booking_unavailable": "Сервис записи сейчас недоступен. Попробуйте через минуту.",
}
UNEXPECTED = "Что-то пошло не так. Попробуйте ещё раз."


def explain(code: str) -> str:
    """A sentence for a person. An unknown code must not leave the page empty."""
    return ERROR_MESSAGES.get(code, UNEXPECTED)
