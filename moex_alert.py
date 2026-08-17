#!/usr/bin/env python3
"""Fetch TOP-100 MOEX stock quotes and post movers (>3% from day open) to Telegram."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

MOEX_TOP_URL = "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json"
MOEX_STATUS_URL = (
    "https://iss.moex.com/iss/engines/stock/markets/shares/"
    "boards/TQBR/securities/SBER.json"
)
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
MSK = ZoneInfo("Europe/Moscow")
TOP_N = 100
MAX_MOVERS_IN_MESSAGE = 30


@dataclass(frozen=True)
class Quote:
    secid: str
    shortname: str
    open_price: float
    last_price: float


@dataclass(frozen=True)
class StockMove:
    secid: str
    shortname: str
    open_price: float
    last_price: float
    change_pct: float


def create_session() -> requests.Session:
    """Create optimized HTTP session with retries."""
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
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
    return {date.fromisoformat(item.strip()) for item in raw.split(",") if item.strip()}


def is_weekday(day: date) -> bool:
    return day.weekday() < 5


def is_within_trading_hours(now: datetime, start: time, end: time) -> bool:
    current = now.time().replace(tzinfo=None)
    return start <= current <= end


def market_has_today_quotes() -> bool:
    """Check if market is trading using SBER ticker."""
    session = create_session()
    try:
        response = session.get(
            MOEX_STATUS_URL,
            params={"iss.meta": "off", "iss.only": "marketdata", "marketdata.columns": "SYSTIME,OPEN,LAST"},
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
            raise RuntimeError("Не удалось проверить статус рынка MOEX") from exc

    return True, "торговая сессия активна"


def fetch_top_100_quotes() -> list[Quote]:
    """Fetch TOP-100 MOEX stocks by trading volume."""
    session = create_session()
    try:
        # Загружаем все акции TQBR с объемами торгов
        all_quotes = []
        start = 0
        page_size = 100

        while True:
            response = session.get(
                MOEX_TOP_URL,
                params={
                    "iss.meta": "off",
                    "iss.only": "securities,marketdata",
                    "securities.columns": "SECID,SHORTNAME",
                    "marketdata.columns": "SECID,OPEN,LAST,VALUE",
                    "start": start,
                    "limit": page_size,
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
                        "LAST": md_row[md_index["LAST"]],
                        "VALUE": md_row[md_index["VALUE"]]
                    }

            for sec_row in securities:
                secid = sec_row[sec_index["SECID"]]
                shortname = sec_row[sec_index["SHORTNAME"]]
                md = md_dict.get(secid, {})

                open_raw = md.get("OPEN")
                last_raw = md.get("LAST")
                value_raw = md.get("VALUE")

                if open_raw is None or last_raw is None:
                    continue

                open_price = float(open_raw)
                last_price = float(last_raw)
                volume = float(value_raw) if value_raw is not None else 0

                if open_price > 0 and last_price > 0:
                    all_quotes.append((secid, shortname, open_price, last_price, volume))

            if len(securities) < page_size:
                break
            start += page_size

        # Сортируем по объему торгов и берем топ-100
        all_quotes.sort(key=lambda x: x[4], reverse=True)
        top_quotes = [
            Quote(secid=q[0], shortname=q[1], open_price=q[2], last_price=q[3])
            for q in all_quotes[:TOP_N]
        ]

        return top_quotes
    finally:
        session.close()


def find_movers(quotes: list[Quote], threshold_pct: float) -> list[StockMove]:
    movers = []
    for quote in quotes:
        change_pct = (quote.last_price - quote.open_price) / quote.open_price * 100
        if abs(change_pct) > threshold_pct:
            movers.append(
                StockMove(
                    secid=quote.secid,
                    shortname=quote.shortname,
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
        f"<b>MOEX ТОП-{TOP_N} — изменение &gt; {threshold_pct:g}% с открытия</b>\n"
        f"<i>{now.strftime('%d.%m.%Y %H:%M')} MSK</i>\n"
        f"Проверено бумаг: {securities_checked}\n\n"
    )


def format_message(movers: list[StockMove], threshold_pct: float, securities_checked: int) -> str:
    header = format_header(threshold_pct, securities_checked)
    lines = []
    for move in movers:
        direction = "" if move.change_pct > 0 else ""
        sign = "+" if move.change_pct > 0 else ""
        lines.append(
            f"{direction} <b>{move.secid}</b> ({move.shortname}): "
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
    print(" Starting MOEX Alert Script (TOP-100)...")
    start_time = datetime.now()

    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    threshold = float(os.environ.get("CHANGE_THRESHOLD", "3"))

    print("⏰ Checking if MOEX is trading...")
    is_trading, reason = is_moex_trading_session()
    if not is_trading:
        print(f"️  Skipping run: {reason}")
        return

    print(f"📊 Fetching TOP-{TOP_N} MOEX stocks by volume...")
    fetch_start = datetime.now()
    quotes = fetch_top_100_quotes()
    fetch_time = (datetime.now() - fetch_start).total_seconds()
    print(f"✅ Loaded {len(quotes)} stocks in {fetch_time:.1f}s")

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
