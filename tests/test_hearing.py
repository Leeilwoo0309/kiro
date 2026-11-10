"""소비 결정, 기간 경계, 정산 금액과 기존 DB 호환성 회귀 테스트."""
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import ai_adapter, hearing, store
from app.errors import AppError
from app.main import app


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ai_adapter, "is_configured", lambda: False)
    store.init_db()
    return tmp_path


def add(day, amount=5000, merchant="편의점", category="food", account="A", kind="expense", direction="out"):
    with store.tx() as conn:
        tid = store.new_id("tx")
        conn.execute("INSERT INTO transactions(id,date,time,amount_krw,direction,payment_type,merchant_name,account_ref) VALUES (?,?,?,?,?,?,?,?)",
                     (tid, day, "01:00", amount, direction, "debit", merchant, account))
        conn.execute("INSERT INTO interpretations(transaction_id,category,economic_type,classification_source,review_status) VALUES (?,?,?,'user','confirmed')",
                     (tid, category, kind))
        return tid


def seed():
    for day in range(1, 15):
        add(f"2026-10-{day:02d}", 5000 if day <= 7 else 10000)


def test_rolling_comparison_and_disjoint_cases(db):
    seed()
    h = hearing.create(days=7, use_ai=False)
    assert (h["start_date"], h["end_date"]) == ("2026-10-08", "2026-10-14")
    assert h["summary"]["period_total_krw"] == 70000
    assert h["comparison"]["total_krw"] == 35000
    assert h["comparison"]["available"]
    assert h["cases"][0]["kind"] == "increase"
    ids = [tid for c in h["cases"] for tid in c["tx_ids"]]
    assert len(ids) == len(set(ids))
    assert hearing.latest(days=7)["id"] == h["id"]
    assert not h["can_apply_month"]
    with pytest.raises(AppError):
        hearing.apply(h["id"])


def test_partial_coverage_not_zero(db):
    add("2026-10-12")
    add("2026-10-14")
    h = hearing.create(days=7, use_ai=False)
    assert not h["comparison"]["available"]
    assert h["comparison"]["total_krw"] is None
    assert all(c["kind"] != "increase" for c in h["cases"])


def test_new_account_disables_incomplete_comparison(db):
    seed()
    add("2026-10-13", account="B")
    assert not hearing.create(days=7)["comparison"]["available"]


def test_custom_cross_month_and_latest_isolation(db):
    add("2026-09-30")
    add("2026-10-01")
    h = hearing.create(start_date="2026-09-30", end_date="2026-10-01", use_ai=False)
    assert h["summary"]["period_total_krw"] == 10000
    assert hearing.latest(start_date="2026-09-30", end_date="2026-10-01")["id"] == h["id"]
    assert hearing.latest(month="2026-10") is None


@pytest.mark.parametrize("kwargs", [
    {"month": "2026-13"}, {"days": 8}, {"start_date": "2026-10-01"},
    {"start_date": "2026-10-02", "end_date": "2026-10-01"},
    {"start_date": "bad", "end_date": "2026-10-01"},
    {"month": "2026-10", "start_date": "2026-10-01", "end_date": "2026-10-02"},
])
def test_invalid_periods(db, kwargs):
    add("2026-10-01")
    with pytest.raises(AppError) as e:
        hearing.create(**kwargs)
    assert e.value.status == 400


def test_no_transactions(db):
    assert hearing.latest() is None
    with pytest.raises(AppError, match="거래 데이터"):
        hearing.create()


def test_choices_do_not_call_ai_or_count_one_off_as_savings(db, monkeypatch):
    seed()
    call = Mock(side_effect=AssertionError("Explicit choices must not call AI"))
    monkeypatch.setattr(ai_adapter, "call_json", call)
    for choice, status in [("keep", "kept"), ("one_off", "one_off")]:
        h = hearing.create(days=7)
        h = hearing.reply(h["cases"][0]["id"], choice=choice)
        assert h["cases"][0]["status"] == status
        assert h["summary"]["saving_krw"] == 0
        assert h["summary"]["done"] == 1
    call.assert_not_called()


