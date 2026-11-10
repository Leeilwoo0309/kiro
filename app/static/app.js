"use strict";
// 사용자 문자열은 모두 textContent로 넣는다. innerHTML을 쓰지 않는다.

const CATS = [
  ["food", "식비"], ["transport", "교통"], ["shopping", "쇼핑"], ["cafe", "카페"],
  ["medical", "의료"], ["subscription", "구독/OTT"], ["other", "기타"],
];
const CAT_LABEL = Object.fromEntries(CATS);
const KIND_ICON = { budget: "clipboard-list", increase: "trending-up", repeat_amount: "repeat", repeat_merchant: "building-store", late_night: "moon", burst: "bolt", spike: "receipt" };
const STATUS = { open: ["질의 중", ""], proposed: ["제안 대기", "warn"], agreed: ["줄이기 합의", "ok"], kept: ["유지", ""], one_off: ["일회성", ""] };

const state = { settings: null, dashboard: null, preview: null, hearing: null, activeCase: null, busy: false };

// ───────── 공통 ─────────
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
function icon(name) {
  return el("img", { class: "icon", src: `/static/icons/${name}.svg`, alt: "", "aria-hidden": "true", width: 20, height: 20 });
}
const $ = (id) => document.getElementById(id);
const won = (n) => (n === null || n === undefined ? "-" : `${Number(n).toLocaleString("ko-KR")}원`);
const present = () => !!(state.settings && state.settings.presentation_mode);
function name(n) {
  // 발표 모드: 거래처명을 첫 글자만 남기고 가린다
  if (!present() || !n) return n || "";
  return n.slice(0, 1) + "○".repeat(Math.max(1, Math.min(n.length - 1, 6)));
}
function maskText(text, names) {
  if (!present()) return text;
  let out = text;
  for (const n of names || []) if (n && n.length > 1) out = out.split(n).join(name(n));
  return out;
}
function notify(text, kind = "info") {
  $("notice").replaceChildren(text ? el("div", { class: `msg ${kind}`, text }) : "");
}

async function api(method, path, body, isForm) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    if (isForm) opts.body = body;
    else { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (_) { /* 본문 없음 */ }
  if (!res.ok) {
    const msg = (data && data.error && data.error.message) || (data && data.detail) || `요청 실패 (${res.status})`;
    const err = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    err.status = res.status; err.data = data;
    throw err;
  }
  return data;
}

async function guarded(fn) {
  if (state.busy) return;
  state.busy = true;
  const locked = [...document.querySelectorAll("main button, main input, main select, main textarea")]
    .filter(control => !control.disabled);
  locked.forEach(control => { control.disabled = true; });
  document.body.setAttribute("aria-busy", "true");
  try { await fn(); } catch (e) { notify(e.message, "error"); }
  finally {
    locked.forEach(control => { control.disabled = false; });
    state.busy = false;
    document.body.removeAttribute("aria-busy");
    if (state.pendingTab) {
      const next = state.pendingTab;
      state.pendingTab = null;
      showTab(next);
    }
  }
}

function showTab(tab) {
  if (state.busy) { state.pendingTab = tab; return; }
  document.querySelectorAll(".tabs button").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === tab);
    if (b.dataset.tab === tab) $("page-title").textContent = b.querySelector("span:last-child").textContent;
  });
  document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("active", s.id === `tab-${tab}`));
  const render = { dashboard: renderDashboard, hearing: renderHearing, import: renderImport, settings: renderSettings }[tab];
  if (render) guarded(render);
}

// ───────── 설정 ─────────
async function loadSettings() {
  state.settings = await api("GET", "/settings");
  $("toggle-hearing").checked = state.settings.tone === "hearing";
  $("toggle-present").checked = !!state.settings.presentation_mode;
  const ai = state.settings.ai;
  const badge = $("ai-status");
  badge.textContent = ai.configured ? "연결 설정" : "연결 설정 · 미연결";
  badge.className = `badge ${ai.configured ? "on" : "off"}`;
}

async function renderSettings() {
  await loadSettings();
  $("tab-settings").replaceChildren(
    aiCard(state.settings.ai),
    el("div", { class: "card" },
      el("h2", { text: "로컬 데이터 전체 삭제" }),
      el("p", { text: "가져온 거래와 청문회 기록, 다음 달 계획을 모두 지웁니다. 되돌릴 수 없습니다. AI 키는 남습니다." }),
      el("button", { class: "btn danger", onclick: () => guarded(async () => {
        const typed = window.prompt("되돌릴 수 없습니다. 계속하려면 '삭제'를 입력하세요.");
        if (typed !== "삭제") { notify("삭제를 취소했습니다."); return; }
        await api("DELETE", "/local-data", { confirm: "삭제" });
        state.hearing = null; state.dashboard = null;
        notify("로컬 데이터를 모두 삭제했습니다.", "ok");
      }) }, "전체 삭제")));
}

