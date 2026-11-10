"""
Ledger_Engine (R5, R6)

모든 화면의 금액은 이 모듈의 summarize()가 계산한 값만 쓴다 (R5.7).

거래 성격(economic_type)
- expense          : 소비. 본인 부담 = 원결제액 - 적용 중인 연결 금액
- income           : 수입. 지출에 배분된 금액은 수입에서 뺀다 (R6.6)
- reimbursement    : 정산 입금 (지출과 연결된 income)
- refund           : 환불 입금
- card_settlement  : 카드대금 출금. 소비에 다시 더하지 않는다
- own_transfer     : 본인 계좌 간 이체. 소비에서 제외
- unknown          : 불명 거래. 미확인 금액으로 따로 표시
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Optional

from app import store
from app.classifier import CATEGORIES, CATEGORY_LABELS
from app.errors import not_found, unprocessable

ECONOMIC_TYPES = [
    "expense", "income", "reimbursement", "refund", "card_settlement", "own_transfer", "unknown",
]
CREDIT_TYPES = ("income", "reimbursement", "refund")  # 연결에 쓸 수 있는 입금 성격 (R6.5)

_CARD_SETTLEMENT_KEYWORDS = (
    "카드대금", "카드결제대금", "삼성카드", "신한카드", "현대카드", "국민카드", "KB카드", "KB국민카드",
    "롯데카드", "하나카드", "우리카드", "BC카드", "비씨카드", "NH카드", "농협카드", "카드출금",
)
# '주식'(주식회사), '코인'(코인노래방), '이자'(이자카야)는 일반 가맹점명에 흔해서 넣지 않는다.
_UNSUPPORTED_KEYWORDS = ("해외", "외화", "USD", "할부", "대출", "증권", "투자", "주식매수", "주식매도")
_REFUND_KEYWORDS = ("취소", "환불")
# 카드 가승인: 승인 출금과 가승인 해제 입금이 짝으로 찍히므로 소비·수입 어디에도 넣지 않고 확인 대상으로 둔다
_PREAUTH_KEYWORDS = ("가승인",)
# 간편결제 충전: 실제 소비는 충전한 지갑에서 일어나 여기서는 보이지 않는다. 소비로 두되 확인 대상으로 표시
_WALLET_TOPUP_KEYWORDS = ("페이충전", "페이머니충전", "머니충전", "네이버페이", "카카오페이", "토스페이", "페이코", "PAYCO")


def initial_economic_type(direction: str, payment_type: str, merchant_name: str,
                          owner_names: list[str]) -> tuple[str, str, list[str]]:
    """(economic_type, economic_status, flags). 규칙으로 정한 후보는 pending (R5.2)."""
    name = (merchant_name or "").upper()
    if any(k in name for k in _PREAUTH_KEYWORDS):
        return "unknown", "pending", ["preauth"]
    if direction == "in":
        if any(k in name for k in _REFUND_KEYWORDS):
            return "refund", "pending", []
        if payment_type == "transfer" and any(o and o.upper() in name for o in owner_names):
            return "own_transfer", "pending", []
        return "income", "confirmed", []
    if any(k in name for k in _UNSUPPORTED_KEYWORDS):
        return "unknown", "pending", ["unsupported"]  # R5.8
    if payment_type == "transfer":
        if any(k.upper() in name for k in _CARD_SETTLEMENT_KEYWORDS):
            return "card_settlement", "pending", []
        if any(o and o.upper() in name for o in owner_names):
            return "own_transfer", "pending", []
    if any(k.upper() in name for k in _WALLET_TOPUP_KEYWORDS):
        return "expense", "pending", ["wallet_topup"]
    return "expense", "confirmed", []


def month_of(date_str: str) -> str:
    return date_str[:7]


def load_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT t.id, t.date, t.time, t.amount_krw, t.direction, t.payment_type, t.merchant_name,
                  t.account_ref, t.source_id, t.import_id,
                  i.category, i.economic_type, i.classification_source, i.review_status,
                  i.economic_status, i.flags
           FROM transactions t JOIN interpretations i ON i.transaction_id = t.id
           ORDER BY t.date, t.rowid"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["flags"] = store.loads(d["flags"], [])
        out.append(d)
    return out


def active_allocations(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM allocations WHERE state = 'active'"
    ).fetchall()]


def allocation_sums(allocs: list[dict]) -> tuple[dict, dict]:
    by_expense: dict[str, int] = defaultdict(int)
    by_credit: dict[str, int] = defaultdict(int)
    for a in allocs:
        by_expense[a["expense_id"]] += a["amount_krw"]
        by_credit[a["credit_id"]] += a["amount_krw"]
    return by_expense, by_credit


def needs_review(row: dict) -> bool:
    """대시보드의 '미확인 거래' 기준 (R7.1).

    - 불명 거래(unknown)
    - 규칙으로 정한 성격 후보(카드대금, 본인 이체, 환불)를 아직 확인하지 않음
    - 분류에 실패해 기타(other) + pending으로 남은 지출
    """
    if row["economic_type"] == "unknown":
        return True
    if row["economic_status"] == "pending":
        return True
    return (row["direction"] == "out" and row["economic_type"] == "expense"
            and row["category"] == "other" and row["review_status"] == "pending")


def summarize(conn: sqlite3.Connection, month: Optional[str] = None,
              budget_overrides: Optional[dict] = None,
              extra_allocations: Optional[list[dict]] = None) -> dict:
    """월별 집계. extra_allocations는 변경안 미리보기용 가상 연결이다 (저장하지 않음)."""
    rows = load_rows(conn)
    months = sorted({month_of(r["date"]) for r in rows})
    if month is None:
        month = months[-1] if months else None

    allocs = active_allocations(conn) + list(extra_allocations or [])
    by_expense, by_credit = allocation_sums(allocs)
    receivables = defaultdict(int)
    for r in conn.execute("SELECT expense_id, amount_krw FROM receivables").fetchall():
        receivables[r["expense_id"]] += r["amount_krw"]

    pledge = get_pledge(conn, month) if month else None
    category_budgets = dict((pledge or {}).get("category_budgets") or {})
    if budget_overrides:
        category_budgets.update(budget_overrides)

    in_month = [r for r in rows if month and month_of(r["date"]) == month]
    agg = {c: {"total": 0, "gross": 0, "count": 0} for c in CATEGORIES}
    gross_total = allocated_total = 0
    excluded = {"card_settlement": 0, "own_transfer": 0, "preauth": 0}
    unconfirmed = {"count": 0, "amount": 0}
    income_total = income_unallocated = 0
    unallocated_credits = []
    receivable_total = 0
    unsupported = 0

    for r in in_month:
        if needs_review(r):
            unconfirmed["count"] += 1
            unconfirmed["amount"] += r["amount_krw"]
        et = r["economic_type"]
        if r["direction"] == "out":
            if et == "expense":
                linked = by_expense.get(r["id"], 0)
                net = r["amount_krw"] - linked
                cat = r["category"] if r["category"] in agg else "other"
                agg[cat]["total"] += net
                agg[cat]["gross"] += r["amount_krw"]
                agg[cat]["count"] += 1
                gross_total += r["amount_krw"]
                allocated_total += linked
                receivable_total += receivables.get(r["id"], 0)
            elif et in excluded:
                excluded[et] += r["amount_krw"]
            elif et == "unknown" and "unsupported" in r["flags"]:
                unsupported += r["amount_krw"]
            elif et == "unknown" and "preauth" in r["flags"]:
                excluded["preauth"] += r["amount_krw"]
        else:
            used = by_credit.get(r["id"], 0)
            remaining = r["amount_krw"] - used
            if et in ("income", "reimbursement"):
                # 받은 입금 전체와, 지출에 배분하고 남은 순수입을 따로 센다 (R6.6)
                income_total += r["amount_krw"]
                income_unallocated += remaining
            if et in CREDIT_TYPES and remaining > 0:
                unallocated_credits.append({"id": r["id"], "date": r["date"],
                                            "merchant_name": r["merchant_name"],
                                            "remaining_krw": remaining})

    total_net = sum(v["total"] for v in agg.values())
    ranking = []
    for cat, v in agg.items():
        if v["count"] == 0:
            continue
        budget = category_budgets.get(cat)
        ranking.append({
            "category": cat,
            "label": CATEGORY_LABELS[cat],
            "total_krw": v["total"],
            "gross_krw": v["gross"],
            "percent": round(v["total"] / total_net * 100, 2) if total_net > 0 else 0.0,
            "count": v["count"],
            "budget_krw": budget,
            "over_budget_krw": (v["total"] - budget) if budget is not None else None,
        })
    # 본인 부담 내림차순, 같으면 카테고리 코드 오름차순 (R5.5)
    ranking.sort(key=lambda x: (-x["total_krw"], x["category"]))

    total_budget = (pledge or {}).get("total_budget_krw")
    month_dates = [r["date"] for r in in_month]
    return {
        "month": month,
        "months_available": months,
        "period": {"first": min(month_dates), "last": max(month_dates)} if month_dates else None,
        "accounts": sorted({r["account_ref"] for r in in_month if r["account_ref"]}),
        "transaction_count": len(in_month),
        "total_net_krw": total_net,
        "gross_krw": gross_total,
        "allocated_krw": allocated_total,
        "receivable_krw": receivable_total,
        "budget_krw": total_budget,
        "budget_diff_krw": (total_net - total_budget) if total_budget is not None else None,
        "ranking": ranking,
        "excluded": excluded,
        "unsupported_krw": unsupported,
        "unconfirmed": unconfirmed,
        "income": {"received_krw": income_total, "net_krw": income_unallocated},
        "unallocated_credits": unallocated_credits,
        "provisional": unconfirmed["count"] > 0,
        "ledger_version": store.ledger_version(conn),
    }


def get_pledge(conn: sqlite3.Connection, month: Optional[str]) -> Optional[dict]:
    if not month:
        return None
    row = conn.execute("SELECT * FROM pledges WHERE month = ?", (month,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["category_budgets"] = store.loads(d["category_budgets"], {})
    d["adjust_categories"] = store.loads(d["adjust_categories"], [])
    d["protected_categories"] = store.loads(d["protected_categories"], [])
    return d


def get_row(conn: sqlite3.Connection, tx_id: str) -> dict:
    for r in load_rows(conn):
        if r["id"] == tx_id:
            return r
    raise not_found("거래를 찾을 수 없습니다.")


def expense_remaining(conn: sqlite3.Connection, expense_id: str) -> int:
    row = conn.execute("SELECT amount_krw FROM transactions WHERE id = ?", (expense_id,)).fetchone()
    if row is None:
        raise not_found("거래를 찾을 수 없습니다.")
    used = conn.execute(
        "SELECT COALESCE(SUM(amount_krw), 0) FROM allocations WHERE expense_id = ? AND state = 'active'",
        (expense_id,),
    ).fetchone()[0]
    return row["amount_krw"] - used


def credit_remaining(conn: sqlite3.Connection, credit_id: str) -> int:
    row = conn.execute("SELECT amount_krw FROM transactions WHERE id = ?", (credit_id,)).fetchone()
    if row is None:
        raise not_found("입금 거래를 찾을 수 없습니다.")
    used = conn.execute(
        "SELECT COALESCE(SUM(amount_krw), 0) FROM allocations WHERE credit_id = ? AND state = 'active'",
        (credit_id,),
    ).fetchone()[0]
    return row["amount_krw"] - used


def validate_links(conn: sqlite3.Connection, expense_id: str, links: list[dict]) -> list[dict]:
    """연결 제약 검증 (R6). 통과하면 [{credit_id, amount_krw, kind}]를 돌려준다.

    위반은 결과를 0원으로 잘라 숨기지 않고 422로 거부한다 (R6.2).
    """
    expense = get_row(conn, expense_id)
    if expense["direction"] != "out" or expense["economic_type"] != "expense":
        raise unprocessable("소비(지출) 거래에만 정산·환불을 연결할 수 있습니다.")
    if not links:
        raise unprocessable("연결할 입금이 없습니다.")

    seen = set()
    normalized = []
    for link in links:
        credit_id = link.get("credit_id")
        amount = link.get("amount_krw")
        if credit_id in seen:
            raise unprocessable("같은 입금을 한 지출에 두 번 연결할 수 없습니다.", {"credit_id": credit_id})
        seen.add(credit_id)
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise unprocessable("연결 금액은 양의 정수여야 합니다.", {"credit_id": credit_id})
        credit = get_row(conn, credit_id)
        if credit["direction"] != "in" or credit["economic_type"] not in CREDIT_TYPES:
            raise unprocessable(
                "실제 입금(정산·환불)만 연결할 수 있습니다. 본인 이체와 카드대금 관련 입금은 제외됩니다.",
                {"credit_id": credit_id},
            )
        dup = conn.execute(
            "SELECT 1 FROM allocations WHERE credit_id = ? AND expense_id = ? AND state = 'active'",
            (credit_id, expense_id),
        ).fetchone()
        if dup:
            raise unprocessable("이미 연결된 입금입니다.", {"credit_id": credit_id})
        remaining = credit_remaining(conn, credit_id)
        if amount > remaining:
            raise unprocessable(
                f"입금에 배분할 수 있는 금액({remaining:,}원)을 넘었습니다.",
                {"credit_id": credit_id, "remaining_krw": remaining, "requested_krw": amount},
            )
        kind = "refund" if credit["economic_type"] == "refund" else "settlement"
        normalized.append({"credit_id": credit_id, "amount_krw": amount, "kind": kind})

    total = sum(x["amount_krw"] for x in normalized)
    exp_remaining = expense_remaining(conn, expense_id)
    if total > exp_remaining:
        raise unprocessable(
            f"연결 합계({total:,}원)가 원소비의 남은 금액({exp_remaining:,}원)을 넘습니다.",
            {"expense_remaining_krw": exp_remaining, "requested_krw": total},
        )
    return normalized


def apply_links(conn: sqlite3.Connection, expense_id: str, links: list[dict],
                proposal_id: Optional[str], reason: str) -> list[str]:
    """검증된 연결을 저장한다. 호출자는 같은 트랜잭션 안에서 validate_links를 먼저 호출해야 한다."""
    event_ids = []
    for link in links:
        alloc_id = store.new_id("alloc")
        conn.execute(
            "INSERT INTO allocations(id, credit_id, expense_id, kind, amount_krw, state, proposal_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
            (alloc_id, link["credit_id"], expense_id, link["kind"], link["amount_krw"], proposal_id, store.now_iso()),
        )
        event_ids.append(store.add_event(conn, "allocation", alloc_id, None, link, reason))
        # 정산에 쓴 입금은 일반 수입이 아니라 정산 입금으로 표시한다 (R6.6)
        if link["kind"] == "settlement":
            before = conn.execute(
                "SELECT economic_type FROM interpretations WHERE transaction_id = ?", (link["credit_id"],)
            ).fetchone()["economic_type"]
            if before == "income":
                conn.execute(
                    "UPDATE interpretations SET economic_type = 'reimbursement' WHERE transaction_id = ?",
                    (link["credit_id"],),
                )
                event_ids.append(store.add_event(
                    conn, "interpretation", link["credit_id"],
                    {"economic_type": before}, {"economic_type": "reimbursement"}, reason,
                ))
    return event_ids