def test_explicit_target_and_rejection(db):
    seed()
    h = hearing.create(days=7)
    cid = h["cases"][0]["id"]
    h = hearing.reply(cid, choice="adjust", target_count=6)
    assert h["cases"][0]["proposal_krw"] == 60000
    h = hearing.reply(cid, choice="reject")
    assert h["cases"][0]["status"] == "kept"
    assert "추가 설명은 필요하지" in h["cases"][0]["turns"][-1]["text"]
    with pytest.raises(AppError):
        hearing.reply(cid, choice="adjust", target_count=3)


@pytest.mark.parametrize("target", [None, -1, 7, 1.5, True])
def test_invalid_targets(db, target):
    seed()
    h = hearing.create(days=7)
    with pytest.raises(AppError):
        hearing.reply(h["cases"][0]["id"], choice="adjust", target_count=target)


def test_ai_disabled_remains_disabled_during_reply(db, monkeypatch):
    seed()
    h = hearing.create(days=7, use_ai=False)
    monkeypatch.setattr(ai_adapter, "is_configured", lambda: True)
    call = Mock(side_effect=AssertionError("Session opted out"))
    monkeypatch.setattr(ai_adapter, "call_json", call)
    h = hearing.reply(h["cases"][0]["id"], text="야근 식사였어요")
    assert h["cases"][0]["status"] == "open"
    assert h["cases"][0]["proposal_count"] is None
    assert h["cases"][0]["label"] is None
    call.assert_not_called()


def test_ai_context_does_not_force_a_cut(db, monkeypatch):
    seed()
    monkeypatch.setattr(ai_adapter, "is_configured", lambda: True)
    call = Mock(return_value=hearing._Turn(reply="야근 식사로 확인했습니다. 유지할까요?", action="choose_action", label="야근 식사"))
    monkeypatch.setattr(ai_adapter, "call_json", call)
    h = hearing.create(days=7)
    call.assert_not_called()
    h = hearing.reply(h["cases"][0]["id"], text="야근 식사였어요")
    assert h["cases"][0]["status"] == "open"
    assert h["cases"][0]["label"] == "야근 식사"
    assert h["cases"][0]["turns"][-1]["source"] == "model"
    assert call.call_count == 1


def test_ai_invalid_numbers_fallback_and_no_automatic_decision(db, monkeypatch):
    seed()
    monkeypatch.setattr(ai_adapter, "is_configured", lambda: True)
    monkeypatch.setattr(ai_adapter, "call_json", lambda *a, **kw: hearing._Turn(reply="999999원은 유지합니다.", action="accept"))
    h = hearing.create(days=7)
    h = hearing.reply(h["cases"][0]["id"], text="식사")
    assert h["cases"][0]["status"] == "open"
    assert h["cases"][0]["turns"][-1]["source"] == "template"


def test_refunds_and_card_settlements(db):
    expense = add("2026-10-14", amount=10000)
    credit = add("2026-10-14", amount=3000, kind="refund", direction="in")
    add("2026-10-14", amount=50000, kind="card_settlement")
    with store.tx() as conn:
        conn.execute("INSERT INTO allocations(id,credit_id,expense_id,kind,amount_krw,created_at) VALUES (?,?,?,'refund',3000,?)",
                     (store.new_id("allocation"), credit, expense, store.now_iso()))
    h = hearing.create(days=7)
    assert h["summary"]["period_total_krw"] == 7000
    h = hearing.reply(h["cases"][0]["id"], choice="adjust", target_count=0)
    h = hearing.reply(h["cases"][0]["id"], choice="accept")
    assert h["summary"]["saving_krw"] == 7000


def test_amount_repeats_require_same_merchant(db):
    for n in range(4):
        add("2026-10-14", merchant=f"가게{n}")
    h = hearing.create(days=7)
    assert not any(c["kind"] == "repeat_amount" for c in h["cases"])


