"""
Forecast_Engine (R13)

예상 소비 = 등록한 반복·예정 소비 + 생활 소비 추정 + 등록한 일회성 소비
생활 소비 추정 = 최근 완결된 최대 8주의 본인 부담 생활비 일평균 × 다음 달 일수
관측이 4주 미만이면 사용자 예산을 그대로 쓰고 '입력 계획'으로 표시한다.
확률이나 신뢰구간은 만들지 않는다.
"""

from __future__ import annotations

import calendar
import math
import sqlite3
from datetime import date, timedelta
from typing import Optional

from app import ledger, store
from app.errors import bad_request


def next_month(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{y + (m // 12)}-{m % 12 + 1:02d}"


def days_in_month(month: str) -> int:
    y, m = map(int, month.split("-"))
    return calendar.monthrange(y, m)[1]


def add_months(d: date, n: int) -> date:
    total = d.month - 1 + n
    y, m = d.year + total // 12, total % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _int(value, name: str, allow_zero: bool = True) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise bad_request(f"{name}은(는) 원화 정수여야 합니다.")
    value = int(value)
    if value < 0 or (value == 0 and not allow_zero) or value > 999_999_999:
        raise bad_request(f"{name}은(는) 0 이상 999,999,999 이하여야 합니다.")
    return value


def goal_projection(goal: Optional[dict]) -> Optional[dict]:
    """저축 목표 달성 시점 (R13.8~R13.11). 현재 저축액은 사용자가 입력한 값만 쓴다."""
    if not goal:
        return None
    target = _int(goal.get("target_krw"), "목표 금액")
    saved = _int(goal.get("current_saved_krw"), "현재 저축액") or 0
    monthly = goal.get("monthly_contribution_krw")
    first = goal.get("first_contribution_date")
    if target is None:
        return None
    if saved >= target:
        return {"status": "completed", "remaining_krw": 0, "assumption": None}
    remaining = target - saved
    if monthly is None or monthly == "" or int(monthly) <= 0:
        return {"status": "insufficient", "remaining_krw": remaining,
                "message": "월 납입액이 없거나 0 이하라 달성 시점을 계산하지 않습니다."}
    monthly = _int(monthly, "월 납입액")
    count = math.ceil(remaining / monthly)
    try:
        first_date = date.fromisoformat(first) if first else None
    except ValueError:
        raise bad_request("첫 납입일은 YYYY-MM-DD 형식이어야 합니다.")
    achieved = add_months(first_date, count - 1).isoformat() if first_date else None
    return {"status": "in_progress", "remaining_krw": remaining, "payments_needed": count,
            "achieved_on": achieved,
            "assumption": "이자·수익률 없이 같은 납입액이 유지된다고 가정했습니다."}


def preview(conn: sqlite3.Connection, base_month: Optional[str], body: dict) -> dict:
    summary = ledger.summarize(conn, base_month)
    base_month = summary["month"]
    if not base_month:
        raise bad_request("거래 데이터가 없습니다.")
    target_month = next_month(base_month)
    days = days_in_month(target_month)
    pledge = ledger.get_pledge(conn, target_month) or ledger.get_pledge(conn, base_month) or {}

    items = body.get("planned_items") or []
    recurring = one_off = cashflow = 0
    keywords = []
    for it in items:
        amount = _int(it.get("amount_krw"), "예정 항목 금액") or 0
        if it.get("forecast_kind") == "cashflow":
            cashflow += amount  # 카드대금 청구액 등은 소비 전망에 더하지 않는다 (R13.4)
            continue
        if it.get("kind") == "one_off":
            one_off += amount
        else:
            recurring += amount
        if it.get("merchant_keyword"):
            keywords.append(str(it["merchant_keyword"]).upper())

    # 생활 소비: 반복·예정으로 등록한 거래는 제외 (R13.2)
    rows = ledger.load_rows(conn)
    by_expense, _ = ledger.allocation_sums(ledger.active_allocations(conn))
    expenses = [r for r in rows if r["direction"] == "out" and r["economic_type"] == "expense"
                and not any(k in r["merchant_name"].upper() for k in keywords)]
    all_dates = [date.fromisoformat(r["date"]) for r in rows]
    living = None
    weeks = 0
    window = None
    if all_dates:
        last, first = max(all_dates), min(all_dates)
        weeks = min(8, ((last - first).days + 1) // 7)
        if weeks >= 4:
            start = last - timedelta(days=weeks * 7 - 1)
            total = sum(r["amount_krw"] - by_expense.get(r["id"], 0) for r in expenses
                        if start <= date.fromisoformat(r["date"]) <= last)
            living = round(total / (weeks * 7) * days)
            window = {"start": start.isoformat(), "end": last.isoformat()}

    if living is None:
        budget = pledge.get("total_budget_krw")
        base_total = budget if budget is not None else None
        basis = "input_plan"
    else:
        base_total = recurring + living + one_off
        basis = "observed"

    scenario = body.get("scenario") or []
    reduction = 0
    scenario_out = []
    for s in scenario:
        frm = _int(s.get("from_krw"), "변경 전 금액") or 0
        to = _int(s.get("to_krw"), "변경 후 금액") or 0
        diff = max(0, frm - to)
        reduction += diff
        scenario_out.append({"label": s.get("label") or "", "from_krw": frm, "to_krw": to, "reduction_krw": diff})

    income = _int(body.get("income_krw"), "예상 수입")
    if income is None:
        income = pledge.get("income_krw")
    modified_total = (base_total - reduction) if base_total is not None else None

    def residual(total):
        return (income - total) if (income is not None and total is not None) else None

    return {
        "base_month": base_month,
        "target_month": target_month,
        "days_in_month": days,
        "basis": basis,
        "basis_note": ("최근 관측 생활비로 추정했습니다." if basis == "observed"
                       else "관측 기간이 4주 미만이라 예측이 아닌 입력 계획(예산)을 사용합니다."),
        "observed_weeks": weeks,
        "observation_window": window,
        "components": {"recurring_krw": recurring, "living_krw": living, "one_off_krw": one_off},
        "cashflow_only_krw": cashflow,
        "base_total_krw": base_total,
        "scenario": scenario_out,
        "reduction_krw": reduction,
        "modified_total_krw": modified_total,
        "income_krw": income,
        "residual_base_krw": residual(base_total),
        "residual_modified_krw": residual(modified_total),
        "residual_note": "소득 대비 소비 잔여는 실제 계좌 잔액이나 지금 송금 가능한 금액이 아닙니다.",
        "scenario_assumption": "다른 소비와 수입이 같다는 가정입니다.",
        "provisional": summary["provisional"],
        "goal": goal_projection(body.get("goal")),
        "ledger_version": summary["ledger_version"],
    }


def save_plan(conn: sqlite3.Connection, month: str, data: dict) -> dict:
    row = conn.execute("SELECT version FROM plans WHERE month = ?", (month,)).fetchone()
    version = (row["version"] + 1) if row else 1
    conn.execute(
        "INSERT INTO plans(month, data, version, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(month) DO UPDATE SET data = excluded.data, version = excluded.version, "
        "updated_at = excluded.updated_at",
        (month, store.dumps(data), version, store.now_iso()),
    )
    store.add_event(conn, "plan", month, None, {"version": version}, "계획 저장")
    return {"month": month, "version": version}