// ───────── 대시보드 ─────────
async function renderDashboard() {
  const [d, nextPlan, adjusted] = await Promise.all([api("GET", "/dashboard"), api("GET", "/plan/next"), api("GET", "/plan/adjusted")]);
  state.adjustedLedger = adjusted.hearing;
  state.dashboard = d;
  const plan = nextPlan.plan;
  const box = $("tab-dashboard");
  if (!d.month) {
    box.replaceChildren(el("div", { class: "card hero" }, el("h2", { text: "거래 내역을 가져오세요" }),
      el("p", { text: "은행·카드 PDF나 CSV를 올리면 소비 근거를 확인하고 유지할 지출과 조정할 지출을 정리합니다." }),
      el("button", { class: "btn primary", onclick: () => showTab("import") }, icon("upload"), "파일 가져오기")));
    return;
  }
  const monthSel = el("select", { "aria-label": "조회 월" }, d.months_available.map((m) =>
    el("option", { value: m, selected: m === d.month }, m)));
  monthSel.addEventListener("change", () => guarded(async () => {
    state.dashboard = await api("GET", `/dashboard?month=${monthSel.value}`);
    drawDashboard(box, state.dashboard, monthSel, plan);
  }));
  drawDashboard(box, d, monthSel, plan);
}

function drawDashboard(box, d, monthSel, plan) {
  const top = d.ranking[0];
  box.replaceChildren(
    el("div", { class: "row" }, el("strong", { text: "조회 월" }), monthSel,
      el("span", { class: "muted", text: d.period ? `${d.period.first} ~ ${d.period.last} · 거래 ${d.transaction_count}건` : "" })),
    el("div", { class: "card hero" },
      el("h2", { text: "내 소비, 근거부터 확인" }),
      el("p", { text: "최근 7일·30일 또는 원하는 기간을 선택하세요. 중요한 안건부터 3개씩 확인하고 유지·조정·일회성으로 결정합니다." }),
      el("button", { class: "btn primary", onclick: () => { showTab("hearing"); } }, icon("scale"), "청문회 열기")),
    el("div", { class: "grid" },
      el("div", { class: "card stat" }, el("div", { class: "label", text: "선택 월 본인 부담 소비" }),
        el("div", { class: "value", text: won(d.total_net_krw) }),
        el("div", { class: "muted", text: `카드대금·본인 이체·가승인은 빼고 계산` })),
      el("div", { class: "card stat" }, el("div", { class: "label", text: "가장 많이 쓴 항목" }),
        el("div", { class: "value", text: top ? top.label : "-" }),
        el("div", { class: "muted", text: top ? `${won(top.total_krw)} · ${top.percent}%` : "" })),
      planStat(plan, state.adjustedLedger)),
    adjustmentLedger(state.adjustedLedger) || "",
    el("div", { class: "card" }, el("h2", { text: "카테고리별 지출" }),
      el("div", { class: "bars" }, d.ranking.map((r) => el("div", { class: "bar-row" },
        el("span", { class: "bar-label", text: r.label }),
        el("div", { class: "bar" }, el("span", { style: `width:${Math.max(2, r.percent)}%` })),
        el("span", { class: "bar-value", text: `${won(r.total_krw)} · ${r.count}건` }))))),
  );
}

