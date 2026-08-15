"""
Buyuk Premium — Cashback Platform
1C OptimalSavdo (ОбщийМодуль.CashbackPlatfrom) uchun server.

Kontrakt 1C moduldan olingan:
  POST /{API_VERSION}/card_scan        {"scan_data": "..."}
  POST /{API_VERSION}/update_client    {"card_number","client_name","phone_number","generate_card"}
  POST /{API_VERSION}/add_transaction  [ {...}, {...} ]

Javob: {"success": bool, "message": str, "data": {...}}
Xato:  HTTP != 200 + {"error": "..."}
Auth:  HTTP Basic (1C HTTPСоединече user/password)
"""

import os
import re
import secrets
import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Numeric, String, func, select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ─────────────────────────── Sozlamalar ───────────────────────────

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cashback")


def _env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"{name} muhit o'zgaruvchisi sozlanmagan")
    return value


def _async_dsn(raw: str) -> str:
    """Railway `postgresql://` yoki `postgres://` beradi — asyncpg ga o'tkazamiz."""
    if raw.startswith("postgresql+"):
        return raw
    if raw.startswith("postgresql://"):
        return raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    if raw.startswith("postgres://"):
        return raw.replace("postgres://", "postgresql+asyncpg://", 1)
    return raw


DATABASE_URL = _async_dsn(_env("DATABASE_URL"))
API_VERSION = os.getenv("CASHBACK_API_VERSION", "v1").strip("/")
API_USERNAME = _env("CASHBACK_USERNAME")
API_PASSWORD = _env("CASHBACK_PASSWORD")
SHOP_TOKEN = os.getenv("CASHBACK_SHOP_TOKEN", "")
CARD_PREFIX = os.getenv("CARD_PREFIX", "29")
DEFAULT_CASHBACK_PCT = Decimal(os.getenv("DEFAULT_CASHBACK_PCT", "1"))
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "300"))

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=5)
Session = async_sessionmaker(engine, expire_on_commit=False)

# ─────────────────────────── Modellar ───────────────────────────


class Base(DeclarativeBase):
    pass


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    card_number: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    client_name: Mapped[str] = mapped_column(String(200), default="")
    phone_number: Mapped[str] = mapped_column(String(30), default="", index=True)
    telegram_chat_id: Mapped[str] = mapped_column(String(30), default="")
    balance: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal(0))
    cashback_percentage: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=DEFAULT_CASHBACK_PCT
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def as_payload(self) -> dict[str, Any]:
        """1C aynan shu kalitlarni o'qiydi — nomlarini o'zgartirmang."""
        return {
            "card_number": self.card_number,
            "client_name": self.client_name or "",
            "phone_number": self.phone_number or "",
            "telegram_chat_id": self.telegram_chat_id or "",
            "balance": float(self.balance),
            "cashback_percentage": float(self.cashback_percentage),
        }


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True)
    card_number: Mapped[str] = mapped_column(String(50), default="", index=True)
    purchase_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal(0))
    cashback_earned: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal(0))
    cashback_spent: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal(0))
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ─────────────────────────── Xavfsizlik ───────────────────────────

basic = HTTPBasic(auto_error=True)
_hits: dict[str, list[datetime]] = defaultdict(list)


def check_auth(credentials: HTTPBasicCredentials = Depends(basic)) -> str:
    user_ok = secrets.compare_digest(credentials.username, API_USERNAME)
    pass_ok = secrets.compare_digest(credentials.password, API_PASSWORD)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Login yoki parol noto'g'ri",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def check_rate_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = datetime.now(timezone.utc)
    window = now - timedelta(minutes=1)
    _hits[ip] = [t for t in _hits[ip] if t > window]
    if len(_hits[ip]) >= RATE_LIMIT_PER_MIN:
        raise HTTPException(status_code=429, detail="So'rovlar juda ko'p")
    _hits[ip].append(now)


async def get_session() -> AsyncSession:
    async with Session() as session:
        yield session


