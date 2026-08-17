#!/usr/bin/env python3
"""Fetch MOEX stock quotes and post movers (>3% from day open) to Telegram.
Optimized version with faster data loading."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Iterable
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

MOEX_SHARES_URL = "https://iss.moex.com/iss/engines/stock/markets/shares/securities.json"
MOEX_STATUS_URL = (
    "https://iss.moex.com/iss/engines/stock/markets/shares/"
    "boards/TQBR/securities/SBER.json"
)
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
MSK = ZoneInfo("Europe/Moscow")
PAGE_SIZE = 100
MAX_MOVERS_IN_MESSAGE = 30

# Основные торговые площадки для быстрой загрузки
MAIN_BOARDS = {"TQBR", "TQTD", "SMAL"}

BOARD_PRIORITY = {
    "TQBR": 0,
    "TQTD": 1,
    "SMAL": 2,
    "TQTF": 3,
    "TQIF": 4,
    "TQTE": 5,
    "TQTY": 6,
    "TQTH": 7,
    "TQFE": 8,
    "TQFD": 9,
    "SPEQ": 10,
}


@dataclass(frozen=True)
class Quote:
    secid: str
    shortname: str
    boardid: str
    open_price: float | None
    last_price: float | None


@dataclass(frozen=True)
class StockMove:
    secid: str
    shortname: str
    boardid: str
    open_price: float
    last_price: float
    change_pct: float


def create_session() -> requests.Session:
    """Create optimized HTTP session with retries and connection pooling."""
    session = requests.Session()
    
    # Настройка retry для устойчивости к сетевым ошибкам
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    
    return session


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if not value:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return value


def parse_time(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(hour=int(hour), minute=int(minute))


def parse_holidays(raw: str | None) -> set[date]:
    if not raw:
        return set()
    holidays: set[date] = set()
    for item in raw.split(","):
        item = item.strip()
        if item:
            holidays.add(date.fromisoformat(item))
    return holidays


def is_weekday(day: date) -> bool:
    return day.weekday() < 5


def is_within_trading_hours(
    now: datetime,
    start: time,
    end: time,
) -> bool:
    current = now.time().replace(tzinfo=None)
    return start <= current <= end


def market_has_today_quotes() -> bool:
    """Use a liquid ticker to detect holidays when the calendar API is unavailable."""
    session = create_session()
    try:
        response = session.get(
            MOEX_STATUS_URL,
            params={
                "iss.meta": "off",
                "iss.only": "marketdata",
                "marketdata.columns": "SYSTIME,OPEN,LAST",
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload["marketdata"]["data"]
        if not rows:
            return False

        columns = payload["marketdata"]["columns"]
        row = dict(zip(columns, rows[0], strict=False))
        systime_raw = row.get("SYSTIME")
        if not systime_raw:
            return False

        systime = datetime.fromisoformat(str(systime_raw).replace(" ", "T")).replace(tzinfo=MSK)
        today = datetime.now(MSK).date()
        if systime.date() != today:
            return False

        return row.get("OPEN") is not None or row.get("LAST") is not None
    finally:
        session.close()


def is_moex_trading_session(now: datetime | None = None) -> tuple[bool, str]:
    now = now or datetime.now(MSK)
    today = now.date()
    start = parse_time(os.environ.get("TRADING_START_MSK", "10:00"))
    end = parse_time(os.environ.get("TRADING_END_MSK", "18:50"))
    holidays = parse_holidays(os.environ.get("MOEX_HOLIDAYS"))

    if not is_weekday(today):
        return False, "выходной день"

    if today in holidays:
        return False, "неторговый день (праздник)"

    if not is_within_trading_hours(now, start, end):
        return False, f"вне торговых часов ({start.strftime('%H:%M')}–{end.strftime('%H:%M')} MSK)"

    if os.environ.get("SKIP_MARKET_ACTIVITY_CHECK", "0") != "1":
        try:
            if not market_has_today_quotes():
                return False, "биржа сегодня не торгует"
        except requests.RequestException as exc:
            print(f"Market activity check failed: {exc}", file=sys.stderr)
            raise RuntimeError("Не удалось проверить статус рынка MOEX из-за сетевой ошибки") from exc

    return True, "торговая сессия активна"


def board_rank(boardid: str) -> int:
    return BOARD_PRIORITY.get(boardid, 100)


def quote_is_usable(quote: Quote) -> bool:
    return (
        quote.open_price is not None
        and quote.last_price is not None
        and quote.open_price > 0
        and quote.last_price > 0
    )


def should_replace(existing: Quote, candidate: Quote) -> bool:
    existing_ok = quote_is_usable(existing)
    candidate_ok = quote_is_usable(candidate)

    if candidate_ok and not existing_ok:
        return True
    if existing_ok and not candidate_ok:
        return False
    if candidate_ok and existing_ok:
        return board_rank(candidate.boardid) < board_rank(existing.boardid)
    return board_rank(candidate.boardid) < board_rank(existing.boardid)


def fetch_all_share_quotes() -> list[Quote]:
    """Return quotes for all MOEX share listings, optimized for speed."""
    by_secid: dict[str, Quote] = {}
    start = 0
    
    session = create_session()
    try:
        while True:
            response = session.get(
                MOEX_SHARES_URL,
                params={
                    "iss.meta": "off",
                    "iss.only": "securities,marketdata",
                    "securities.columns": "SECID,SHORTNAME,BOARDID",
                    "marketdata.columns": "SECID,OPEN,LAST",
                    "start": start,
                    "limit": PAGE_SIZE,
                },
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()

            securities = payload["securities"]["data"]
            if not securities:
                break

            sec_index = {name: idx for idx, name in enumerate(payload["securities"]["columns"])}

            md_dict = {}
            if "marketdata" in payload and payload["marketdata"]["data"]:
                md_columns = payload["marketdata"]["columns"]
                md_index = {name: idx for idx, name in enumerate(md_columns)}
                for md_row in payload["marketdata"]["data"]:
                    secid = md_row[md_index["SECID"]]
                    md_dict[secid] = {
                        "OPEN": md_row[md_index["OPEN"]],
                        "LAST": md_row[md_index["LAST"]]
                    }

            for sec_row in securities:
                secid = sec_row[sec_index["SECID"]]
                shortname = sec_row[sec_index["SHORTNAME"]]
                boardid = sec_row[sec_index["BOARDID"]]

                # ОПТИМИЗАЦИЯ: пропускаем неосновные площадки
                if boardid not in MAIN_BOARDS:
                    continue

                md = md_dict.get(secid, {})
                open_raw = md.get("OPEN")
                last_raw = md.get("LAST")

                open_price = float(open_raw) if open_raw is not None else None
                last_price = float(last_raw) if last_raw is not None else None

                candidate = Quote(
                    secid=secid,
                    shortname=shortname,
                    boardid=boardid,
                    open_price=open_price,
                    last_price=last_price,
                )
                existing = by_secid.get(secid)
                if existing is None or should_replace(existing, candidate):
                    by_secid[secid] = candidate

            if len(securities) < PAGE_SIZE:
                break
            start += PAGE_SIZE
            
            # Вывод прогресса
            if start % 300 == 0:
                print(f"  Loaded {len(by_secid)} securities so far...")

    finally:
        session.close()

    return list(by_secid.values())


def find_movers(quotes: Iterable[Quote], threshold_pct: float) -> list[StockMove]:
    movers: list[StockMove] = []

    for quote in quotes:
        if not quote_is_usable(quote):
            continue

        assert quote.open_price is not None
        assert quote.last_price is not None
        change_pct = (quote.last_price - quote.open_price) / quote.open_price * 100
        if abs(change_pct) > threshold_pct:
            movers.append(
                StockMove(
                    secid=quote.secid,
                    shortname=quote.shortname,
                    boardid=quote.boardid,
                    open_price=quote.open_price,
                    last_price=quote.last_price,
                    change_pct=change_pct,
                )
            )

    movers.sort(key=lambda item: abs(item.change_pct), reverse=True)
    return movers


def format_header(threshold_pct: float, securities_checked: int) -> str:
    now = datetime.now(MSK)
    return (
        f"<b>MOEX — все акции, изменение &gt; {threshold_pct:g}% с открытия</b>\n"
        f"<i>{now.strftime('%d.%m.%Y %H:%M')} MSK</i>\n"
        f"Проверено бумаг: {securities_checked}\n\n"
    )


def format_message(
    movers: list[StockMove],
    threshold_pct: float,
    securities_checked: int,
) -> str:
    header = format_header(threshold_pct, securities_checked)

    lines: list[str] = []
    for move in movers:
        direction = "🟢" if move.change_pct > 0 else "🔴"
        sign = "+" if move.change_pct > 0 else ""
        lines.append(
            f"{direction} <b>{move.secid}</b> ({move.shortname}, {move.boardid}): "
            f"{sign}{move.change_pct:.2f}% — {move.last_price:.2f} ₽ "
            f"(откр. {move.open_price:.2f})"
        )

    return header + "\n".join(lines)


def format_empty_message(threshold_pct: float, securities_checked: int) -> str:
    return (
        format_header(threshold_pct, securities_checked)
        + f"Значимых изменений (&gt; {threshold_pct:g}%) с начала дня нет."
    )


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    session = create_session()
    try:
        response = session.post(
            TELEGRAM_API_URL.format(token=token),
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            },
            timeout=15,
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram API error: {body}")
    finally:
        session.close()


def main() -> None:
    print(" Starting MOEX Alert Script...")
    start_time = datetime.now()
    
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    threshold = float(os.environ.get("CHANGE_THRESHOLD", "3"))

    print("⏰ Checking if MOEX is trading...")
    is_trading, reason = is_moex_trading_session()
    if not is_trading:
        print(f"️  Skipping run: {reason}")
        return

    print("📊 Fetching quotes for MOEX shares (optimized: TQBR+TQTD+SMAL only)...")
    fetch_start = datetime.now()
    quotes = fetch_all_share_quotes()
    fetch_time = (datetime.now() - fetch_start).total_seconds()
    
    usable_quotes = [quote for quote in quotes if quote_is_usable(quote)]
    print(f"✅ Loaded {len(quotes)} unique securities ({len(usable_quotes)} with quotes) in {fetch_time:.1f}s")

    print(f"📈 Finding movers above {threshold}%...")
    movers = find_movers(quotes, threshold)
    print(f"🎯 Found {len(movers)} movers")

    if movers:
        displayed_movers = movers[:MAX_MOVERS_IN_MESSAGE]
        message = format_message(displayed_movers, threshold, len(quotes))
        
        if len(movers) > MAX_MOVERS_IN_MESSAGE:
            message += f"\n\n<i>... и ещё {len(movers) - MAX_MOVERS_IN_MESSAGE} бумаг</i>"
    else:
        message = format_empty_message(threshold, len(quotes))

    print(" Sending message to Telegram...")
    send_start = datetime.now()
    send_telegram_message(token, chat_id, message)
    send_time = (datetime.now() - send_start).total_seconds()
    
    total_time = (datetime.now() - start_time).total_seconds()
    print(f"✅ Message sent in {send_time:.1f}s (total: {total_time:.1f}s)")


if __name__ == "__main__":
    main()