// ───────── 청문회 ─────────
async function renderHearing() {
  const box = $("tab-hearing");
  const bounds = await api("GET", "/hearing-periods");
  if (!bounds.last) {
    box.replaceChildren(el("div", { class: "card" }, el("p", { text: "먼저 거래 파일을 가져와 주세요." }),
      el("button", { class: "btn primary", onclick: () => showTab("import") }, "파일 가져오기")));
    return;
  }
  const selected = state.hearingFilter || { mode: "30", end: bounds.last };
  const mode = el("select", { "aria-label": "분석 기간" },
    [["7", "최근 7일"], ["30", "최근 30일"], ["custom", "직접 선택"], ["month", "월별 예산 점검"]].map(([v, label]) =>
      el("option", { value: v, selected: selected.mode === v }, label)));
  const startDate = el("input", { type: "date", "aria-label": "시작일", value: selected.start || bounds.first });
  const endDate = el("input", { type: "date", "aria-label": "종료일", value: selected.end || bounds.last });
  const month = el("select", { "aria-label": "예산 조회 월" }, bounds.months.map(m =>
    el("option", { value: m, selected: m === (selected.month || bounds.last.slice(0, 7)) }, m)));
  const custom = el("div", { class: "row" }, startDate, "~", endDate);
  const caption = el("p", { class: "muted" });
  const body = el("div");
  const params = () => {
    if (mode.value === "month") return { month: month.value };
    if (mode.value === "custom") return { start_date: startDate.value, end_date: endDate.value };
    return { days: Number(mode.value) };
  };
  const updateControls = () => {
    custom.hidden = mode.value !== "custom";
    month.hidden = mode.value !== "month";
    caption.textContent = ["7", "30"].includes(mode.value)
      ? `마지막 거래일 ${bounds.last} 기준 최근 ${mode.value}일 · 업로드 기록 ${bounds.first} ~ ${bounds.last}`
      : "선택한 기간의 업로드 기록만 분석합니다. 월별 예산 점검에서 월 예산을 저장할 수 있습니다.";
    state.hearingFilter = { mode: mode.value, start: startDate.value, end: endDate.value, month: month.value };
  };
  const loadLatest = async () => {
    updateControls();
    state.visibleCases = 3;
    state.activeCase = null;
    state.hearing = null;
    drawHearing(body);
    state.hearing = (await api("GET", `/hearings/latest?${new URLSearchParams(params())}`)).hearing;
    if (state.hearing?.cases.length) await openCase(state.hearing.cases[0].id, body);
    else drawHearing(body);
  };
  for (const input of [mode, startDate, endDate, month]) input.addEventListener("change", () => guarded(loadLatest));
  const start = () => guarded(async () => {
    updateControls();
    notify(state.settings.ai.configured ? "거래 근거를 바탕으로 첫 질문을 작성하고 있습니다…" : "소비 근거를 확인하고 있습니다…");
    state.hearing = await api("POST", "/hearings", params());
    state.visibleCases = 3;
    state.activeCase = state.hearing.cases[0].id;
    notify(`확인할 안건 ${state.hearing.cases.length}건입니다. 유지·조정·일회성 중 선택해 주세요.`, "ok");
    await openCase(state.activeCase, body);
  });
  box.replaceChildren(el("div", { class: "row" }, el("strong", { text: "분석 기간" }), mode, custom, month,
    el("button", { class: "btn primary", onclick: start }, "청문회 시작하기")), caption, body);
  await loadLatest();
}

async function openCase(caseId, box) {
  state.activeCase = caseId;
  drawHearing(box);
  const c = state.hearing.cases.find(item => item.id === caseId);
  if (c && !c.turns.length && !state.hearing.stale) {
    try {
      state.hearing = await api("POST", `/cases/${caseId}/open`);
    } finally {
      drawHearing(box);
    }
  }
}