guard = [Depends(check_auth), Depends(check_rate_limit)]

# ─────────────────────────── Sxemalar ───────────────────────────

PHONE_RE = re.compile(r"[^\d]")


def normalize_phone(raw: str) -> str:
    digits = PHONE_RE.sub("", raw or "")
    if digits.startswith("998"):
        digits = digits[3:]
    return digits[-9:] if len(digits) >= 9 else digits


class CardScanIn(BaseModel):
    scan_data: str = Field(min_length=1, max_length=100)

    @field_validator("scan_data")
    @classmethod
    def clean(cls, v: str) -> str:
        return v.strip().replace(" ", "").replace("+", "")


class UpdateClientIn(BaseModel):
    card_number: str = Field(default="", max_length=30)
    client_name: str = Field(default="", max_length=200)
    phone_number: str = Field(default="", max_length=30)
    generate_card: bool = False


# ─────────────────────────── Yordamchilar ───────────────────────────


def ok(data: dict | list | None = None, message: str = "") -> dict:
    return {"success": True, "message": message, "data": data if data is not None else {}}


def fail(message: str) -> dict:
    """HTTP 200 + success=false — 1C buni `Контекст.message` orqali ko'rsatadi."""
    return {"success": False, "message": message, "data": {}}


def ean13_check_digit(body12: str) -> str:
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(body12))
    return str((10 - total % 10) % 10)


async def generate_card_number(session: AsyncSession) -> str:
    """Skanerlanadigan EAN-13. Tovar prefikslari bilan to'qnashmasligi shart."""
    for _ in range(20):
        body = CARD_PREFIX + "".join(secrets.choice("0123456789") for _ in range(12 - len(CARD_PREFIX)))
        candidate = body + ean13_check_digit(body)
        exists = await session.scalar(select(Client.id).where(Client.card_number == candidate))
        if not exists:
            return candidate
    raise HTTPException(status_code=500, detail="Karta raqami generatsiya qilinmadi")


async def find_client(session: AsyncSession, needle: str) -> Client | None:
    """Karta raqami, telefon yoki telegram_chat_id bo'yicha qidirish."""
    client = await session.scalar(select(Client).where(Client.card_number == needle))
    if client:
        return client
    phone = normalize_phone(needle)
    if len(phone) == 9:
        client = await session.scalar(select(Client).where(Client.phone_number == phone))
        if client:
            return client
    if needle.isdigit():
        client = await session.scalar(select(Client).where(Client.telegram_chat_id == needle))
    return client


# ─────────────────────────── Ilova ───────────────────────────

app = FastAPI(title="Buyuk Premium Cashback", docs_url=None, redoc_url=None)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    """1C xatoni `ЖавобСтркт.error` dan o'qiydi."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
        headers=exc.headers or {},
    )


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    log.exception("Kutilmagan xato: %s", request.url.path)
    return JSONResponse(status_code=500, content={"error": "Ichki xatolik"})


@app.on_event("startup")
async def startup() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("Cashback server tayyor, API versiya: /%s", API_VERSION)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": API_VERSION}


@app.post(f"/{API_VERSION}/card_scan", dependencies=guard)
async def card_scan(payload: CardScanIn, session: AsyncSession = Depends(get_session)) -> dict:
    """Kassa kartani skanerlaganda — mijoz va ball qoldig'i."""
    client = await find_client(session, payload.scan_data)
    if client is None:
        return fail("Бундай карта топилмади")
    if not client.is_active:
        return fail("Карта блокланган")
    return ok(client.as_payload())


