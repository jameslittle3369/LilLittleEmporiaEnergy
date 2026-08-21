#!/usr/bin/env python3
"""
Emporia_Energy.py -- per-circuit energy report for an Emporia Vue account.

For every channel (circuit) on every device in the account it reports:

  * current power draw, in watts
  * kWh used today            (local midnight -> now)
  * kWh used the last 7 days  (rolling window, includes today)
  * kWh used the last 30 days (rolling window, includes today)
  * kWh used month to date    (local 1st of the month -> now)

Results always go to stdout.  With --sendemail the same numbers, plus
charts, are mailed through Gmail SMTP.

Credentials and defaults come from a .env file -- see .env.example.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate
from functools import cached_property
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency guard
    sys.exit("Missing dependency 'python-dotenv'.  Run: pip install -r requirements.txt")

try:
    import pyemvue
    from pyemvue.enums import Scale, Unit
except ImportError:  # pragma: no cover - dependency guard
    sys.exit("Missing dependency 'pyemvue'.  Run: pip install -r requirements.txt")

try:
    import requests
except ImportError:  # pragma: no cover - dependency guard
    requests = None  # only required for --push-api


# --------------------------------------------------------------------------
# Channel bookkeeping
# --------------------------------------------------------------------------

# Emporia reports a few synthetic channels alongside the real circuits.
MAINS_CHANNEL = "1,2,3"      # the mains / whole-home total on a Vue
TOTAL_CHANNEL = "TotalUsage"  # whole-home total on newer firmware
BALANCE_CHANNEL = "Balance"   # unmetered remainder (mains minus the circuits)
TOTAL_CHANNELS = (MAINS_CHANNEL, TOTAL_CHANNEL)


@dataclass
class Circuit:
    """One channel, with its usage across every window we report on."""

    device_gid: int
    channel_num: str
    device_name: str
    channel_name: str
    current_w: float | None = None
    daily_kwh: dict[date, float | None] = field(default_factory=dict)
    # Set when two channels share a name (both legs of a 240V circuit, say).
    label_override: str | None = None

    @property
    def key(self) -> tuple[int, str]:
        return (self.device_gid, self.channel_num)

    @property
    def is_total(self) -> bool:
        return self.channel_num in TOTAL_CHANNELS

    @property
    def label(self) -> str:
        if self.label_override:
            return self.label_override
        if self.is_total:
            return f"{self.device_name} (total)"
        if self.channel_num == BALANCE_CHANNEL:
            return f"{self.device_name} (unmetered balance)"
        name = self.channel_name.strip()
        if not name or name.lower() in ("unnamed", "circuit"):
            name = f"Circuit {self.channel_num}"
        return name

    def window_kwh(self, days: Sequence[date]) -> float:
        return sum(self.daily_kwh.get(d) or 0.0 for d in days)


# A trailing "1"/"2" on an otherwise identical name marks the two legs of a
# 240V circuit: "Up Comp 1" + "Up Comp 2" -> "Up Comp".
LEG_SUFFIX_RE = re.compile(r"^(?P<base>.+?)[\s_-]*(?P<leg>[12])$")


def suggest_circuit_map(circuits: Iterable[Circuit]) -> dict[str, list[str]]:
    """Group channels whose names match apart from a trailing 1 or 2.

    Channels sharing a name outright (the app lets both legs carry the same
    name) group too.  Only groups of two or more are returned.
    """
    groups: dict[str, tuple[str, list[Circuit]]] = {}
    for circuit in circuits:
        if circuit.is_total or circuit.channel_num == BALANCE_CHANNEL:
            continue
        name = (circuit.channel_name or "").strip()
        if not name:
            continue
        match = LEG_SUFFIX_RE.match(name)
        base = (match.group("base").strip() if match else name) or name
        display, members = groups.setdefault(base.lower(), (base, []))
        members.append(circuit)

    return {
        display: [f"{c.device_gid}:{c.channel_num}" for c in members]
        for display, members in groups.values()
        if len(members) > 1
    }


def load_circuit_map(path: Path) -> dict[str, list[str]]:
    """Read the combine map.  Keys starting with // or _ are comments."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object of \"Combined name\": [members]")
    mapping: dict[str, list[str]] = {}
    for name, members in data.items():
        if name.startswith(("//", "_")):
            continue
        if isinstance(members, str):
            members = [members]
        if not isinstance(members, list):
            raise ValueError(f"members for {name!r} must be a list")
        mapping[name] = [str(m).strip() for m in members if str(m).strip()]
    return mapping


def _merge_circuits(name: str, members: Sequence[Circuit]) -> Circuit:
    """Sum a group of channels into one reported circuit."""
    first = members[0]
    combined = Circuit(
        device_gid=first.device_gid,
        channel_num="+".join(m.channel_num for m in members),
        device_name=first.device_name,
        channel_name=name,
        label_override=name,
    )
    watts = [m.current_w for m in members if m.current_w is not None]
    combined.current_w = sum(watts) if watts else None
    for day in sorted({d for m in members for d in m.daily_kwh}):
        values = [m.daily_kwh.get(day) for m in members]
        present = [v for v in values if v is not None]
        combined.daily_kwh[day] = sum(present) if present else None
    return combined


