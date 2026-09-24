import asyncio
from datetime import datetime, timedelta, timezone

import aiohttp
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated

from app.db import engine
from app.models import Booking, BookingStatus, Event, EventSeat, Seat, SeatStatus
from app.schemas import (
    BookingCreate,
    CheckoutBooking,
    CheckoutResponse,
    EventCreate,
    EventDashboard,
    EventRead,
    EventSeatRead,
    LocationDetail,
    LocationRead,
    OccupancyDashboard,
    PaymentCompleted,
    PaymentCreate,
    PaymentQuote,
    ProtectionQuote,
    SalesDashboard,
    SeatRead,
)

router = APIRouter()


def get_current_user_id(x_user_id: Annotated[int, Header()]) -> int:
    return x_user_id


CurrentUserId = Annotated[int, Depends(get_current_user_id)]


@router.get("/locations")
async def list_locations() -> list[LocationRead]:
    """Возвращает список площадок."""
    ...


@router.get("/locations/{location_id}")
async def get_location(location_id: int) -> LocationDetail:
    """Возвращает площадку со схемой мест."""
    ...


@router.get("/locations/{location_id}/seats")
async def list_location_seats(location_id: int) -> list[SeatRead]:
    """Возвращает все места площадки."""
    ...


@router.get("/events")
async def list_events() -> list[EventRead]:
    """Возвращает список мероприятий для клиента."""
    ...


@router.get("/events/{event_id}")
async def get_event(event_id: int) -> EventRead:
    """Возвращает описание мероприятия."""
    ...


@router.get("/events/{event_id}/seats")
async def list_event_seats(event_id: int) -> list[EventSeatRead]:
    """Возвращает места на мероприятии с ценами и статусами."""
    ...


@router.get("/organizer/events")
async def list_organizer_events(organizer_id: CurrentUserId) -> list[EventRead]:
    """Возвращает список созданных событий текущего организатора."""
    ...


@router.post("/organizer/events")
async def create_event(payload: EventCreate, organizer_id: CurrentUserId) -> EventRead:
    """Создает мероприятие от лица текущего организатора."""
    ...


@router.get("/organizer/events/{event_id}/dashboard")
async def get_event_dashboard(event_id: int, organizer_id: CurrentUserId) -> EventDashboard:
    """Возвращает аналитические данные для дашборда по мероприятию."""
    # TODO: проверить, что мероприятие принадлежит organizer_id.
    # TODO: конкурентно загрузить аналитику продаж и занятость мест отдельными запросами к БД.
    async with AsyncSession(engine) as db:
        # Проверяем, что мероприятие принадлежит организатору
        event_query = select(Event).where(Event.id == event_id, Event.organizer_id == organizer_id)
        event_result = await db.execute(event_query)
        event = event_result.scalar_one_or_none()

        if not event:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Event not found or access denied",
            )

        # Конкурентно загружаем аналитику продаж и заполняемости
        async with asyncio.TaskGroup() as tg:
            sales_task = tg.create_task(_get_sales_analytics(db, event_id))
            occupancy_task = tg.create_task(_get_occupancy_analytics(db, event_id))

        sales = sales_task.result()
        occupancy = occupancy_task.result()

        return EventDashboard(
            event_title=event.title,
            starts_at=event.starts_at,
            sales=sales,
            occupancy=occupancy,
        )


async def _get_sales_analytics(db: AsyncSession, event_id: int) -> SalesDashboard:
    """Получает данные по продажам: кол-во продаж, билетов, выручка, средний чек."""
    # Считаем только оплаченные брони (paid)
    query = select(
        func.count(func.distinct(Booking.id)).label("paid_orders"),
        func.count(EventSeat.id).label("sold_tickets"),
        func.sum(Booking.amount).label("revenue"),
    ).join(
        EventSeat, Booking.id == EventSeat.booking_id
    ).where(
        Booking.event_id == event_id,
        Booking.status == BookingStatus.paid,
    )

    result = await db.execute(query)
    row = result.one()

    paid_orders = row.paid_orders or 0
    sold_tickets = row.sold_tickets or 0
    revenue = row.revenue or 0
    average_order = revenue // paid_orders if paid_orders > 0 else 0

    return SalesDashboard(
        paid_orders=paid_orders,
        sold_tickets=sold_tickets,
        revenue=revenue,
        average_order=average_order,
    )