def test_month_budget_priority_save_and_stale_guard(db):
    seed()
    with store.tx() as conn:
        conn.execute("INSERT INTO pledges(month, category_budgets, updated_at) VALUES (?,?,?)",
                     ("2026-10", store.dumps({"food": 50000}), store.now_iso()))
    h = hearing.create(month="2026-10", use_ai=False)
    assert h["cases"][0]["kind"] == "budget"
    cid = h["cases"][0]["id"]
    h = hearing.reply(cid, choice="adjust", target_count=10)
    h = hearing.reply(cid, choice="accept")
    result = hearing.apply(h["id"])
    assert result["next_month"] == "2026-11"
    assert result["total_budget_krw"] == 75000
    with store.tx() as conn:
        store.bump_ledger_version(conn)
    assert hearing.get(h["id"])["stale"]
    with pytest.raises(AppError):
        hearing.apply(h["id"])


def test_stale_reply_is_rejected(db):
    seed()
    h = hearing.create(days=7)
    with store.tx() as conn:
        store.bump_ledger_version(conn)
    with pytest.raises(AppError) as e:
        hearing.reply(h["cases"][0]["id"], choice="keep")
    assert e.value.status == 409


def test_migration_idempotent_and_legacy_read(db):
    add("2026-10-14")
    with store.tx() as conn:
        conn.execute("INSERT INTO hearings(id,month,ledger_version,tone,created_at) VALUES ('legacy','2026-10',0,'calm',?)", (store.now_iso(),))
    store.init_db()
    store.init_db()
    h = hearing.get("legacy")
    assert h["mode"] == "month"
    assert h["summary"]["period_total_krw"] == 5000
    assert hearing.latest(month="2026-10") is None


def test_api_periods_and_choices(db):
    seed()
    with TestClient(app) as client:
        assert client.get("/hearing-periods").json()["last"] == "2026-10-14"
        response = client.post("/hearings", json={"days": 7, "use_ai": False})
        assert response.status_code == 200
        h = response.json()
        cid = h["cases"][0]["id"]
        assert client.post(f"/cases/{cid}/reply", json={"choice": "adjust", "target_count": 1.5}).status_code == 422
        result = client.post(f"/cases/{cid}/reply", json={"choice": "one_off"})
        assert result.status_code == 200
        assert result.json()["summary"]["one_off"] == 1
        assert client.post(f"/hearings/{h['id']}/apply").status_code == 400
        assert client.get("/hearings/latest?days=7").json()["hearing"]["id"] == h["id"]
        assert client.get("/hearings/latest?start_date=bad&end_date=bad").status_code == 400


def test_evidence_lists_actual_amounts_not_average(db):
    for day, amount in [(1, 10000), (2, 20000), (3, 30000)]:
        add(f"2026-10-{day:02d}", amount=amount)
    h = hearing.create(days=7, use_ai=False)
    c = h["cases"][0]
    rows = c["facts"]["transactions"]
    assert [r["amount_krw"] for r in rows] == [10000, 20000, 30000]
    assert [r["date"] for r in rows] == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert sum(r["net_krw"] for r in rows) == c["total_krw"] == 60000
    # 예전 세션에도 원본 거래 ID로 표를 복원한다.
    with store.tx() as conn:
        facts = dict(c["facts"])
        del facts["transactions"]
        conn.execute("UPDATE hearing_cases SET facts=? WHERE id=?", (store.dumps(facts), c["id"]))
    restored = hearing.get(h["id"])["cases"][0]["facts"]
    assert restored["transactions"] == rows
    assert restored["evidence_current"]


def test_actual_evidence_frequencies_are_allowed(db):
    seed()
    h = hearing.create(days=30, use_ai=False)
    c = h['cases'][0]
    assert 7 in hearing._allowed_numbers(c)
    assert 5000 in hearing._allowed_numbers(c)
    assert 10000 in hearing._allowed_numbers(c)
    assert 999999 not in hearing._allowed_numbers(c)