def combine_circuits(circuits: list[Circuit], mapping: dict[str, list[str]]) -> list[Circuit]:
    """Replace each mapped group with a single summed circuit, in place of its first member."""
    index: dict[str, list[Circuit]] = {}
    for circuit in circuits:
        keys = (
            f"{circuit.device_gid}:{circuit.channel_num}",
            circuit.channel_num,
            circuit.channel_name.strip().lower(),
            circuit.label.strip().lower(),
        )
        for key in keys:
            if key:
                index.setdefault(key, []).append(circuit)

    merged: dict[int, Circuit] = {}   # position of first member -> combined circuit
    consumed: set[int] = set()

    for name, refs in mapping.items():
        members: list[Circuit] = []
        for ref in refs:
            matches = index.get(ref.lower()) or index.get(ref) or []
            for candidate in matches:
                if id(candidate) not in consumed and candidate not in members:
                    members.append(candidate)
        if len(members) < 2:
            warn(f"circuit map entry {name!r} matched {len(members)} channel(s); leaving it uncombined")
            continue
        members.sort(key=circuits.index)
        for member in members:
            consumed.add(id(member))
        merged[circuits.index(members[0])] = _merge_circuits(name, members)

    out: list[Circuit] = []
    for position, circuit in enumerate(circuits):
        if position in merged:
            out.append(merged[position])
        elif id(circuit) not in consumed:
            out.append(circuit)
    return out


def disambiguate_labels(circuits: Iterable[Circuit]) -> None:
    """Append the channel number to labels that would otherwise collide."""
    groups: dict[str, list[Circuit]] = {}
    for circuit in circuits:
        groups.setdefault(circuit.label, []).append(circuit)
    for label, group in groups.items():
        if len(group) > 1:
            for circuit in group:
                circuit.label_override = f"{label} (ch {circuit.channel_num})"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        warn(f"{name}={raw!r} is not an integer; using {default}")
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        warn(f"{name}={raw!r} is not a number; using {default}")
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


def _env_list(name: str) -> list[str]:
    raw = _env(name)
    return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]


@dataclass
class Config:
    username: str
    password: str
    token_file: str
    tz_name: str
    request_delay: float
    instant_scale: str
    history_days: int
    top_n: int
    line_top_n: int
    output_dir: Path
    circuit_map_file: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    email_from: str
    email_to: list[str]
    subject_prefix: str
    attach_csv: bool
    api_base_url: str

    @classmethod
    def from_env(cls) -> "Config":
        smtp_user = _env("SMTP_USER")
        return cls(
            username=_env("EMPORIA_USERNAME"),
            password=_env("EMPORIA_PASSWORD"),
            token_file=_env("EMPORIA_TOKEN_FILE", "emporia_tokens.json"),
            tz_name=_env("TIMEZONE"),
            request_delay=_env_float("REQUEST_DELAY_SECONDS", 0.35),
            instant_scale=_env("INSTANT_SCALE", "minute").lower(),
            history_days=_env_int("HISTORY_DAYS", 30),
            top_n=_env_int("CHART_TOP_N", 15),
            line_top_n=_env_int("CHART_LINE_TOP_N", 5),
            output_dir=Path(_env("OUTPUT_DIR", "reports")),
            circuit_map_file=_env("CIRCUIT_MAP_FILE", "circuit_map.json"),
            smtp_host=_env("SMTP_HOST", "smtp.gmail.com"),
            smtp_port=_env_int("SMTP_PORT", 587),
            smtp_user=smtp_user,
            smtp_password=_env("SMTP_APP_PASSWORD"),
            email_from=_env("EMAIL_FROM") or smtp_user,
            email_to=_env_list("EMAIL_TO"),
            subject_prefix=_env("EMAIL_SUBJECT_PREFIX", "Emporia Energy Report"),
            attach_csv=_env_bool("EMAIL_ATTACH_CSV", True),
            api_base_url=_env("API_BASE_URL"),
        )

    @cached_property
    def tzinfo(self):
        if self.tz_name:
            try:
                return ZoneInfo(self.tz_name)
            except (ZoneInfoNotFoundError, ValueError):
                warn(
                    f"Unknown TIMEZONE {self.tz_name!r} -- no IANA timezone database available. "
                    "Install it with 'pip install tzdata'. Falling back to system local time."
                )
        return datetime.now().astimezone().tzinfo

    @property
    def scale_for_instant(self) -> str:
        return Scale.SECOND.value if self.instant_scale.startswith("sec") else Scale.MINUTE.value

    @property
    def watts_per_kwh_bucket(self) -> float:
        """kWh in one bucket -> average watts over that bucket."""
        return 3_600_000.0 if self.instant_scale.startswith("sec") else 60_000.0


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

QUIET = False


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def info(message: str) -> None:
    if not QUIET:
        print(message, file=sys.stderr)


def with_retries(fn: Callable[[], Any], label: str, attempts: int = 3, delay: float = 2.0) -> Any:
    """Retry an API call a few times -- the Emporia cloud throttles bursts."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - the client raises a variety of errors
            if attempt == attempts:
                raise
            warn(f"{label} failed ({exc.__class__.__name__}: {exc}); retry {attempt}/{attempts - 1} in {delay:.0f}s")
            time.sleep(delay)
            delay *= 2


def fmt(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "--"
    return f"{value:,.{decimals}f}"


# --------------------------------------------------------------------------
# Emporia data collection
# --------------------------------------------------------------------------


def connect(cfg: Config) -> pyemvue.PyEmVue:
    if not cfg.username or not cfg.password:
        sys.exit("EMPORIA_USERNAME and EMPORIA_PASSWORD must be set (copy .env.example to .env).")
    vue = pyemvue.PyEmVue()
    info("Signing in to Emporia...")
    with_retries(
        lambda: vue.login(
            username=cfg.username,
            password=cfg.password,
            token_storage_file=cfg.token_file or None,
        ),
        "login",
    )
    return vue


@dataclass
class Account:
    """Everything we know about the account's devices and channels."""

    gids: list[int] = field(default_factory=list)
    circuits: dict[tuple[int, str], Circuit] = field(default_factory=dict)
    device_names: dict[int, str] = field(default_factory=dict)

    def register(self, key: tuple[int, str], channel: Any) -> Circuit:
        """Fetch (or create) the Circuit for a usage channel discovery missed."""
        circuit = self.circuits.get(key)
        if circuit is None:
            circuit = Circuit(
                device_gid=key[0],
                channel_num=key[1],
                device_name=self.device_names.get(key[0], f"Device {key[0]}"),
                channel_name=(getattr(channel, "name", "") or "").strip(),
            )
            self.circuits[key] = circuit
        elif not circuit.channel_name:
            circuit.channel_name = (getattr(channel, "name", "") or "").strip()
        return circuit