function drawHearing(box) {
  const h = state.hearing;
  if (!h || !h.cases.length) {
    box.replaceChildren(el("div", { class: "card hero" }, el("h2", { text: "아직 청문회를 열지 않았습니다" }),
      el("p", { text: "‘새 청문회 시작’을 누르면 예산·기간별 변화·반복 지출에서 중요한 안건을 골라 보여줍니다. 줄이지 않고 유지해도 됩니다." })));
    return;
  }
  if (!h.cases.some((c) => c.id === state.activeCase)) {
    const next = h.cases.find((c) => ["open", "proposed"].includes(c.status)) || h.cases[0];
    state.activeCase = next.id;
  }
  const s = h.summary;
  const summary = el("div", { class: "card summary-bar" },
    el("div", {}, el("div", { class: "muted", text: `결정 ${s.done}/${s.cases}` }),
      el("div", { class: "progress" }, el("span", { style: `width:${s.cases ? Math.round(s.done / s.cases * 100) : 0}%` }))),
    el("div", {}, el("div", { class: "muted", text: "선택 기간 소비" }), el("strong", { text: won(s.period_total_krw) })),
    el("div", {}, el("div", { class: "muted", text: "같은 길이의 기간 기준 예상 절감액" }),
      el("strong", { class: "good", text: won(s.saving_krw) })),
    el("div", {}, el("div", { class: "muted", text: "결정 결과" }),
      el("strong", { text: `유지 ${s.kept} · 조정 ${s.agreed} · 일회성 ${s.one_off}` })),
    h.mode === "month" ? el("button", { class: "btn primary", disabled: !h.can_apply_month || s.agreed === 0 || s.done !== s.cases,
      onclick: () => guarded(async () => {
        const r = await api("POST", `/hearings/${h.id}/apply`);
        notify(`${r.next_month} 예산 ${won(r.total_budget_krw)}을 저장했습니다.`, "ok");
        showTab("dashboard");
      }) }, "다음 달 예산으로 저장") : "");
  const comparison = h.comparison;
  const context = el("div", { class: "card" },
    el("strong", { text: h.start_date ? `${h.start_date} ~ ${h.end_date}` : h.month }),
    el("p", { class: "muted", text: h.notice || "월 단위 소비 점검" }),
    comparison ? el("p", { text: comparison.available
      ? `이전 동일 기간 ${comparison.start_date} ~ ${comparison.end_date}: ${won(comparison.total_krw)} · 선택 기간: ${won(s.period_total_krw)}`
      : comparison.notice }) : "",
    comparison && comparison.available ? el("p", { class: "muted", text: comparison.notice }) : "",
    el("p", { class: "muted", text: "예상 절감액은 과거 평균 단가 기준입니다. 일회성 지출은 절약 성과에 포함하지 않습니다." }),
    h.stale ? el("p", { class: "msg warn", text: "거래가 변경됐습니다. 새 청문회를 시작해 주세요." }) : "");

  const visible = Math.max(3, state.visibleCases || 3);
  const list = el("div", { class: "case-list" }, h.cases.slice(0, visible).map((c) => {
    const [label, tone] = STATUS[c.status] || [c.status, ""];
    return el("button", { class: `case-item ${c.id === state.activeCase ? "active" : ""}`, onclick: () => guarded(() => openCase(c.id, box)) },
      el("span", { class: "case-ico" }, icon(KIND_ICON[c.kind] || "receipt")),
      el("span", { class: "case-main" }, el("strong", { text: maskText(c.title, c.facts.merchants) }),
        el("span", { class: "muted", text: `${c.category_label} · ${won(c.total_krw)}${c.label ? ` · ${c.label}` : ""}` })),
      el("span", { class: `tag ${tone}`, text: label }));
  }));

  if (h.cases.length > visible) list.append(el("button", { class: "btn", onclick: () => guarded(async () => {
    state.visibleCases = visible + 3;
    await openCase(h.cases[visible].id, box);
  }) }, `다음 안건 보기 (${h.cases.length - visible}건 남음)`));
  const c = h.cases.find((x) => x.id === state.activeCase);
  box.replaceChildren(context, summary, adjustmentLedger(h) || "", el("div", { class: "hearing-layout" }, list, chatPanel(c, box, h.stale)));
  const log = box.querySelector(".chat-log");
  if (log) log.scrollTop = log.scrollHeight;
}

function planStat(plan, adjusted) {
  // 월 예산을 저장했으면 그것을, 아니면 청문회에서 확정한 조정안을 보여준다.
  const a = adjusted && adjusted.summary.agreed ? adjusted.summary : null;
  const value = plan ? won(plan.total_budget_krw) : a ? won(a.next_plan_krw) : "아직 없음";
  const note = plan ? `${plan.month} 예산 · 청문회 합의 반영`
    : a ? `청문회 조정안 · ${won(a.saving_krw)} 절감 예정` : "청문회에서 목표를 확정하면 표시됩니다";
  return el("div", { class: "card stat" }, el("div", { class: "label", text: "다음 소비 계획" }),
    el("div", { class: "value", text: value }), el("div", { class: "muted", text: note }));
}

