// Интерфейс агента: чат с лентой шагов (NDJSON-поток), служба качества, документы и граф, аналитика.
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
let dialogId = null, busy = false, docsCache = [];

const EXAMPLES = [
  "Кто назначает руководителя проекта?",
  "Кто создаёт карточку проекта в CRM в Дирекции ИБ?",
  "Когда привлекается главный инженер проекта?",
  "Можно ли давать доступ к сайту проекта субподрядчику?",
  "Как согласовать командировку за границу?",
  "Какая у меня будет премия за сделку?",
];

document.querySelectorAll("nav button").forEach(b => b.onclick = () => {
  document.querySelectorAll("nav button").forEach(x => x.classList.toggle("active", x === b));
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.id === b.dataset.tab));
  ({quality: loadQuality, docs: loadDocs, stats: loadStats, chat: loadMyTickets})[b.dataset.tab]?.();
});

$("#examples").innerHTML = EXAMPLES.map(e => `<button>${esc(e)}</button>`).join("");
$("#examples").onclick = e => { if (e.target.tagName === "BUTTON") { $("#q").value = e.target.textContent; ask(); } };
$("#askForm").onsubmit = e => { e.preventDefault(); ask(); };
$("#newDialog").onclick = () => { dialogId = null; $("#messages").innerHTML = ""; $("#examples").style.display = ""; };

function addMsg(cls, html) {
  const d = document.createElement("div");
  d.className = "msg " + cls; d.innerHTML = html;
  $("#messages").appendChild(d); d.scrollIntoView({block: "end", behavior: "smooth"});
  return d;
}

async function ask(text) {
  const q = (text ?? $("#q").value).trim();
  if (!q || busy) return;
  busy = true; $("#sendBtn").disabled = true; $("#q").value = ""; $("#examples").style.display = "none";
  addMsg("user", esc(q));
  const box = addMsg("bot", `<div class="src"></div><div class="steps"><div><span class="spin"></span>Агент работает…</div></div><div class="res"></div>`);
  const stepsEl = box.querySelector(".steps"); stepsEl.innerHTML = "";
  try {
    const r = await fetch("/api/ask", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({message: q, dialog_id: dialogId, user_name: $("#userName").value, user_email: $("#userEmail").value})});
    const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
    while (true) {
      const {value, done} = await reader.read(); if (done) break;
      buf += dec.decode(value, {stream: true});
      let i; while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1); if (!line.trim()) continue;
        const ev = JSON.parse(line);
        if (ev.type === "step") stepsEl.insertAdjacentHTML("beforeend", stepHtml(ev));
        else if (ev.type === "sources") renderSources(box.querySelector(".src"), ev);
        else if (ev.type === "partial") box.querySelector(".res").innerHTML = `<span class="badge answer">Формулирую ответ…</span><div class="answer-text">${esc(ev.answer)}<span class="caret">▍</span></div>`;
        else if (ev.type === "result") renderResult(box.querySelector(".res"), ev);
        else if (ev.type === "error") box.querySelector(".res").innerHTML = `<span class="badge escalated">Ошибка</span> ${esc(ev.text)}`;
        box.scrollIntoView({block: "end"});
      }
    }
  } catch (e) { box.querySelector(".res").textContent = "Ошибка соединения: " + e; }
  busy = false; $("#sendBtn").disabled = false; $("#q").focus({preventScroll: true}); box.scrollIntoView({block: "end"}); $("#userEmail").value = localStorage.getItem("userEmail") || "";
$("#userEmail").onchange = () => localStorage.setItem("userEmail", $("#userEmail").value);
$("#pollMail").onclick = async () => { const r = await (await fetch("/api/mail/poll", {method: "POST"})).json(); loadQuality(); loadOpenCount();
  $("#pollMail").textContent = `Проверить почту сейчас (обработано писем: ${r.processed})`; };
loadMyTickets(); loadOpenCount();
}

function renderSources(el, ev) {
  el.innerHTML = `<div class="srcbox"><b>Где искать в регламентах (найдено за ${(ev.ms / 1000).toFixed(1)} с):</b>` +
    ev.items.map(i => `<div class="srcitem" data-doc="${i.doc_id}" data-clause="${esc(i.clause_no || "")}">↗ ${esc(i.label)}<small>${esc(i.snippet)}…</small></div>`).join("") + "</div>";
  el.querySelectorAll(".srcitem").forEach(x => x.onclick = () => openDoc(+x.dataset.doc, x.dataset.clause));
}