def discover_circuits(vue: pyemvue.PyEmVue, cfg: Config) -> Account:
    """Collect the device gids plus a Circuit for every named channel."""
    devices = with_retries(vue.get_devices, "get_devices")
    account = Account()
    gids, circuits = account.gids, account.circuits

    for device in devices:
        if device.device_gid not in gids:
            gids.append(device.device_gid)
        try:
            with_retries(lambda d=device: vue.populate_device_properties(d), f"device {device.device_gid} properties")
        except Exception as exc:  # noqa: BLE001
            warn(f"could not read properties for device {device.device_gid}: {exc}")
        time.sleep(cfg.request_delay)

        device_name = (getattr(device, "device_name", "") or f"Device {device.device_gid}").strip()
        account.device_names.setdefault(device.device_gid, device_name)
        for channel in getattr(device, "channels", []) or []:
            gid = getattr(channel, "device_gid", device.device_gid) or device.device_gid
            circuit = Circuit(
                device_gid=gid,
                channel_num=str(channel.channel_num),
                device_name=device_name,
                channel_name=(getattr(channel, "name", "") or "").strip(),
            )
            circuits.setdefault(circuit.key, circuit)

    info(f"Found {len(gids)} device(s) / {len(circuits)} channel(s).")
    return account


def flatten_usage(usage_devices: dict, out: dict[tuple[int, str], Any] | None = None) -> dict[tuple[int, str], Any]:
    """Walk the (possibly nested) usage tree into a flat {(gid, channel): channel} map."""
    out = {} if out is None else out
    for gid, device in (usage_devices or {}).items():
        for channel in (getattr(device, "channels", {}) or {}).values():
            channel_gid = getattr(channel, "device_gid", gid) or gid
            out[(channel_gid, str(channel.channel_num))] = channel
            nested = getattr(channel, "nested_devices", None)
            if nested:
                flatten_usage(nested, out)
    return out


def fetch_current_watts(vue: pyemvue.PyEmVue, cfg: Config, account: Account) -> None:
    info("Reading current power...")
    usage = with_retries(
        lambda: vue.get_device_list_usage(
            deviceGids=account.gids,
            instant=datetime.now(timezone.utc),
            scale=cfg.scale_for_instant,
            unit=Unit.KWH.value,
        ),
        "current usage",
    )
    for key, channel in flatten_usage(usage).items():
        circuit = account.register(key, channel)
        raw = getattr(channel, "usage", None)
        circuit.current_w = None if raw is None else float(raw) * cfg.watts_per_kwh_bucket


def fetch_daily_kwh(vue: pyemvue.PyEmVue, cfg: Config, account: Account, days: list[date]) -> None:
    """One request per day, covering every channel at once."""
    tz = cfg.tzinfo
    today = days[-1]
    info(f"Reading daily totals for {len(days)} day(s)...")

    for index, day in enumerate(days, start=1):
        if day == today:
            # "Today" is still in progress -- ask for the bucket as of right now.
            instant = datetime.now(timezone.utc)
        else:
            # Local noon sits safely inside the day's bucket whatever the offset.
            instant = datetime(day.year, day.month, day.day, 12, 0, tzinfo=tz).astimezone(timezone.utc)

        try:
            usage = with_retries(
                lambda i=instant: vue.get_device_list_usage(
                    deviceGids=account.gids, instant=i, scale=Scale.DAY.value, unit=Unit.KWH.value
                ),
                f"daily usage for {day.isoformat()}",
            )
        except Exception as exc:  # noqa: BLE001
            warn(f"no data for {day.isoformat()}: {exc}")
            continue

        for key, channel in flatten_usage(usage).items():
            circuit = account.register(key, channel)
            raw = getattr(channel, "usage", None)
            circuit.daily_kwh[day] = None if raw is None else float(raw)

        # Redraw in place on a terminal; stay silent when stderr is redirected.
        if not QUIET and sys.stderr.isatty():
            print(f"\r  {index}/{len(days)} days", end="", file=sys.stderr, flush=True)
        time.sleep(cfg.request_delay)

    if not QUIET and sys.stderr.isatty():
        print("", file=sys.stderr)


# --------------------------------------------------------------------------
# Report assembly
# --------------------------------------------------------------------------