function adjustmentLedger(h) {
  if (!h || !h.summary.agreed) return null;
  const s = h.summary;
  const adjusted = h.cases.filter(c => c.status === "agreed");
  const header = ["조정 항목", "기존 횟수", "목표 횟수", "기존 지출", "조정 후 예상", "예상 절감"];
  return el("section", { class: "card adjustment-ledger", "aria-label": "조정 가계부" },
    el("h2", { text: "조정 가계부" }),
    el("p", { class: "muted", text: `${h.start_date || h.month} ~ ${h.end_date || h.month} 기록 기준 · 확정한 목표가 자동 반영됩니다.` }),
    el("div", { class: "grid" },
      el("div", {}, el("div", { class: "muted", text: "기존 기간 지출" }), el("strong", { text: won(s.period_total_krw) })),
      el("div", {}, el("div", { class: "muted", text: "목표 적용 후 예상 지출" }), el("strong", { text: won(s.next_plan_krw) })),
      el("div", {}, el("div", { class: "muted", text: "예상 절감액" }), el("strong", { text: won(s.saving_krw) }))),
    el("div", { class: "table-wrap", tabindex: 0, role: "region", "aria-label": "소비 조정 전후 비교" },
      el("table", {}, el("caption", { class: "muted", text: "확정한 소비 조정 내역" }),
        el("thead", {}, el("tr", {}, header.map((text, i) => el("th", { scope: "col", class: i ? "num" : "", text })))),
        el("tbody", {}, adjusted.map(c => el("tr", {},
          el("th", { scope: "row", text: maskText(c.label || c.title, c.facts.merchants) }),
          ...[`${c.count}회`, `${c.proposal_count}회`, won(c.total_krw), won(c.target_krw), won(c.total_krw - c.target_krw)].map(text =>
            el("td", { class: "num", text }))))))),
    el("p", { class: "muted", text: "실제 결제 내역은 그대로 보존됩니다. 예상 지출은 같은 길이의 기간에 목표를 적용한 금액이며, 조정하지 않은 소비는 기존 금액으로 포함합니다." }),
    s.done < s.cases ? el("p", { class: "muted", text: `미결정 안건 ${s.cases - s.done}건은 아직 반영되지 않았습니다.` }) : "",
    h.stale ? el("p", { class: "msg warn", text: "원장이 변경되어 이전 기록 기준의 조정안입니다. 새 청문회에서 다시 확인하세요." }) : "",
    el("button", { class: "btn", onclick: () => showTab("dashboard") }, "가계부에서 보기"));
}