async def _get_occupancy_analytics(db: AsyncSession, event_id: int) -> OccupancyDashboard:
    """Получает данные по заполняемости: всего мест, доступных, забронированных, проданных, процент занятости."""
    from sqlalchemy import case

    query = select(
        func.count(EventSeat.id).label("total"),
        func.sum(case((EventSeat.status == SeatStatus.available, 1), else_=0)).label("available"),
        func.sum(case((EventSeat.status == SeatStatus.reserved, 1), else_=0)).label("reserved"),
        func.sum(case((EventSeat.status == SeatStatus.sold, 1), else_=0)).label("sold"),
    ).where(EventSeat.event_id == event_id)

    result = await db.execute(query)
    row = result.one()

    total = row.total or 0
    available = row.available or 0
    reserved = row.reserved or 0
    sold = row.sold or 0

    # Процент занятости = (проданные + забронированные) / всего * 100
    occupied = sold + reserved
    occupancy_percent = (occupied / total * 100) if total > 0 else 0.0

    return OccupancyDashboard(
        total=total,
        available=available,
        reserved=reserved,
        sold=sold,
        occupancy_percent=occupancy_percent,
    )


# Конфигурация для внешних API
PAYMENT_API_URL = "http://localhost:9001"
PROTECTION_API_URL = "http://localhost:9002"
BOOKING_TIMEOUT_MINUTES = 15
PROTECTION_TIMEOUT_SECONDS = 3.0
PAYMENT_MAX_RETRIES = 7


async def _reserve_seats(
    db: AsyncSession,
    event_id: int,
    seat_ids: list[int],
    user_id: int,
) -> tuple[Event, list[tuple[EventSeat, Seat]], Booking]:
    """Бронирует места с блокировкой строк. Возвращает 409 при конфликте."""
    # Проверяем существование мероприятия
    event_query = select(Event).where(Event.id == event_id)
    event_result = await db.execute(event_query)
    event = event_result.scalar_one_or_none()

    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event not found",
        )

    # Получаем и блокируем места с помощью FOR UPDATE NOWAIT
    seats_query = (
        select(EventSeat, Seat)
        .join(Seat, EventSeat.seat_id == Seat.id)
        .where(
            EventSeat.event_id == event_id,
            EventSeat.seat_id.in_(seat_ids),
        )
        .with_for_update(nowait=True)
    )
    seats_result = await db.execute(seats_query)
    seats_data = seats_result.all()

    # Проверяем, что все места найдены
    if len(seats_data) != len(seat_ids):
        found_ids = {event_seat.seat_id for event_seat, _ in seats_data}
        missing_ids = set(seat_ids) - found_ids
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Seats not found: {missing_ids}",
        )

    # Проверяем доступность мест
    unavailable_seats = []
    base_amount = 0
    for event_seat, seat in seats_data:
        if event_seat.status != SeatStatus.available:
            unavailable_seats.append(f"Seat {seat.number} in row {seat.row}")
        base_amount += event_seat.price

    if unavailable_seats:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Seats already booked: {', '.join(unavailable_seats)}",
        )

    # Создаем бронь
    reserved_until = datetime.now(timezone.utc) + timedelta(
        minutes=BOOKING_TIMEOUT_MINUTES
    )
    booking = Booking(
        event_id=event_id,
        user_id=user_id,
        amount=base_amount,
        payment_commission=0,
        protection_price=None,
        with_protection=False,
        status=BookingStatus.pending_payment,
        reserved_until=reserved_until,
    )
    db.add(booking)
    await db.flush()

    # Обновляем статус мест на reserved
    update_seats = (
        update(EventSeat)
        .where(
            EventSeat.event_id == event_id,
            EventSeat.seat_id.in_(seat_ids),
        )
        .values(
            status=SeatStatus.reserved,
            reserved_until=reserved_until,
            booking_id=booking.id,
        )
    )
    await db.execute(update_seats)

    return event, seats_data, booking


async def _fetch_payment_quote(
    session: aiohttp.ClientSession,
    booking_id: int,
    amount: int,
) -> PaymentQuote:
    """Запрос к Payment API с retry логикой для 429 ошибок."""
    url = f"{PAYMENT_API_URL}/payment/calculate"
    payload = {
        "booking_id": booking_id,
        "amount": amount,
        "currency": "RUB",
    }

    for attempt in range(PAYMENT_MAX_RETRIES):
        try:
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    expires_at = None
                    if data.get("expires_at"):
                        expires_at = datetime.fromisoformat(data["expires_at"])
                    return PaymentQuote(
                        commission=data["commission"],
                        total=data["total"],
                        payment_methods=data["payment_methods"],
                        expires_at=expires_at,
                    )
                elif response.status == 429:
                    # Экспоненциальный backoff с jitter для retry
                    if attempt < PAYMENT_MAX_RETRIES - 1:
                        wait_time = (2 ** attempt) * 0.2 + (0.1 * (attempt % 3))
                        await asyncio.sleep(wait_time)
                        continue
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail="Payment service rate limit exceeded",
                    )
                else:
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=f"Payment service error: {response.status}",
                    )
        except asyncio.TimeoutError:
            if attempt < PAYMENT_MAX_RETRIES - 1:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Payment service timeout",
            )

    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Payment service unavailable after retries",
    )