@dataclass
class Report:
    generated_at: datetime
    tz_name: str
    days: list[date]
    circuits: list[Circuit]

    @property
    def today(self) -> date:
        return self.days[-1]

    @property
    def last_7(self) -> list[date]:
        return self.days[-7:]

    @property
    def last_30(self) -> list[date]:
        return self.days[-30:]

    @property
    def month_to_date(self) -> list[date]:
        return [d for d in self.days if (d.year, d.month) == (self.today.year, self.today.month)]

    def rows(self) -> list[dict[str, Any]]:
        out = []
        for circuit in self.circuits:
            out.append(
                {
                    "label": circuit.label,
                    "device": circuit.device_name,
                    "channel": circuit.channel_num,
                    "is_total": circuit.is_total,
                    "current_w": circuit.current_w,
                    "kwh_today": circuit.window_kwh([self.today]),
                    "kwh_last_7d": circuit.window_kwh(self.last_7),
                    "kwh_last_30d": circuit.window_kwh(self.last_30),
                    "kwh_month_to_date": circuit.window_kwh(self.month_to_date),
                }
            )
        return out

    def totals_rows(self) -> list[dict[str, Any]]:
        return [r for r in self.rows() if r["is_total"]]

    def circuit_rows(self) -> list[dict[str, Any]]:
        rows = [r for r in self.rows() if not r["is_total"]]
        rows.sort(key=lambda r: (-(r["kwh_last_30d"] or 0.0), r["label"].lower()))
        return rows

    def sum_of_circuits(self) -> dict[str, Any]:
        rows = self.circuit_rows()
        # With the Balance channel in play this sum reconciles with the mains
        # total exactly; say so rather than leaving the reader to wonder.
        has_balance = any(c.channel_num == BALANCE_CHANNEL for c in self.circuits)
        return {
            "label": "Sum of circuits + balance" if has_balance else "Sum of circuits",
            "current_w": sum(r["current_w"] or 0.0 for r in rows) if rows else 0.0,
            "kwh_today": sum(r["kwh_today"] for r in rows),
            "kwh_last_7d": sum(r["kwh_last_7d"] for r in rows),
            "kwh_last_30d": sum(r["kwh_last_30d"] for r in rows),
            "kwh_month_to_date": sum(r["kwh_month_to_date"] for r in rows),
        }

    def top_circuits(self, count: int) -> list[Circuit]:
        """Non-total circuits, heaviest 30-day consumers first."""
        ranked = [c for c in self.circuits if not c.is_total]
        ranked.sort(key=lambda c: (-c.window_kwh(self.last_30), c.label.lower()))
        return ranked[: max(1, count)]

    def whole_home_daily(self) -> list[tuple[date, float]]:
        """Daily kWh for the house: the mains channel if present, else the circuit sum."""
        totals = [c for c in self.circuits if c.is_total]
        source = totals or [c for c in self.circuits if not c.is_total]
        return [(day, sum(c.daily_kwh.get(day) or 0.0 for c in source)) for day in self.days]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "timezone": self.tz_name,
            "windows": {
                "today": self.today.isoformat(),
                "last_7d": [self.last_7[0].isoformat(), self.today.isoformat()],
                "last_30d": [self.last_30[0].isoformat(), self.today.isoformat()],
                "month_to_date": [self.month_to_date[0].isoformat(), self.today.isoformat()],
            },
            "totals": self.totals_rows(),
            "circuits": self.circuit_rows(),
            "sum_of_circuits": self.sum_of_circuits(),
            "daily_whole_home_kwh": [
                {"date": d.isoformat(), "kwh": round(v, 4)} for d, v in self.whole_home_daily()
            ],
        }


COLUMNS = [
    ("Now (W)", "current_w", 0),
    ("Today (kWh)", "kwh_today", 2),
    ("Last 7d (kWh)", "kwh_last_7d", 2),
    ("Last 30d (kWh)", "kwh_last_30d", 2),
    ("MTD (kWh)", "kwh_month_to_date", 2),
]


def render_text(report: Report) -> str:
    rows = report.totals_rows() + report.circuit_rows() + [report.sum_of_circuits()]
    name_width = max([len("Circuit")] + [len(r["label"]) for r in rows])
    widths = [max(len(head), max(len(fmt(r.get(key), dp)) for r in rows)) for head, key, dp in COLUMNS]

    lines: list[str] = []
    stamp = report.generated_at.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    lines.append(f"Emporia Energy Report -- {stamp}")
    lines.append(
        f"Today {report.today.isoformat()} | "
        f"last 7d from {report.last_7[0].isoformat()} | "
        f"last 30d from {report.last_30[0].isoformat()} | "
        f"MTD from {report.month_to_date[0].isoformat()}"
    )
    lines.append("")

    header = "Circuit".ljust(name_width) + "".join(
        "  " + head.rjust(width) for (head, _, _), width in zip(COLUMNS, widths)
    )
    lines.append(header)
    lines.append("-" * len(header))

    def emit(row: dict[str, Any]) -> None:
        lines.append(
            row["label"].ljust(name_width)
            + "".join(
                "  " + fmt(row.get(key), dp).rjust(width)
                for (_, key, dp), width in zip(COLUMNS, widths)
            )
        )

    for row in report.totals_rows():
        emit(row)
    if report.totals_rows():
        lines.append("-" * len(header))
    for row in report.circuit_rows():
        emit(row)
    lines.append("-" * len(header))
    emit(report.sum_of_circuits())
    lines.append("")
    lines.append("Rolling windows include today's partial data. '--' means the API returned no value.")
    return "\n".join(lines)