function chatPanel(c, box, stale) {
  const names = c.facts.merchants;
  const log = el("div", { class: "chat-log", role: "log", "aria-label": "소비 대화", "aria-live": "polite" }, c.turns.map((t) => el("div", { class: `bubble ${t.role}` },
    t.role === "ai" ? el("span", { class: "who", text: "청문위원" }) : "",
    el("span", { text: maskText(t.text, names) }),
    t.proposal ? el("span", { class: "proposal", text: `${t.source === "template" ? "직접 정한 목표" : "절충안"}: ${t.proposal.count}회 · 예상 ${won(t.proposal.krw)}` }) : "")));
  const transactions = c.facts.transactions || [];
  const evidence = el("details", { class: "evidence", open: true },
    el("summary", { text: `근거 거래 ${c.count}건 · 평균 ${won(c.avg_krw)}` }),
    c.facts.evidence_current ? el("p", { class: "muted", text: "기존 청문회의 거래 내역입니다. 환불·정산은 현재 원장 기준입니다." }) : "",
    transactions.length ? el("div", { class: "table-wrap evidence-table", tabindex: 0, role: "region", "aria-label": "안건별 거래 내역" },
      el("table", {},
        el("caption", { text: "거래별 결제 내역 · 금액 단위: 원" }),
        el("thead", {}, el("tr", {}, ["날짜 / 시각", "거래처", "결제액", "환불·정산", "본인 부담"].map((label, i) =>
          el("th", { scope: "col", class: i >= 2 ? "num" : "", text: label })))),
        el("tbody", {}, transactions.map(t => el("tr", {},
          el("td", { text: `${t.date}${t.time ? ` ${t.time}` : ""}` }),
          el("td", { text: name(t.merchant_name) }),
          el("td", { class: "num", text: won(t.amount_krw) }),
          el("td", { class: "num", text: won(t.adjusted_krw) }),
          el("td", { class: "num", text: won(t.net_krw) })))),
        el("tfoot", {}, el("tr", {}, el("th", { scope: "row", colspan: 2, text: "합계" }),
          ...["amount_krw", "adjusted_krw", "net_krw"].map(key =>
            el("td", { class: "num", text: won(transactions.reduce((sum, t) => sum + t[key], 0)) })))))) :
      el("p", { class: "muted", text: "거래 상세를 찾을 수 없습니다. 새 청문회를 시작해 주세요." }));
  const done = ["agreed", "kept", "one_off"].includes(c.status);
  const input = el("textarea", { rows: 3, placeholder: "이 지출에 대한 의견을 남겨주세요", "aria-label": "소비 맥락", maxlength: 300 });
  const target = el("input", { type: "number", min: 0, max: c.count - 1, step: 1,
    placeholder: `0~${c.count - 1}회`, "aria-label": "직접 정할 목표 횟수", value: c.proposal_count ?? undefined });
  let failedBubble = null;
  const send = choice => guarded(async () => {
    const text = input.value.trim();
    if (!choice && !text) { notify("답을 입력해 주세요.", "error"); return; }
    const targetCount = target.value === "" ? null : Number(target.value);
    if (choice === "adjust" && (!Number.isInteger(targetCount) || targetCount < 0 || targetCount >= c.count)) {
      notify(`목표 횟수를 0~${c.count - 1}회로 입력해 주세요.`, "error"); return;
    }
    notify("");
    failedBubble?.remove();
    failedBubble = null;
    const labels = { keep: "유지", one_off: "안건 전체가 일회성", accept: "목표 확정", adjust: `목표 ${targetCount}회` };
    const bubble = el("div", { class: "bubble user" },
      el("span", { text: maskText(choice ? `${labels[choice]}${text ? ` · ${text}` : ""}` : text, names) }));
    const waiting = el("div", { class: "reply-wait", role: "status", text: "답변을 준비하고 있습니다…" });
    log.append(bubble, waiting);
    input.value = "";
    const controls = [...actions.querySelectorAll("button, input, textarea")];
    controls.forEach(control => { control.disabled = true; });
    log.scrollTop = log.scrollHeight;
    try {
      state.hearing = await api("POST", `/cases/${c.id}/reply`, {
        text: text || undefined, choice, target_count: choice === "adjust" ? targetCount : undefined });
      drawHearing(box);
    } catch (error) {
      // 응답만 유실된 경우 이미 저장한 메시지를 다시 전송하지 않도록 확인한다.
      try {
        const latest = await api("GET", `/hearings/${state.hearing.id}`);
        const updated = latest.cases.find(item => item.id === c.id);
        if (updated && updated.turns.length > c.turns.length) {
          state.hearing = latest;
          drawHearing(box);
          return;
        }
      } catch (_) { /* 확인할 수 없으면 입력을 보존한다. */ }
      waiting.remove();
      bubble.append(el("span", { class: "send-error", text: "전송 확인 실패 · 입력 내용을 복원했습니다" }));
      failedBubble = bubble;
      input.value = text;
      notify(error.message, "error");
    } finally {
      controls.forEach(control => { control.disabled = false; });
      if (input.isConnected) input.focus();
    }
  });
  input.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  const actions = !c.turns.length && !stale ? el("div", { role: "status" },
    el("p", { class: "muted", text: "거래 근거를 바탕으로 첫 질문을 준비합니다." }),
    el("button", { class: "btn", onclick: () => guarded(() => openCase(c.id, box)) }, "질문 불러오기")) : done ? el("div", { class: `result ${c.status}` },
    c.status === "agreed" ? `조정: ${c.proposal_count}회 · 예상 ${won(c.target_krw)}` :
      c.status === "one_off" ? "일회성으로 기록 · 예상 절감액에서 제외" : `유지: ${c.reason || "유지할 소비"}`) :
    stale ? el("p", { text: "새 청문회에서 결정을 이어가 주세요." }) :
    el("div", { class: "composer" },
      el("div", { class: "row" },
        el("button", { class: "btn", onclick: () => send("keep") }, "유지"),
        el("button", { class: "btn", onclick: () => send("one_off") }, "안건 전체가 일회성"),
        c.status === "proposed" ? el("button", { class: "btn primary", onclick: () => send("accept") }, "목표 확정") : ""),
      el("div", { class: "row target-row" }, el("label", {}, "다음 기간 목표 횟수 ", target),
        el("button", { class: "btn", onclick: () => send("adjust") }, "조정안 확인")),
      el("div", { class: "message-composer" }, input, el("button", { class: "btn primary", onclick: () => send() }, "보내기")),
      el("p", { class: "muted composer-hint", text: "Enter 전송 · Shift+Enter 줄바꿈" }));
  return el("div", { class: "card chat" },
    el("div", { class: "chat-head" }, el("span", { class: "case-ico big" }, icon(KIND_ICON[c.kind] || "receipt")),
      el("div", {}, el("h2", { text: maskText(c.title, names) }), el("span", { class: "muted", text: `${c.category_label} · ${c.count}건 · ${won(c.total_krw)}` }))),
    evidence, log, actions);
}