async def _fetch_protection_quote(
    session: aiohttp.ClientSession,
    booking_id: int,
    ticket_amount: int,
    event_category: str,
    event_starts_at: datetime,
) -> ProtectionQuote | None:
    """Запрос к Protection API с жестким таймаутом 3 секунды."""
    url = f"{PROTECTION_API_URL}/protection/calculate"
    payload = {
        "booking_id": booking_id,
        "ticket_amount": ticket_amount,
        "event_category": event_category,
        "event_starts_at": event_starts_at.isoformat(),
    }

    try:
        async with session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=PROTECTION_TIMEOUT_SECONDS),
        ) as response:
            if response.status == 200:
                data = await response.json()
                return ProtectionQuote(
                    available=data["available"],
                    price=data["price"],
                    covered_amount=data["covered_amount"],
                    description=data.get("description"),
                )
            # Если сервис ответил с ошибкой - возвращаем None
            return None
    except (asyncio.TimeoutError, aiohttp.ClientError, Exception):
        # Любые ошибки или таймауты - возвращаем None
        return None


async def _update_booking_with_quotes(
    db: AsyncSession,
    booking_id: int,
    payment_commission: int,
    protection_price: int | None,
    with_protection: bool,
) -> None:
    """Обновляет бронь данными из внешних API."""
    update_query = (
        update(Booking)
        .where(Booking.id == booking_id)
        .values(
            payment_commission=payment_commission,
            protection_price=protection_price,
            with_protection=with_protection,
        )
    )
    await db.execute(update_query)
    await db.commit()


@router.post("/events/{event_id}/checkout")
async def prepare_checkout(
    event_id: int,
    payload: BookingCreate,
    user_id: CurrentUserId,
) -> CheckoutResponse:
    """Временно бронирует места за клиентом, возвращает итоговую стоимость
    и возможность страховки."""
    async with AsyncSession(engine) as db:
        async with db.begin():
            # 1. Бронируем места с блокировкой строк
            event, seats_data, booking = await _reserve_seats(
                db=db,
                event_id=event_id,
                seat_ids=payload.seat_ids,
                user_id=user_id,
            )

            base_amount = sum(event_seat.price for event_seat, _ in seats_data)
            # Сохраняем ID брони до закрытия сессии
            booking_id = booking.id

            # Сохраняем необходимые данные из event до закрытия сессии
            event_data = {
                "title": event.title,
                "category": event.category,
                "starts_at": event.starts_at,
            }

            # Сохраняем данные о местах до закрытия сессии
            seats_info = [
                {
                    "id": seat.id,
                    "row": seat.row,
                    "number": seat.number,
                    "sector": seat.sector,
                    "x": seat.x,
                    "y": seat.y,
                }
                for _, seat in seats_data
            ]

        # Коммитим транзакцию бронирования перед внешними вызовами
        await db.commit()

    # 2. Конкурентно запрашиваем внешние API
    async with aiohttp.ClientSession() as session:
        async with asyncio.TaskGroup() as tg:
            payment_task = tg.create_task(
                _fetch_payment_quote(
                    session=session,
                    booking_id=booking_id,
                    amount=base_amount,
                )
            )
            protection_task = tg.create_task(
                _fetch_protection_quote(
                    session=session,
                    booking_id=booking_id,
                    ticket_amount=base_amount,
                    event_category=event_data["category"],
                    event_starts_at=event_data["starts_at"],
                )
            )

    payment_quote = payment_task.result()
    protection_quote = protection_task.result()

    # 3. Обновляем бронь с полученными данными
    async with AsyncSession(engine) as db:
        async with db.begin():
            await _update_booking_with_quotes(
                db=db,
                booking_id=booking_id,
                payment_commission=payment_quote.commission,
                protection_price=protection_quote.price if protection_quote else None,
                with_protection=bool(protection_quote and protection_quote.available),
            )

        # 4. Получаем актуальные данные о брони для ответа
        booking_query = select(Booking).where(Booking.id == booking_id)
        booking_result = await db.execute(booking_query)
        booking = booking_result.scalar_one()

    # Формируем ответ
    checkout_booking = CheckoutBooking(
        id=booking.id,
        event_title=event_data["title"],
        starts_at=event_data["starts_at"],
        seats=seats_info,
        base_amount=base_amount,
        payment_commission=payment_quote.commission,
        protection_price=protection_quote.price if protection_quote else None,
        with_protection=bool(protection_quote and protection_quote.available),
        reserved_until=booking.reserved_until,
    )

    return CheckoutResponse(
        booking=checkout_booking,
        payment=payment_quote,
        protection=protection_quote,
    )


@router.post("/bookings/{booking_id}/pay")
async def pay_booking(
    booking_id: int,
    payload: PaymentCreate,
    user_id: CurrentUserId,
) -> PaymentCompleted:
    """Принимает способ оплаты и флаг with_protection."""
    ...