def render_html(report: Report, image_cids: Sequence[tuple[str, str]]) -> str:
    ink, ink2, muted, grid, surface = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#fcfcfb"
    font = 'font-family:system-ui,-apple-system,"Segoe UI",sans-serif;'

    def cells(row: dict[str, Any], bold: bool = False) -> str:
        weight = "600" if bold else "400"
        out = (
            f'<td style="padding:6px 10px;border-bottom:1px solid {grid};color:{ink};'
            f'font-weight:{weight};">{row["label"]}</td>'
        )
        for _, key, dp in COLUMNS:
            out += (
                f'<td style="padding:6px 10px;border-bottom:1px solid {grid};color:{ink2};'
                f'text-align:right;font-variant-numeric:tabular-nums;font-weight:{weight};">'
                f"{fmt(row.get(key), dp)}</td>"
            )
        return f"<tr>{out}</tr>"

    head = (
        f'<th style="padding:6px 10px;text-align:left;color:{muted};font-weight:600;'
        f'border-bottom:1px solid {grid};">Circuit</th>'
    )
    for label, _, _ in COLUMNS:
        head += (
            f'<th style="padding:6px 10px;text-align:right;color:{muted};font-weight:600;'
            f'border-bottom:1px solid {grid};">{label}</th>'
        )

    body = "".join(cells(r, bold=True) for r in report.totals_rows())
    body += "".join(cells(r) for r in report.circuit_rows())
    body += cells(report.sum_of_circuits(), bold=True)

    stamp = report.generated_at.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    images = "".join(
        f'<div style="margin:20px 0;"><img src="cid:{cid}" alt="{alt}" '
        f'style="max-width:100%;border:1px solid {grid};border-radius:6px;"></div>'
        for cid, alt in image_cids
    )

    return f"""\
<!DOCTYPE html>
<html><body style="margin:0;padding:20px;background:#f9f9f7;{font}">
  <div style="max-width:820px;margin:0 auto;background:{surface};padding:24px;border-radius:8px;">
    <h1 style="margin:0 0 4px;font-size:20px;color:{ink};">Emporia Energy Report</h1>
    <p style="margin:0 0 2px;font-size:13px;color:{ink2};">{stamp}</p>
    <p style="margin:0 0 20px;font-size:12px;color:{muted};">
      Today {report.today.isoformat()} &middot;
      last 7d from {report.last_7[0].isoformat()} &middot;
      last 30d from {report.last_30[0].isoformat()} &middot;
      MTD from {report.month_to_date[0].isoformat()}
    </p>
    <table style="border-collapse:collapse;width:100%;font-size:13px;">
      <thead><tr>{head}</tr></thead>
      <tbody>{body}</tbody>
    </table>
    {images}
    <p style="margin:16px 0 0;font-size:11px;color:{muted};">
      Rolling windows include today's partial data. &quot;--&quot; means the API returned no value.
    </p>
  </div>
</body></html>"""


def build_csv_bytes(report: Report) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["circuit", "device", "channel", "current_w", "kwh_today",
         "kwh_last_7d", "kwh_last_30d", "kwh_month_to_date"]
    )
    for row in report.totals_rows() + report.circuit_rows():
        writer.writerow(
            [
                row["label"], row.get("device", ""), row.get("channel", ""),
                "" if row["current_w"] is None else round(row["current_w"], 1),
                round(row["kwh_today"], 4), round(row["kwh_last_7d"], 4),
                round(row["kwh_last_30d"], 4), round(row["kwh_month_to_date"], 4),
            ]
        )
    return buffer.getvalue().encode("utf-8")


def write_csv(report: Report, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_csv_bytes(report))
    return path


# --------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------

# Single-hue sequential palette + recessive chrome; one series per chart, so
# the title names it and no legend is needed.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
BLUE = "#2a78d6"
BLUE_LIGHT = "#86b6ef"

# Categorical slots for the multi-series line chart, assigned in this fixed
# order and never cycled -- validated for adjacent-pair CVD separation against
# the light surface, so the ceiling is a hard 8 series.
CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
MAX_LINE_SERIES = len(CATEGORICAL)


def _style_axes(ax, xlabel: str = "", ylabel: str = "") -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(BASELINE)
        ax.spines[side].set_linewidth(1.0)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    for label in ax.get_xticklabels() + ax.get_yticklabels():
        label.set_color(INK_2)
    if xlabel:
        ax.set_xlabel(xlabel, color=MUTED, fontsize=9, labelpad=8)
    if ylabel:
        ax.set_ylabel(ylabel, color=MUTED, fontsize=9, labelpad=8)


def chart_ranked_bars(
    labels: Sequence[str], values: Sequence[float], title: str, xlabel: str, path: Path, decimals: int = 2
) -> tuple[Path, str] | None:
    """Horizontal bars, sorted high -> low, with a direct label on each bar.

    `title` may contain a {n} placeholder for the number of bars actually drawn.
    Returns the path plus the resolved title, or None if there is nothing to plot.
    """
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    # Drop anything that would render as a zero-length bar with a "0" label.
    pairs = [(l, v or 0.0) for l, v in zip(labels, values)]
    pairs = [p for p in pairs if round(p[1], decimals) > 0]
    if not pairs:
        return None
    pairs.sort(key=lambda p: p[1])

    names = [p[0] for p in pairs]
    nums = [p[1] for p in pairs]
    height = 1.6 + 0.34 * len(pairs)
    fig, ax = plt.subplots(figsize=(9, height), dpi=140)
    fig.patch.set_facecolor(SURFACE)

    ax.barh(range(len(nums)), nums, height=0.62, color=BLUE, linewidth=0)
    ax.set_yticks(range(len(names)), names)
    ax.set_xlim(0, max(nums) * 1.16)
    ax.xaxis.grid(True, color=GRID, linewidth=1.0)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.set_axisbelow(True)
    _style_axes(ax, xlabel=xlabel)
    ax.spines["left"].set_visible(False)

    span = max(nums) * 0.012
    for index, value in enumerate(nums):
        ax.text(value + span, index, f"{value:,.{decimals}f}", va="center", ha="left",
                fontsize=9, color=INK_2)

    resolved = title.format(n=len(pairs))
    ax.set_title(resolved, color=INK, fontsize=13, loc="left", pad=14)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return path, resolved