function aiCard(ai) {
  const SOURCE = { web: "웹에서 입력한 키", env: ".env 파일의 키", none: "키 없음" };
  const key = el("input", { type: "password", id: "ai-key", autocomplete: "off", spellcheck: "false",
    placeholder: ai.has_key ? `저장된 키 ${ai.key_hint || ""} · 바꾸려면 새 키 입력` : "BAZE API 키 붙여넣기", style: "flex:1;min-width:240px" });
  const models = el("div", { class: "model-list" }, el("p", { class: "muted", text: "키를 저장하면 모델 목록이 나옵니다." }));
  let picked = ai.model;

  const loadModels = async () => {
    try {
      const res = await api("GET", "/ai/models");
      picked = res.current;
      const opts = res.models.map((m) => {
        const radio = el("input", { type: "radio", name: "model", value: m.id, checked: m.id === picked });
        const box = el("label", { class: `model-opt ${m.id === picked ? "selected" : ""}` }, radio,
          el("div", {}, el("div", { class: "id", text: m.id }),
            el("div", { class: "muted", text: m.input_rate !== null ? `입력 ${m.input_rate} · 출력 ${m.output_rate} 크레딧/1K` : `${m.provider || ""} · 단가 미확인` })));
        radio.addEventListener("change", () => {
          picked = m.id;
          models.querySelectorAll(".model-opt").forEach((x) => x.classList.remove("selected"));
          box.classList.add("selected");
        });
        return box;
      });
      models.replaceChildren(...opts);
    } catch (e) {
      models.replaceChildren(el("p", { class: "muted", text: e.message }));
    }
  };
  if (ai.has_key) loadModels();

  return el("div", { class: "card" },
    el("h2", { text: "분석 서비스 연결" }),
    el("p", {}, el("span", { class: `tag ${ai.configured ? "ok" : "warn"}`, text: ai.configured ? "연결됨" : "꺼짐" }),
      " ", el("span", { class: "muted", text: `${SOURCE[ai.key_source] || ""}${ai.key_hint ? ` ${ai.key_hint}` : ""} · 모델 ${ai.model || "미선택"}` })),
    el("p", { class: "muted", text: "충남대 aiportal → BAZE → API Gateway에서 만든 키를 넣으세요. 키는 이 컴퓨터의 data/ai_settings.json에만 저장되고 화면·로그에 다시 표시되지 않습니다." }),
    el("div", { class: "row" }, key,
      el("button", { class: "btn primary", onclick: () => guarded(async () => {
        if (!key.value.trim()) { notify("키를 입력해 주세요.", "error"); return; }
        notify("키를 확인하는 중입니다...");
        await api("PUT", "/ai/config", { api_key: key.value.trim() });
        key.value = "";
        await loadSettings(); notify("키를 확인하고 저장했습니다.", "ok"); await renderSettings();
      }) }, "키 확인 후 저장"),
      ai.key_source === "web" ? el("button", { class: "btn", onclick: () => guarded(async () => {
        if (!window.confirm("저장된 키를 지울까요?")) return;
        await api("PUT", "/ai/config", { clear_key: true });
        await loadSettings(); notify("저장된 키를 지웠습니다."); await renderSettings();
      }) }, "키 삭제") : ""),
    el("h3", { text: "모델 선택" }),
    models,
    el("div", { class: "row" },
      el("button", { class: "btn primary", disabled: !ai.has_key, onclick: () => guarded(async () => {
        if (!picked) { notify("모델을 골라 주세요.", "error"); return; }
        await api("PUT", "/ai/config", { model: picked });
        await loadSettings(); notify(`모델을 ${picked}(으)로 바꿨습니다.`, "ok"); await renderSettings();
      }) }, "이 모델로 저장"),
      el("span", { class: "muted", text: "단가는 2026-10-27 기준 BAZE 문서 값입니다." })));
}