function stepHtml(ev) {
  let det = "";
  if (Array.isArray(ev.detail) && ev.detail.length) det = `<details><summary>подробнее</summary>${ev.detail.map(esc).join("<br>")}</details>`;
  else if (ev.detail && ev.detail.linked && (ev.detail.linked.length || ev.detail.gaps.length))
    det = `<details><summary>что нашёл граф</summary>${ev.detail.linked.map(esc).join("<br>")}${ev.detail.gaps.length ? "<br><i>вне базы:</i> " + ev.detail.gaps.map(esc).join("; ") : ""}</details>`;
  return `<div class="${ev.tool || ""}"><span class="t">${(ev.ms / 1000).toFixed(1)} с</span>${esc(ev.text)}${det}</div>`;
}

function renderResult(el, r) {
  dialogId = r.dialog_id;
  const label = {answer: "Ответ по регламенту", clarify: "Нужно уточнение", escalated: "Письмо в службу качества", out_of_scope: "Вне регламентов"}[r.status] || r.status;
  let h = `<span class="badge ${r.status}">${label}</span><div class="answer-text">${esc(r.answer)}</div>`;
  if (r.caveat) h += `<div class="caveat">Оговорка: ${esc(r.caveat)}</div>`;
  if (r.steps && r.steps.length) h += `<ol class="steps-list">${r.steps.map(s => `<li>${esc(s)}</li>`).join("")}</ol>`;
  if (r.options && r.options.length) h += `<div class="options">${r.options.map(o => `<button>${esc(o)}</button>`).join("")}</div>`;
  for (const c of r.citations || [])
    h += `<div class="cite ${c.kind === "kb" ? "kb" : ""}" ${c.doc_id ? `data-doc="${c.doc_id}" data-clause="${esc(c.clause_no || "")}" title="Открыть пункт в регламенте"` : ""}>
      <b>${esc(c.source)}${c.doc_id ? " ↗" : ""}</b><q>${esc(c.quote)}</q></div>`;
  if (r.gaps && r.gaps.length) h += `<div class="meta">В регламенте есть ссылки на документы вне базы: ${r.gaps.map(esc).join("; ")}</div>`;
  h += `<div class="meta">${(r.latency_ms / 1000).toFixed(1)} с · вызовов LLM ${r.llm_calls} · токенов ${r.tokens_in}→${r.tokens_out}
        <span class="fb"><button data-v="up">👍 полезно</button><button data-v="down">👎 неверно</button></span>
        <span class="fb"><button class="rep">⚠ сообщить о несоответствии</button></span></div>`;
  el.innerHTML = h;
  el.querySelectorAll(".options button").forEach(b => b.onclick = () => ask(b.textContent));
  el.querySelectorAll(".cite[data-doc]").forEach(c => c.onclick = () => openDoc(+c.dataset.doc, c.dataset.clause));
  el.querySelector(".rep").onclick = async () => {
    const text = prompt("Что не так? Например: регламенты противоречат друг другу, пункт устарел, ответ неточен");
    if (!text) return;
    const res = await (await fetch("/api/report", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({text, query_id: r.query_id, user_name: $("#userName").value})})).json();
    el.querySelector(".rep").parentElement.textContent = `передано в службу качества, запрос № ${res.ticket_id}`;
    $("#userEmail").value = localStorage.getItem("userEmail") || "";
$("#userEmail").onchange = () => localStorage.setItem("userEmail", $("#userEmail").value);
$("#pollMail").onclick = async () => { const r = await (await fetch("/api/mail/poll", {method: "POST"})).json(); loadQuality(); loadOpenCount();
  $("#pollMail").textContent = `Проверить почту сейчас (обработано писем: ${r.processed})`; };
loadMyTickets(); loadOpenCount();
  };
  el.querySelectorAll(".fb button[data-v]").forEach(b => b.onclick = async () => {
    await fetch("/api/feedback", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({query_id: r.query_id, value: b.dataset.v})});
    b.parentElement.textContent = "спасибо, учтено";
  });
}