def chart_daily_totals(series: Sequence[tuple[date, float]], path: Path) -> Path | None:
    """Columns of whole-home kWh per day; today's partial bar is a lighter step."""
    import matplotlib.pyplot as plt

    series = [(d, v or 0.0) for d, v in series]
    if not series or all(v == 0 for _, v in series):
        return None

    days = [d for d, _ in series]
    values = [v for _, v in series]
    colors = [BLUE] * len(values)
    colors[-1] = BLUE_LIGHT  # today is still accumulating

    fig, ax = plt.subplots(figsize=(9, 3.8), dpi=140)
    fig.patch.set_facecolor(SURFACE)
    ax.bar(range(len(values)), values, width=0.68, color=colors, linewidth=0)

    # Step back from the last day so today always gets a tick and the labels
    # stay evenly spaced -- counting forward can leave two of them colliding.
    step = max(1, len(days) // 12)
    ticks = sorted(range(len(days) - 1, -1, -step))
    ax.set_xticks(ticks, [days[i].strftime("%b %d") for i in ticks], rotation=0)
    ax.yaxis.grid(True, color=GRID, linewidth=1.0)
    ax.set_axisbelow(True)
    ax.set_ylim(0, max(values) * 1.18)
    _style_axes(ax, ylabel="kWh")

    # Label only the peak day and today, not every column.
    peak = max(range(len(values)), key=lambda i: values[i])
    for index in {peak, len(values) - 1}:
        ax.text(index, values[index] + max(values) * 0.03, f"{values[index]:,.1f}",
                ha="center", va="bottom", fontsize=9, color=INK_2)

    ax.set_title("Whole-home kWh per day", color=INK, fontsize=13, loc="left", pad=14)
    ax.text(0.0, 1.0, f"lighter bar = {days[-1].strftime('%b %d')} (partial day)",
            transform=ax.transAxes, ha="left", va="bottom", fontsize=9, color=MUTED)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return path


def chart_daily_by_circuit(
    circuits: Sequence[Circuit], days: Sequence[date], path: Path
) -> tuple[Path, str] | None:
    """One line per circuit over the history window, highest 30-day total first."""
    import matplotlib.pyplot as plt

    if len(days) < 2:
        return None
    series = [(c.label, [c.daily_kwh.get(d) or 0.0 for d in days]) for c in circuits]
    series = [s for s in series if any(v > 0 for v in s[1])]
    if not series:
        return None

    fig, ax = plt.subplots(figsize=(9, 4.8), dpi=140)
    fig.patch.set_facecolor(SURFACE)
    positions = list(range(len(days)))

    for index, (name, values) in enumerate(series):
        color = CATEGORICAL[index]
        # Solid through the last complete day, dashed into today's partial one.
        ax.plot(positions[:-1], values[:-1], color=color, linewidth=2.0, label=name,
                solid_capstyle="round", solid_joinstyle="round")
        ax.plot(positions[-2:], values[-2:], color=color, linewidth=2.0,
                linestyle=(0, (3, 2)), solid_capstyle="round")

    peak = max(max(values) for _, values in series)
    ax.set_ylim(0, peak * 1.12)
    ax.set_xlim(-0.5, len(days) - 0.5 + (len(days) * 0.16 if len(series) <= 4 else 0))
    step = max(1, len(days) // 12)
    ticks = sorted(range(len(days) - 1, -1, -step))
    ax.set_xticks(ticks, [days[i].strftime("%b %d") for i in ticks])
    ax.yaxis.grid(True, color=GRID, linewidth=1.0)
    ax.set_axisbelow(True)
    _style_axes(ax, ylabel="kWh")

    # A legend is always present for 2+ series; 4 or fewer are also labelled
    # directly at the line end, so identity is never carried by color alone.
    # The end-dot carries the color; the text itself stays in ink, since a light
    # categorical hue (yellow, aqua) is illegible as text on the surface.
    if len(series) <= 4:
        for index, (name, values) in enumerate(series):
            ax.plot([positions[-1]], [values[-1]], marker="o", markersize=8, linestyle="none",
                    color=CATEGORICAL[index], zorder=3)
            ax.text(positions[-1] + 0.45, values[-1], name, color=INK_2,
                    va="center", ha="left", fontsize=9)
    legend = ax.legend(
        loc="upper center", bbox_to_anchor=(0.5, -0.13), ncol=min(len(series), 3),
        frameon=False, fontsize=9, handlelength=1.6, columnspacing=1.8, labelcolor=INK_2,
    )
    for handle in legend.get_lines():
        handle.set_linewidth(2.0)

    title = f"Daily kWh by circuit (top {len(series)})"
    ax.set_title(title, color=INK, fontsize=13, loc="left", pad=14)
    ax.text(0.0, 1.0, f"dashed = {days[-1].strftime('%b %d')} (partial day)",
            transform=ax.transAxes, ha="left", va="bottom", fontsize=9, color=MUTED)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    return path, title


def build_charts(report: Report, out_dir: Path, top_n: int, line_top_n: int = 5) -> list[tuple[Path, str]]:
    try:
        import matplotlib
    except ImportError:
        warn("matplotlib is not installed; skipping charts (pip install -r requirements.txt)")
        return []
    matplotlib.use("Agg")
    matplotlib.rcParams["font.family"] = "sans-serif"
    matplotlib.rcParams["font.sans-serif"] = ["Segoe UI", "DejaVu Sans", "Arial"]

    rows = report.circuit_rows()[: max(1, top_n)]
    stamp = report.generated_at.strftime("%Y%m%d-%H%M")
    # {n} is filled in with the bars actually drawn, after zero circuits drop out.
    specs = [
        ("current_w", "Current power by circuit (top {n})", "watts", 0, f"current-watts-{stamp}.png"),
        ("kwh_today", "kWh today by circuit (top {n})", "kWh", 2, f"kwh-today-{stamp}.png"),
        ("kwh_last_30d", "kWh last 30 days by circuit (top {n})", "kWh", 1, f"kwh-30d-{stamp}.png"),
    ]

    charts: list[tuple[Path, str]] = []
    for key, title, xlabel, decimals, filename in specs:
        result = chart_ranked_bars(
            [r["label"] for r in rows], [r.get(key) or 0.0 for r in rows],
            title, xlabel, out_dir / filename, decimals,
        )
        if result:
            charts.append(result)

    daily = chart_daily_totals(report.whole_home_daily(), out_dir / f"kwh-daily-{stamp}.png")
    if daily:
        charts.append((daily, "Whole-home kWh per day"))

    # Categorical hues are never cycled, so the line chart caps at 8 series.
    wanted = max(1, line_top_n)
    if wanted > MAX_LINE_SERIES:
        warn(
            f"line chart limited to {MAX_LINE_SERIES} circuits (asked for {wanted}) -- past that "
            "the series colors stop being reliably distinguishable; the bar charts still show all "
            f"{len(rows)}"
        )
        wanted = MAX_LINE_SERIES
    by_circuit = chart_daily_by_circuit(
        report.top_circuits(wanted), report.days, out_dir / f"kwh-daily-by-circuit-{stamp}.png"
    )
    if by_circuit:
        charts.append(by_circuit)
    return charts


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------


def send_email(cfg: Config, report: Report, charts: Sequence[tuple[Path, str]]) -> None:
    missing = [
        name
        for name, value in (
            ("SMTP_USER", cfg.smtp_user),
            ("SMTP_APP_PASSWORD", cfg.smtp_password),
            ("EMAIL_TO", cfg.email_to),
        )
        if not value
    ]
    if missing:
        sys.exit(f"--sendemail needs these set in .env: {', '.join(missing)}")

    cids = [(f"chart{index}", alt) for index, (_, alt) in enumerate(charts)]

    msg = EmailMessage()
    msg["From"] = cfg.email_from
    msg["To"] = ", ".join(cfg.email_to)
    msg["Date"] = formatdate(localtime=True)
    msg["Subject"] = f"{cfg.subject_prefix} -- {report.generated_at.strftime('%Y-%m-%d %H:%M')}"
    msg.set_content(render_text(report))
    msg.add_alternative(render_html(report, cids), subtype="html")

    html_part = msg.get_payload()[-1]
    for (path, _), (cid, _) in zip(charts, cids):
        html_part.add_related(
            path.read_bytes(), maintype="image", subtype="png", cid=f"<{cid}>", filename=path.name
        )

    if cfg.attach_csv:
        msg.add_attachment(
            build_csv_bytes(report),
            maintype="text",
            subtype="csv",
            filename=f"emporia-{report.generated_at.strftime('%Y%m%d-%H%M')}.csv",
        )

    info(f"Sending mail via {cfg.smtp_host}:{cfg.smtp_port} to {', '.join(cfg.email_to)}...")
    if cfg.smtp_port == 465:
        server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=60)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=60)
    try:
        server.ehlo()
        if cfg.smtp_port != 465:
            server.starttls()
            server.ehlo()
        server.login(cfg.smtp_user, cfg.smtp_password)
        server.send_message(msg)
    finally:
        server.quit()
    info("Mail sent.")


