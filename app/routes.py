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
    ...


@router.post("/events/{event_id}/checkout")
async def prepare_checkout(
    event_id: int,
    payload: BookingCreate,
    user_id: CurrentUserId,
) -> CheckoutResponse:
    """Временно бронирует места за клиентом, возвращает итоговую стоимость
    и возможность страховки."""
    ...


@router.post("/bookings/{booking_id}/pay")
async def pay_booking(
    booking_id: int,
    payload: PaymentCreate,
    user_id: CurrentUserId,
) -> PaymentCompleted:
    """Принимает способ оплаты и флаг with_protection."""
    ...