const fmt = d => d ? new Date(d).toLocaleString("ru-RU", {day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit"}) : "";

async function loadMyTickets() {
  const t = await (await fetch("/api/tickets?user_name=" + encodeURIComponent($("#userName").value))).json();
  $("#myTickets").innerHTML = t.length ? t.map(x => `<div class="t"><span class="st ${x.status}">№ ${x.id} · ${x.status === "open" ? "ждёт ответа" : "есть ответ"}</span>
    <div>${esc(x.question)}</div>${x.answer ? `<div><b>Ответ:</b> ${esc(x.answer)}</div>` : ""}</div>`).join("") : `<div class="hint">Запросов нет</div>`;
}

async function loadOpenCount() {
  const t = await (await fetch("/api/tickets")).json();
  $("#openCount").textContent = t.filter(x => x.status === "open").length || "";
}

async function loadQuality() {
  if (!docsCache.length) docsCache = await (await fetch("/api/docs")).json();
  const t = await (await fetch("/api/tickets")).json();
  const opts = `<option value="">— привязать к регламенту —</option>` + docsCache.map(d => `<option value="${d.id}">${esc(d.title)}</option>`).join("");
  const nOpen = t.filter(x => x.status === "open").length, nOver = t.filter(x => x.overdue).length;
  const summary = `<div class="sla">Ждут ответа: <b>${nOpen}</b>${nOver ? ` · <span class="over">просрочено: <b>${nOver}</b></span>` : " · просроченных нет"}</div>`;
  $("#ticketList").innerHTML = t.length ? summary + t.map(x => `<div class="t ${x.overdue ? "overdue" : ""}" data-id="${x.id}">
    <span class="st ${x.status}">Запрос № ${x.id} · ${fmt(x.created_at)} · ${esc(x.user_name)}${x.department ? " · " + esc(x.department) : ""} · ${x.status === "open" ? (x.overdue ? `<b class="over">ПРОСРОЧЕН — срок ответа был ${fmt(x.due_at)}</b>` : "открыт, ответ до " + fmt(x.due_at)) : "отвечен " + fmt(x.answered_at)}</span>
    <div><b>${esc(x.question)}</b></div>
    ${x.status === "open" && x.context && x.context.redirect_note ? `<div class="hint">Переадресовано (${esc(x.context.redirect_by || "")}): ${esc(x.context.redirect_note)} — ждём окончательного ответа</div>` : ""}
    <details><summary>Письмо на ${esc(x.mail_to)}</summary><pre>${esc(x.mail_body)}</pre></details>
    ${x.status === "open" ? `<textarea placeholder="Ответ службы качества (уйдёт сотруднику и в базу знаний агента)"></textarea>
      <div><select>${opts}</select><input class="cl" placeholder="пункт, напр. 2.3" size="10">
      <label><input type="checkbox" class="kb" checked> добавить в базу знаний</label></div><button>Ответить</button>`
      : `<div><b>Ответ (${esc(x.answered_by)}):</b> ${esc(x.answer)}</div>`}</div>`).join("") : `<div class="hint">Запросов нет</div>`;
  $("#ticketList").querySelectorAll(".t button").forEach(b => b.onclick = async () => {
    const card = b.closest(".t");
    const body = {answer: card.querySelector("textarea").value.trim(), doc_id: +card.querySelector("select").value || null,
      clause_no: card.querySelector(".cl").value.trim() || null, add_to_kb: card.querySelector(".kb").checked};
    if (!body.answer) return;
    b.disabled = true; b.textContent = "Сохраняю…";
    await fetch(`/api/tickets/${card.dataset.id}/answer`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    loadQuality(); loadOpenCount();
  });
  const kb = await (await fetch("/api/kb")).json();
  $("#kbList").innerHTML = kb.length ? `<table><tr><th>№</th><th>Вопрос</th><th>Ответ службы качества</th><th>Привязка</th></tr>` +
    kb.map(a => `<tr><td>${a.id}<br><small>запрос ${a.ticket_id ?? ""}</small></td><td>${esc(a.question)}</td><td>${esc(a.answer)}</td>
      <td>${esc(a.title || "")}${a.clause_no ? ", п. " + esc(a.clause_no) : ""}</td></tr>`).join("") + "</table>" : `<div class="hint">Пока пусто</div>`;
}

async function openDoc(docId, clause) {
  // Сначала готовим текст регламента, потом переключаем вкладку и сразу показываем пункт —
  // без промежуточного кадра с графом в начале вкладки.
  if (!docsCache.length) await loadDocs();
  const ch = await (await fetch("/api/docs/" + docId)).json();
  const d = docsCache.find(x => x.id === docId) || {title: "Документ"};
  $("#docView").innerHTML = `<h3>${esc(d.title)}</h3>` + ch.map(c => `<div class="cite ${c.clause_no === clause ? "hl" : ""}" id="cl-${esc(c.clause_no || c.id)}">
    <b>${esc(c.section || "")}</b>${esc(c.text)}</div>`).join("");
  document.querySelectorAll("nav button").forEach(x => x.classList.toggle("active", x.dataset.tab === "docs"));
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.id === "docs"));
  (document.getElementById("cl-" + clause) || $("#docView")).scrollIntoView({block: "center", behavior: "instant"});
}

async function loadDocs() {
  docsCache = await (await fetch("/api/docs")).json();
  $("#docList").innerHTML = `<table><tr><th>Документ</th><th>Подразделение</th><th>Процесс</th><th>Статус</th><th>Пунктов</th><th>Ссылок</th><th>Из них вне базы</th></tr>` +
    docsCache.map(d => `<tr><td class="doclink" data-id="${d.id}">${esc(d.title)}</td><td>${esc(d.department)}</td><td>${esc(d.process)}</td>
      <td>${d.status === "in_force" ? "действует" : "утратил силу"}</td><td>${d.clauses}</td><td>${d.refs}</td><td>${d.dangling}</td></tr>`).join("") + "</table>";
  $("#docList").querySelectorAll(".doclink").forEach(td => td.onclick = () => openDoc(+td.dataset.id, null));
  const g = await (await fetch("/api/graph")).json();
  drawGraph(g);
  $("#gaps").innerHTML = `<h3>Документы, на которые ссылаются регламенты, но их нет в базе</h3><table><tr><th>Ссылка</th><th>Сколько раз</th><th>Где</th></tr>` +
    g.gaps.map(x => `<tr><td>${esc(x.label)}</td><td>${x.n}</td><td>${x.docs.map(esc).join("<br>")}</td></tr>`).join("") + "</table>";
}

function drawGraph(g) {
  const W = 1200, rowH = 34;
  const docs = g.docs, roles = [...new Set(g.edges.filter(e => e.kind === "mentions_role").map(e => e.dst_node))];
  const gaps = g.gaps.slice(0, 14).map(x => x.dst_node);
  const H = Math.max(docs.length, roles.length, gaps.length) * rowH + 60;
  const pos = {};
  const colors = {"Дирекция ИБ": "#c8102e", "ДБРЦТ": "#1d6fd1", "Технологическая дирекция": "#1a7f37"};
  docs.forEach((d, i) => pos["d" + d.id] = [330, 50 + i * (H - 80) / Math.max(1, docs.length - 1)]);
  roles.forEach((r, i) => pos[r] = [600, 40 + i * rowH]);
  gaps.forEach((r, i) => pos[r] = [900, 40 + i * rowH]);
  let s = `<svg viewBox="0 0 ${W} ${H}">`;
  for (const e of g.edges) {
    const a = pos["d" + e.src_doc], b = e.dst_doc ? pos["d" + e.dst_doc] : pos[e.dst_node];
    if (!a || !b || (e.dst_doc && e.dst_doc === e.src_doc)) continue;
    const col = e.kind === "mentions_role" ? "#c9c9d1" : "#5a3e9b";
    s += `<line x1="${a[0]}" y1="${a[1]}" x2="${b[0]}" y2="${b[1]}" stroke="${col}" stroke-width="${Math.min(4, 0.6 + e.n / 4)}" opacity=".7"/>`;
  }
  docs.forEach(d => { const [x, y] = pos["d" + d.id]; const c = colors[d.department] || "#444";
    s += `<circle cx="${x}" cy="${y}" r="9" fill="${c}"/><text x="${x - 14}" y="${y + 4}" text-anchor="end" font-size="12.5">${esc(d.title.slice(0, 48))}</text>`; });
  const roleName = Object.fromEntries(g.edges.filter(e => e.kind === "mentions_role").map(e => [e.dst_node, e.label]));
  roles.forEach(r => { const [x, y] = pos[r];
    s += `<rect x="${x - 26}" y="${y - 11}" width="52" height="22" rx="11" fill="#f0f0f3" stroke="#bbb"/><text x="${x}" y="${y + 4}" text-anchor="middle" font-size="12" font-weight="600">${esc(r.slice(5))}</text>
          <text x="${x + 34}" y="${y + 4}" font-size="11" fill="#888">${esc(roleName[r] || "")}</text>`; });
  const gapLabel = Object.fromEntries(g.gaps.map(x => [x.dst_node, x.label]));
  gaps.forEach(r => { const [x, y] = pos[r];
    s += `<rect x="${x - 6}" y="${y - 9}" width="12" height="18" fill="#fff" stroke="#5a3e9b" stroke-dasharray="3 2"/><text x="${x + 14}" y="${y + 4}" font-size="12" fill="#5a3e9b">${esc(gapLabel[r].slice(0, 42))}</text>`; });
  s += `<text x="260" y="18" text-anchor="middle" font-size="12" fill="#888">Регламенты</text><text x="600" y="18" text-anchor="middle" font-size="12" fill="#888">Роли</text>
        <text x="960" y="18" font-size="12" fill="#888">Ссылки на документы вне базы (пробелы)</text></svg>`;
  $("#graph").innerHTML = s;
}

async function loadStats() {
  const s = await (await fetch("/api/stats")).json(); const m = s.summary;
  $("#statsBox").innerHTML = `<div class="kpis">
    <div class="kpi"><b>${m.queries}</b><span>вопросов</span></div><div class="kpi"><b>${m.answered}</b><span>ответов по регламенту</span></div>
    <div class="kpi"><b>${m.clarify}</b><span>уточняющих вопросов</span></div><div class="kpi"><b>${m.escalated}</b><span>передано в службу качества</span></div>
    <div class="kpi"><b>${m.avg_ms ? (m.avg_ms / 1000).toFixed(1) + " с" : "—"}</b><span>среднее время ответа</span></div>
    <div class="kpi"><b>${m.avg_in ?? "—"} → ${m.avg_out ?? "—"}</b><span>токенов на шаг (вход → выход)</span></div>
    <div class="kpi"><b>${m.avg_calls ?? "—"}</b><span>вызовов LLM на вопрос</span></div>
    <div class="kpi"><b>${m.out_of_scope}</b><span>вне регламентов</span></div>
    <div class="kpi"><b>${s.tickets.open} / ${s.tickets.total}</b><span>писем в службу качества: ждут ответа / всего</span></div>
    <div class="kpi ${s.tickets.overdue ? "over" : ""}"><b>${s.tickets.overdue}</b><span>ответов службы качества просрочено</span></div></div>
    ${s.hot.length ? `<h3>Частые вопросы, закрытые разъяснениями службы качества — кандидаты на внесение в регламент</h3><table><tr><th>Вопрос</th><th>Разъяснение</th><th>Предложение агента по регламенту</th><th>Использовано раз</th></tr>` +
      s.hot.map(x => `<tr><td>${esc(x.question)}</td><td>${esc(x.answer)}</td><td>${esc(x.proposal || "—")}</td><td>${x.uses}</td></tr>`).join("") + "</table><h3>Журнал вопросов</h3>" : ""}
    <table><tr><th>Время</th><th>Сотрудник</th><th>Вопрос</th><th>Итог</th><th>с</th><th>Токены</th><th>Оценка</th></tr>` +
    s.last.map(q => `<tr><td>${fmt(q.ts)}</td><td>${esc(q.user_name)}</td><td>${esc(q.question)}</td><td>${q.outcome}</td>
      <td>${(q.latency_ms / 1000).toFixed(1)}</td><td>${q.tokens_in}→${q.tokens_out}</td><td>${q.feedback ?? ""}</td></tr>`).join("") + "</table>";
}

$("#userEmail").value = localStorage.getItem("userEmail") || "";
$("#userEmail").onchange = () => localStorage.setItem("userEmail", $("#userEmail").value);
$("#pollMail").onclick = async () => { const r = await (await fetch("/api/mail/poll", {method: "POST"})).json(); loadQuality(); loadOpenCount();
  $("#pollMail").textContent = `Проверить почту сейчас (обработано писем: ${r.processed})`; };
loadMyTickets(); loadOpenCount();