@app.post(f"/{API_VERSION}/update_client", dependencies=guard)
async def update_client(
    payload: UpdateClientIn, session: AsyncSession = Depends(get_session)
) -> dict:
    """Kassadan yangi mijoz qo'shish yoki mavjudini yangilash."""
    card = payload.card_number.strip()
    phone = normalize_phone(payload.phone_number)
    name = payload.client_name.strip()

    if not payload.generate_card and not card:
        return fail("Карта рақами киритилмаган")
    if phone and len(phone) != 9:
        return fail("Телефон рақами нотўғри (9 та рақам бўлиши керак)")

    client: Client | None = None
    if card:
        client = await session.scalar(select(Client).where(Client.card_number == card))
    if client is None and phone:
        client = await session.scalar(select(Client).where(Client.phone_number == phone))

    if client is None:
        if not payload.generate_card and not card:
            return fail("Карта рақами киритилмаган")
        client = Client(
            card_number=card or await generate_card_number(session),
            client_name=name,
            phone_number=phone,
        )
        session.add(client)
    else:
        if name:
            client.client_name = name
        if phone:
            client.phone_number = phone
        if payload.generate_card and not client.card_number:
            client.card_number = await generate_card_number(session)

    if not client.client_name:
        client.client_name = client.card_number

    await session.commit()
    await session.refresh(client)
    log.info("Mijoz saqlandi: karta=%s", client.card_number)
    return ok(client.as_payload())


@app.post(f"/{API_VERSION}/add_transaction", dependencies=guard)
async def add_transaction(
    payload: list[dict[str, Any]], session: AsyncSession = Depends(get_session)
) -> dict:
    """
    1C РегистрСведений.CashbackTransactions.ПолучитьНеОтправленныеЗаписи() massivi.

    Har bir element:
      created_at          "yyyyMMddHHmmss"
      card_number         karta raqami
      purshace_amount     savdo summasi
      cashback_spent      ball bilan to'langan qism
      amount              haqiqatda to'langan pul (purshace - spent)
      cashback_percentage keshbek foizi
      casher, store_token, id_transaction

    Javob AYNAN shu shaklda bo'lishi shart —
    ОтметитьОтправленныхЗаписей() to'liq tanani o'qiydi, .data ni emas:
      {"validTransactions": [...], "failedTransactions": [{"id_transaction","reason"}]}
    """
    valid: list[str] = []
    failed: list[dict[str, str]] = []

    def to_dec(value: Any) -> Decimal:
        try:
            return Decimal(str(value or 0))
        except Exception:
            return Decimal(0)

    for item in payload:
        ext_id = str(item.get("id_transaction") or "").strip()
        if not ext_id:
            failed.append({"id_transaction": "", "reason": "id_transaction bo'sh"})
            continue

        exists = await session.scalar(
            select(Transaction.id).where(Transaction.external_id == ext_id)
        )
        if exists:
            valid.append(ext_id)  # idempotent — qayta yuborilsa ham OK
            continue

        card = str(item.get("card_number") or "").strip()
        client = await session.scalar(select(Client).where(Client.card_number == card))
        if client is None:
            failed.append({"id_transaction": ext_id, "reason": f"Карта топилмади: {card}"})
            continue

        paid = to_dec(item.get("amount"))
        purchase = to_dec(item.get("purshace_amount"))
        spent = to_dec(item.get("cashback_spent"))
        pct = to_dec(item.get("cashback_percentage")) or client.cashback_percentage

        if spent > client.balance:
            failed.append(
                {"id_transaction": ext_id, "reason": "Балл қолдиғи етарли эмас"}
            )
            continue

        # Keshbek faqat haqiqatda to'langan pulga beriladi
        earned = (paid * pct / 100).quantize(Decimal("0.01"))
        client.balance = client.balance + earned - spent
        if client.balance < 0:
            client.balance = Decimal(0)

        session.add(
            Transaction(
                external_id=ext_id,
                client_id=client.id,
                card_number=card,
                purchase_amount=purchase,
                cashback_earned=earned,
                cashback_spent=spent,
                raw=item,
            )
        )
        valid.append(ext_id)

    await session.commit()
    log.info("Tranzaksiya: %s qabul, %s rad", len(valid), len(failed))
    return {"validTransactions": valid, "failedTransactions": failed}