# --------------------------------------------------------------------------
# API push (--push-api)
# --------------------------------------------------------------------------


def push_circuits_to_api(cfg: Config, report: Report) -> int:
    """POST each circuit's current reading to sensors-backend-fastapi.

    Used for the scheduled/automated run -- skips report/chart/email
    entirely. Circuits with no current reading (current_w is None, the
    API returned nothing this cycle) are skipped rather than logging a
    fabricated zero.
    """
    if requests is None:
        sys.exit("--push-api needs the 'requests' package.  Run: pip install -r requirements.txt")
    if not cfg.api_base_url:
        sys.exit("--push-api needs API_BASE_URL set in .env")

    base = cfg.api_base_url.rstrip("/")
    pushed = 0
    for circuit in report.circuits:
        if circuit.current_w is None:
            continue
        # device_gid:channel_num -- unlike channel_num alone, this pair is
        # guaranteed unique even across accounts with multiple devices
        # (two devices can each have a mains channel literally named
        # "1,2,3"), so it's the right key to re-derive from row dicts too.
        external_id = f"{circuit.device_gid}:{circuit.channel_num}"
        url = f"{base}/energy-circuits/emporia/{external_id}/log"
        body = {
            "name": circuit.label,
            "watts": circuit.current_w,
            "kwh_today": circuit.window_kwh([report.today]),
            "kwh_7d": circuit.window_kwh(report.last_7),
            "kwh_30d": circuit.window_kwh(report.last_30),
            "kwh_mtd": circuit.window_kwh(report.month_to_date),
        }
        try:
            response = requests.post(url, json=body, timeout=10)
            response.raise_for_status()
            pushed += 1
        except requests.RequestException as exc:
            warn(f"failed to push circuit {circuit.label!r} to API: {exc}")

    info(f"Pushed {pushed}/{len(report.circuits)} circuit(s) to {base}.")
    return 0 if pushed else 1


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="Emporia_Energy.py",
        description="Per-circuit Emporia Vue energy report: current watts, today, last 7d, last 30d, month to date.",
    )
    parser.add_argument("--push-api", action="store_true",
                        help="POST each circuit's current reading to sensors-backend-fastapi "
                             "(API_BASE_URL) and exit -- no report/chart/email output. This is "
                             "what the scheduled/automated run uses.")
    parser.add_argument("--sendemail", action="store_true",
                        help="additionally email the report and charts via Gmail SMTP")
    parser.add_argument("--charts", action="store_true",
                        help="render chart PNGs to the output directory without emailing")
    parser.add_argument("--days", type=int, default=None,
                        help="days of history to pull (default 30, or HISTORY_DAYS)")
    parser.add_argument("--top", type=int, default=None,
                        help="how many circuits to plot per bar chart (default 15, or CHART_TOP_N)")
    parser.add_argument("--top-lines", type=int, default=None,
                        help=f"how many circuits to plot in the daily line chart, max "
                             f"{MAX_LINE_SERIES} (default 5, or CHART_LINE_TOP_N)")
    parser.add_argument("--output-dir", default=None,
                        help="where charts and CSVs are written (default ./reports, or OUTPUT_DIR)")
    parser.add_argument("--circuit-map", metavar="PATH", default=None,
                        help="combine map to apply (default circuit_map.json, or CIRCUIT_MAP_FILE)")
    parser.add_argument("--generate-circuit-map", action="store_true",
                        help="write a combine map for this account (groups names differing only by a "
                             "trailing 1/2) and exit; will not overwrite without --force")
    parser.add_argument("--force", action="store_true",
                        help="allow --generate-circuit-map to overwrite an existing map")
    parser.add_argument("--no-combine", action="store_true",
                        help="report every channel separately, ignoring the combine map")
    parser.add_argument("--csv", metavar="PATH", default=None, help="also write the summary to a CSV file")
    parser.add_argument("--json", action="store_true", help="print JSON to stdout instead of a table")
    parser.add_argument("--env-file", default=".env", help="path to the .env file (default .env)")
    parser.add_argument("--quiet", action="store_true", help="suppress progress messages on stderr")
    return parser.parse_args(argv)