def test_adapter_requests_json_output(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from app import config
    create = Mock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"reply":"확인했습니다.","action":"choose_action"}'))],
        usage=None))
    monkeypatch.setattr(ai_adapter, 'is_configured', lambda: True)
    monkeypatch.setattr(ai_adapter, '_get_client', lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(config, 'llm_model', lambda: 'test-model')
    result = ai_adapter.call_json('JSON으로 응답', {}, hearing._Turn)
    assert result.action == 'choose_action'
    assert create.call_args.kwargs['response_format'] == {'type': 'json_object'}


def test_adjusted_ledger_persists_and_preserves_actual_transactions(db):
    seed()
    assert hearing.latest_adjusted() is None
    h = hearing.create(days=7, use_ai=False)
    cid = h['cases'][0]['id']
    hearing.reply(cid, choice='adjust', target_count=6)
    assert hearing.latest_adjusted() is None
    hearing.reply(cid, choice='accept')
    with TestClient(app) as client:
        saved = client.get('/plan/adjusted').json()['hearing']
        assert saved['id'] == h['id']
        assert saved['summary']['period_total_krw'] == 70000
        assert saved['summary']['next_plan_krw'] == 60000
        assert saved['summary']['saving_krw'] == 10000
        assert saved['cases'][0]['proposal_count'] == 6
        # 새 미확정 세션이 기존 확정 내역을 가리지 않는다.
        hearing.create(days=7, use_ai=False)
        assert client.get('/plan/adjusted').json()['hearing']['id'] == h['id']
    with store.read() as conn:
        actual = conn.execute("SELECT SUM(amount_krw) FROM transactions WHERE date >= '2026-10-08'").fetchone()[0]
        assert actual == 70000
    with store.tx() as conn:
        store.bump_ledger_version(conn)
    assert hearing.latest_adjusted()['stale']


def test_first_question_is_generated_by_ai(db, monkeypatch):
    seed()
    monkeypatch.setattr(ai_adapter, 'is_configured', lambda: True)
    call = Mock(return_value=hearing._Turn(reply='식비 지출이 늘었습니다. 일정에 변화가 있었나요?', action='ask_context'))
    monkeypatch.setattr(ai_adapter, 'call_json', call)
    h = hearing.create(days=7)
    call.assert_not_called()
    h = hearing.open_case(h['cases'][0]['id'])
    assert call.call_count == 1
    assert h['cases'][0]['turns'][0]['source'] == 'model'
    assert h['cases'][0]['status'] == 'open'
    payload = call.call_args.args[1]
    assert payload['event'] == 'start'
    assert payload['conversation'] == []
    assert payload['case']['facts']['transactions']


@pytest.mark.parametrize('response', ['failure', 'numbers', 'decision'])
def test_invalid_first_question_falls_back_without_deciding(db, monkeypatch, response):
    seed()
    monkeypatch.setattr(ai_adapter, 'is_configured', lambda: True)
    if response == 'failure':
        call = Mock(side_effect=ai_adapter.AIUnavailable('timeout'))
    elif response == 'numbers':
        call = Mock(return_value=hearing._Turn(reply='999999원입니다.', action='ask_context'))
    else:
        call = Mock(return_value=hearing._Turn(reply='유지로 확정합니다.', action='accept', label='추측한 품목'))
    monkeypatch.setattr(ai_adapter, 'call_json', call)
    h = hearing.create(days=7)
    h = hearing.open_case(h['cases'][0]['id'])
    c = h['cases'][0]
    assert c['status'] == 'open'
    assert c['label'] is None
    assert c['turns'][0]['source'] == 'template'
    assert c['turns'][0]['action'] == 'ask_context'


def test_more_than_three_cases_and_lazy_questions(db, monkeypatch):
    for n in range(7):
        add('2026-10-14', amount=1000 + n * 100, merchant=f'상점{n}')
    monkeypatch.setattr(ai_adapter, 'is_configured', lambda: True)
    call = Mock(return_value=hearing._Turn(reply='반복될 지출인가요?', action='ask_context'))
    monkeypatch.setattr(ai_adapter, 'call_json', call)
    h = hearing.create(days=7)
    assert len(h['cases']) == 7
    call.assert_not_called()
    assert all(not c['turns'] for c in h['cases'])
    ids = [tid for c in h['cases'] for tid in c['tx_ids']]
    assert len(ids) == len(set(ids))
    fourth_id = h['cases'][3]['id']
    with TestClient(app) as client:
        response = client.post(f'/cases/{fourth_id}/open')
        assert response.status_code == 200
        opened = response.json()
        assert opened['cases'][3]['turns'][0]['source'] == 'model'
        assert not opened['cases'][0]['turns']
        client.post(f'/cases/{fourth_id}/open')
    assert call.call_count == 1
    assert hearing.get(h['id'])['cases'][3]['turns']


def test_ai_proposes_cut_using_nearby_evidence(db, monkeypatch):
    for n in range(6):
        add(f'2026-10-{n+1:02d}', amount=4000, merchant='교내카페', category='cafe')
    add('2026-10-03', amount=3000, merchant='편의점', category='food')
    monkeypatch.setattr(ai_adapter, 'is_configured', lambda: True)
    call = Mock(return_value=hearing._Turn(reply='같은 날 편의점 결제도 있습니다. 음료 구매가 겹쳤다면 5회로 조정할까요?', action='suggest_cut', target_count=5, label='카페 음료'))
    monkeypatch.setattr(ai_adapter, 'call_json', call)
    h = hearing.create(days=7)
    c = next(c for c in h['cases'] if c['count'] == 6)
    h = hearing.reply(c['id'], text='커피와 스무디를 샀습니다. 적절한 소비인지 판단해주세요.')
    updated = next(x for x in h['cases'] if x['id'] == c['id'])
    assert updated['status'] == 'proposed'
    assert updated['proposal_count'] == 5
    assert updated['proposal_krw'] == 20000
    assert h['summary']['saving_krw'] == 0
    related = call.call_args.args[1]['case']['facts']['related_transactions']
    assert related[0]['merchant_name'] == '편의점'
    assert related[0]['gap_minutes'] == 0
    h = hearing.reply(c['id'], choice='accept')
    assert h['summary']['saving_krw'] == 4000


@pytest.mark.parametrize('target', [13, -1, None])
def test_ai_cannot_propose_invalid_count(db, monkeypatch, target):
    seed()
    monkeypatch.setattr(ai_adapter, 'is_configured', lambda: True)
    monkeypatch.setattr(ai_adapter, 'call_json', Mock(return_value=hearing._Turn(
        reply='횟수를 조정할까요?', action='suggest_cut', target_count=target)))
    h = hearing.create(days=7)
    h = hearing.reply(h['cases'][0]['id'], text='음료를 샀습니다.')
    assert h['cases'][0]['status'] == 'open'
    assert h['cases'][0]['proposal_count'] is None


def test_reconsidered_proposal_cannot_be_accepted(db, monkeypatch):
    seed()
    monkeypatch.setattr(ai_adapter, 'is_configured', lambda: True)
    call = Mock(return_value=hearing._Turn(reply='다른 용도였다면 필요성을 다시 확인하겠습니다.', action='ask_context'))
    monkeypatch.setattr(ai_adapter, 'call_json', call)
    h = hearing.create(days=7)
    cid = h['cases'][0]['id']
    hearing.reply(cid, choice='adjust', target_count=5)
    h = hearing.reply(cid, text='편의점은 식사였고 카페는 공부하려고 갔습니다.')
    c = h['cases'][0]
    assert c['status'] == 'open'
    assert c['proposal_count'] is None
    assert c['proposal_krw'] is None
    assert h['summary']['saving_krw'] == 0
    with pytest.raises(AppError, match='수락할 제안'):
        hearing.reply(cid, choice='accept')


def test_explicit_choice_skips_related_ledger_scan(db, monkeypatch):
    seed()
    h = hearing.create(days=7, use_ai=False)
    monkeypatch.setattr(hearing, '_expenses', Mock(side_effect=AssertionError('Unnecessary ledger scan')))
    h = hearing.reply(h['cases'][0]['id'], choice='keep')
    assert h['cases'][0]['status'] == 'kept'


@pytest.mark.parametrize('target', [True, '5', 1.5])
def test_ai_target_requires_strict_integer(target):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        hearing._Turn(reply='조정할까요?', action='suggest_cut', target_count=target)