// ───────── 가져오기 (R3) ─────────
async function renderImport() {
  const box = $("tab-import");
  const file = el("input", { type: "file", accept: ".csv,.pdf", id: "file" });
  const pw = el("input", { type: "password", id: "pdf-pw", autocomplete: "off", placeholder: "암호 PDF만" });
  const useAi = el("input", { type: "checkbox", id: "use-ai", checked: !!(state.settings && state.settings.ai.configured) });
  const result = el("div");
  box.replaceChildren(
    el("div", { class: "card" },
      el("h2", { text: "거래 파일 가져오기" }),
      el("p", { class: "muted", text: "CSV(UTF-8, 열: date, amount, merchant_name, payment_type, direction) 또는 텍스트 PDF. 원본 파일은 저장하지 않습니다." }),
      el("p", { class: "tag warn", text: "규칙에 맞지 않는 거래처명은 분류를 위해 BAZE(Mindlogic) 서버로 전송됩니다." }),
      el("div", { class: "row" },
        el("label", { for: "file" }, "파일 "), file,
        el("label", { for: "pdf-pw" }, "PDF 비밀번호 "), pw,
        el("label", {}, useAi, " 미분류 거래 자동 분류"),
        el("button", { class: "btn primary", onclick: () => guarded(async () => {
          if (!file.files[0]) { notify("파일을 골라 주세요.", "error"); return; }
          const fd = new FormData(); fd.append("file", file.files[0]);
          if (pw.value) fd.append("password", pw.value);
          state.preview = await api("POST", "/imports/preview", fd, true);
          pw.value = "";
          renderPreview(result, useAi);
        }) }, "미리보기"))),
    result,
  );
}

function renderPreview(box, useAi) {
  const pv = state.preview;
  const checks = new Map();
  const rows = pv.rows.map((r) => {
    const cb = el("input", { type: "checkbox", checked: !r.default_excluded, "aria-label": `${r.idx + 1}행 가져오기` });
    checks.set(r.idx, cb);
    return el("tr", {},
      el("td", {}, cb),
      el("td", { text: r.date }),
      el("td", { text: name(r.merchant_name) }),
      el("td", { class: "num", text: won(r.amount_krw) }),
      el("td", { text: r.direction === "in" ? "입금" : "출금" }),
      el("td", { class: "hide-sm", text: r.payment_type }),
      el("td", {}, r.duplicate_reason ? el("span", { class: "tag warn", text: r.duplicate_reason }) : ""));
  });
  box.replaceChildren(el("div", { class: "card" },
    el("h2", { text: "미리보기" }),
    el("p", {}, `유효 ${pv.valid_count}건 · 건너뜀 ${pv.skipped_count}건 · 중복 후보 ${pv.duplicate_count}건`),
    pv.account_holder_detected ? el("p", { class: "muted", text: "명세서의 예금주 이름을 본인 이체 판별에 씁니다 (이 컴퓨터의 설정에만 저장)." }) : "",
    pv.errors.length ? el("details", {}, el("summary", { text: "건너뛴 행과 사유" }),
      el("ul", {}, pv.errors.map((e) => el("li", { text: `${e.row_no ? e.row_no + "행: " : ""}${e.reason}` })))) : "",
    el("table", {}, el("thead", {}, el("tr", {}, ["포함", "날짜", "거래처", "금액", "방향", "결제", "확인"].map((h, i) =>
      el("th", { class: i === 3 ? "num" : i === 5 ? "hide-sm" : "", text: h })))), el("tbody", {}, rows)),
    el("div", { class: "row" },
      el("button", { class: "btn primary", onclick: () => guarded(async () => {
        const exclude = [...checks.entries()].filter(([, cb]) => !cb.checked).map(([i]) => i);
        notify("저장하고 분류하는 중입니다...");
        const res = await api("POST", `/imports/${pv.preview_id}/commit`, { exclude_idx: exclude, use_ai: useAi.checked });
        const c = res.counts;
        notify(`${c.inserted}건을 가져왔습니다. 규칙 분류 ${c.rule}건, AI 분류 ${c.model}건, 미분류(기타) ${c.other_pending}건.`, "ok");
        state.preview = null;
        showTab("hearing");
      }) }, "확인한 행 가져오기"))));
}


// ───────── 시작 ─────────
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
$("ai-status").addEventListener("click", () => showTab("settings"));
$("toggle-hearing").addEventListener("change", (e) => guarded(async () => {
  await api("PATCH", "/settings", { tone: e.target.checked ? "hearing" : "calm" });
  await loadSettings();
  notify(e.target.checked ? "국감 모드: 새 청문회부터 단호한 말투로 묻습니다." : "차분한 말투로 묻습니다.");
}));
$("toggle-present").addEventListener("change", (e) => guarded(async () => {
  await api("PATCH", "/settings", { presentation_mode: e.target.checked });
  await loadSettings();
  const active = document.querySelector(".tabs button.active").dataset.tab;
  showTab(active);
}));
(async () => {
  try { await loadSettings(); } catch (e) { notify(e.message, "error"); }
  const initial = new URLSearchParams(location.search).get("tab");
  showTab(["dashboard", "hearing", "import", "settings"].includes(initial) ? initial : "dashboard");
})();