def generate_circuit_map(account: Account, path: Path, force: bool = False) -> int:
    """Write a combine map seeded from the account's channel names."""
    if path.exists() and not force:
        print(f"{path} already exists; pass --force to overwrite it.", file=sys.stderr)
        return 1

    circuits = list(account.circuits.values())
    suggested = suggest_circuit_map(circuits)
    if not suggested:
        print("No channels look like a matched pair; nothing to write.", file=sys.stderr)
        return 1

    by_ref = {f"{c.device_gid}:{c.channel_num}": c for c in circuits}
    document: dict[str, Any] = {
        "//": (
            "Combine map for Emporia_Energy.py. Each entry sums its member channels into one "
            "reported circuit -- typically the two legs of a 240V circuit. Members are "
            "\"<device_gid>:<channel>\"; a bare channel number or a channel name also works. "
            "Delete an entry to report those channels separately, or run with --no-combine to "
            "ignore this file entirely. Keys starting with // or _ are comments."
        )
    }
    for name, refs in suggested.items():
        document[f"_{name}"] = " + ".join(
            by_ref[ref].channel_name or ref for ref in refs if ref in by_ref
        )
        document[name] = refs

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {path} with {len(suggested)} combined circuit(s):", file=sys.stderr)
    for name, refs in suggested.items():
        members = ", ".join(by_ref[ref].channel_name or ref for ref in refs if ref in by_ref)
        print(f"  {name}  <-  {members}", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    global QUIET
    args = parse_args(argv)
    QUIET = args.quiet

    env_path = Path(args.env_file)
    if env_path.is_file():
        load_dotenv(env_path)
    else:
        load_dotenv()  # fall back to any .env on the default search path
        if args.env_file == ".env":
            warn("no .env file found; relying on existing environment variables")

    cfg = Config.from_env()
    if args.days is not None:
        cfg.history_days = args.days
    if args.top is not None:
        cfg.top_n = args.top
    if args.top_lines is not None:
        cfg.line_top_n = args.top_lines
    if args.output_dir:
        cfg.output_dir = Path(args.output_dir)

    tz = cfg.tzinfo
    now = datetime.now(tz)
    today = now.date()
    # Always cover at least the current month so month-to-date is complete.
    span = max(cfg.history_days, 30, today.day)
    days = [today - timedelta(days=offset) for offset in range(span - 1, -1, -1)]

    map_path = Path(args.circuit_map or cfg.circuit_map_file)

    vue = connect(cfg)
    account = discover_circuits(vue, cfg)
    if not account.gids:
        sys.exit("No devices found on this Emporia account.")

    if args.generate_circuit_map:
        return generate_circuit_map(account, map_path, force=args.force)

    fetch_current_watts(vue, cfg, account)
    fetch_daily_kwh(vue, cfg, account, days)

    circuits = list(account.circuits.values())
    if args.no_combine:
        pass
    elif map_path.is_file():
        try:
            mapping = load_circuit_map(map_path)
        except (ValueError, json.JSONDecodeError) as exc:
            sys.exit(f"{map_path}: {exc}")
        if mapping:
            before = len(circuits)
            circuits = combine_circuits(circuits, mapping)
            info(f"Applied {map_path}: {before} channels -> {len(circuits)} circuits.")
    else:
        info(f"No combine map at {map_path} (create one with --generate-circuit-map).")
    disambiguate_labels(circuits)

    report = Report(
        generated_at=now,
        tz_name=str(tz),
        days=days,
        circuits=circuits,
    )

    if args.push_api:
        return push_circuits_to_api(cfg, report)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_text(report))

    if args.csv:
        path = write_csv(report, Path(args.csv))
        info(f"Wrote {path}")

    charts: list[tuple[Path, str]] = []
    if args.charts or args.sendemail:
        charts = build_charts(report, cfg.output_dir, cfg.top_n, cfg.line_top_n)
        for path, _ in charts:
            info(f"Wrote {path}")

    if args.sendemail:
        send_email(cfg, report, charts)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
