// Интерфейс агента: вход, чат с лентой шагов (NDJSON-поток), служба качества, регламенты и граф, аналитика, пользователи.
// Вкладки — по роли учётной записи (сотрудник / служба качества / администратор); права проверяет сервер.
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
let dialogId = null, busy = false, docsCache = [], ME = null, ROLES = {};
const RANK = {employee: 0, quality: 1, admin: 2};
const can = role => ME && RANK[ME.role] >= RANK[role];
const fmt = d => d ? new Date(d).toLocaleString("ru-RU", {day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit"}) : "";

// Все запросы к API: при окончании сессии — снова экран входа.
async function api(url, opts = {}) {
  const r = await fetch(url, opts);
  if (r.status === 401 && !url.startsWith("/api/login")) showLogin();
  return r;
}

const EXAMPLES = [
  "Кто назначает руководителя проекта?",
  "Кто создаёт карточку проекта в CRM в Дирекции ИБ?",
  "Когда привлекается главный инженер проекта?",
  "Можно ли давать доступ к сайту проекта субподрядчику?",
  "Как согласовать командировку за границу?",
  "Какая у меня будет премия за сделку?",
];

// ---------- вход и роли ----------
function showLogin() {
  ME = null;
  document.body.className = "locked";
  $("#loginPass").value = "";
}

async function start() {
  const r = await fetch("/api/me");
  if (!r.ok) { showLogin(); return; }
  const j = await r.json();
  ME = j.user; ROLES = j.roles;
  document.body.className = "role-" + ME.role;
  $("#who").textContent = `${ME.name} · ${ROLES[ME.role]}${ME.department ? " · " + ME.department : ""}`;
  $("#profile").innerHTML = `<b>${esc(ME.name)}</b><br>${esc(ROLES[ME.role])}${ME.department ? `<br>Подразделение: ${esc(ME.department)}` : ""}`;
  $("#userEmail").value = localStorage.getItem("userEmail:" + ME.login) || ME.email || "";
  loadMyTickets();
  if (can("quality")) loadOpenCount();
  // Вход по адресу /admin — служба качества сразу на своей вкладке.
  if (location.pathname.startsWith("/admin") && can("quality")) openTab("quality");
}

$("#loginForm").onsubmit = async e => {
  e.preventDefault();
  const r = await fetch("/api/login", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({login: $("#loginName").value, password: $("#loginPass").value})});
  if (!r.ok) { $("#loginErr").textContent = (await r.json()).detail || "не удалось войти"; return; }
  $("#loginErr").textContent = "";
  start();
};
$("#logout").onclick = async () => { await fetch("/api/logout", {method: "POST"}); dialogId = null; $("#messages").innerHTML = ""; showLogin(); };

// ---------- вкладки ----------
function openTab(name) {
  document.querySelectorAll("nav button").forEach(x => x.classList.toggle("active", x.dataset.tab === name));
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.id === name));
  ({quality: loadQuality, docs: loadDocs, stats: loadStats, chat: loadMyTickets, users: loadUsers})[name]?.();
}
document.querySelectorAll("nav button").forEach(b => b.onclick = () => openTab(b.dataset.tab));

// ---------- чат ----------
$("#examples").innerHTML = EXAMPLES.map(e => `<button>${esc(e)}</button>`).join("");
$("#examples").onclick = e => { if (e.target.tagName === "BUTTON") { $("#q").value = e.target.textContent; ask(); } };
$("#askForm").onsubmit = e => { e.preventDefault(); ask(); };
$("#newDialog").onclick = () => { dialogId = null; $("#messages").innerHTML = ""; $("#examples").style.display = ""; };
$("#userEmail").onchange = () => ME && localStorage.setItem("userEmail:" + ME.login, $("#userEmail").value);

function addMsg(cls, html) {
  const d = document.createElement("div");
  d.className = "msg " + cls; d.innerHTML = html;
  $("#messages").appendChild(d); d.scrollIntoView({block: "end", behavior: "smooth"});
  return d;
}

async function ask(text, nocache = false) {
  const q = (text ?? $("#q").value).trim();
  if (!q || busy) return;
  busy = true; $("#sendBtn").disabled = true; $("#q").value = ""; $("#examples").style.display = "none";
  addMsg("user", esc(q));
  const box = addMsg("bot", `<div class="src"></div><div class="steps"><div><span class="spin"></span>Агент работает…</div></div><div class="res"></div>`);
  const stepsEl = box.querySelector(".steps"); stepsEl.innerHTML = "";
  try {
    const r = await api("/api/ask", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({message: q, dialog_id: dialogId, user_email: $("#userEmail").value, nocache})});
    if (!r.ok) throw new Error((await r.json()).detail || r.status);
    const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
    while (true) {
      const {value, done} = await reader.read(); if (done) break;
      buf += dec.decode(value, {stream: true});
      let i; while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1); if (!line.trim()) continue;
        const ev = JSON.parse(line);
        if (ev.type === "step") { stepsEl.insertAdjacentHTML("beforeend", stepHtml(ev));
          // Модель сначала выписывает дословные цитаты, потом пишет ответ — показываем это, а не пустой курсор.
          if (ev.tool === "answer") box.querySelector(".res").innerHTML = `<span class="badge answer">Выписываю дословные цитаты из пунктов…</span>`; }
        else if (ev.type === "sources") renderSources(box.querySelector(".src"), ev);
        else if (ev.type === "partial") box.querySelector(".res").innerHTML = `<span class="badge answer">Черновик — проверяю цитаты…</span><div class="answer-text">${esc(ev.answer)}<span class="caret">▍</span></div>`;
        else if (ev.type === "result") renderResult(box.querySelector(".res"), ev, q);
        else if (ev.type === "error") box.querySelector(".res").innerHTML = `<span class="badge escalated">Ошибка</span> ${esc(ev.text)}`;
        box.scrollIntoView({block: "end"});
      }
    }
  } catch (e) { box.querySelector(".res").textContent = "Ошибка соединения: " + e.message; }
  busy = false; $("#sendBtn").disabled = false; $("#q").focus({preventScroll: true}); box.scrollIntoView({block: "end"});
  loadMyTickets();
  if (can("quality")) loadOpenCount();
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

function renderResult(el, r, asked) {
  dialogId = r.dialog_id;
  const label = {answer: "Ответ по регламенту", clarify: "Нужно уточнение", escalated: "Письмо в службу качества", out_of_scope: "Вне регламентов"}[r.status] || r.status;
  let h = `<span class="badge ${r.status}">${label}</span><div class="answer-text">${esc(r.answer)}</div>`;
  if (r.scope_note) h += `<div class="meta scope">${esc(r.scope_note)}</div>`;
  if (r.from_cache && r.cache_question) h += `<div class="meta scope">Мгновенный ответ на тот же по смыслу вопрос: «${esc(r.cache_question)}» (запрос № ${r.from_cache}); регламенты и разъяснения с тех пор не менялись.
      <button class="recheck">Это другой вопрос — ответить заново</button></div>`;
  else if (r.from_cache) h += `<div class="meta scope">Мгновенный ответ: такой же вопрос уже задавали (запрос № ${r.from_cache}), регламенты и разъяснения с тех пор не менялись.</div>`;
  if (r.caveat) h += `<div class="caveat">Оговорка: ${esc(r.caveat)}</div>`;
  if (r.steps && r.steps.length) h += `<ol class="steps-list">${r.steps.map(s => `<li>${esc(s)}</li>`).join("")}</ol>`;
  if (r.options && r.options.length) h += `<div class="options">${r.options.map(o => `<button>${esc(o)}</button>`).join("")}</div>`;
  for (const c of r.citations || [])
    h += `<div class="cite ${c.kind === "kb" ? "kb" : ""}" ${c.doc_id ? `data-doc="${c.doc_id}" data-clause="${esc(c.clause_no || "")}" title="Открыть пункт в регламенте"` : ""}>
      <b>${esc(c.source)}${c.doc_id ? " ↗" : ""}</b>${c.quote ? `<q>${esc(c.quote)}</q>` : ""}</div>`;
  // «Также учтите»: связанные пункты того же регламента, которые не вошли в ответ (как подсказывает специалист качества).
  if (r.see_also && r.see_also.length) h += `<div class="seealso"><b>Также учтите:</b>` + r.see_also.map(x =>
      `<div class="cite" data-doc="${x.doc_id}" data-clause="${esc(x.clause_no || "")}" title="Открыть пункт в регламенте"><b>${esc(x.label)} ↗</b><small>${esc(x.snippet)}…</small></div>`).join("") + "</div>";
  if (r.gaps && r.gaps.length) h += `<div class="meta">В регламенте есть ссылки на документы вне базы: ${r.gaps.map(esc).join("; ")}</div>`;
  h += `<div class="meta">${(r.latency_ms / 1000).toFixed(1)} с · вызовов LLM ${r.llm_calls} · токенов ${r.tokens_in}→${r.tokens_out}
        <span class="fb"><button data-v="up">👍 полезно</button><button data-v="down">👎 неверно</button></span>
        <span class="fb"><button class="rep">⚠ сообщить о несоответствии</button></span></div>`;
  el.innerHTML = h;
  el.querySelectorAll(".options button").forEach(b => b.onclick = () => ask(b.textContent));
  const re = el.querySelector(".recheck");
  if (re && asked) re.onclick = async () => {   // отметка в журнале — статистика для настройки смыслового кэша
    re.disabled = true;
    await api("/api/feedback", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({query_id: r.query_id, value: "other_question"})});
    ask(asked, true);
  };
  el.querySelectorAll(".cite[data-doc]").forEach(c => c.onclick = () => openDoc(+c.dataset.doc, c.dataset.clause));
  el.querySelector(".rep").onclick = async () => {
    const text = prompt("Что не так? Например: регламенты противоречат друг другу, пункт устарел, ответ неточен");
    if (!text) return;
    const res = await (await api("/api/report", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({text, query_id: r.query_id})})).json();
    el.querySelector(".rep").parentElement.textContent = `передано в службу качества, запрос № ${res.ticket_id}`;
  };
  el.querySelectorAll(".fb button[data-v]").forEach(b => b.onclick = async () => {
    await api("/api/feedback", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({query_id: r.query_id, value: b.dataset.v})});
    b.parentElement.textContent = "спасибо, учтено";
  });
}

async function loadMyTickets() {
  if (!ME) return;
  const r = await api("/api/tickets?mine=1");
  if (!r.ok) return;
  const t = await r.json();
  $("#myTickets").innerHTML = t.length ? t.map(x => `<div class="t"><span class="st ${x.status}">№ ${x.id} · ${x.status === "open" ? "ждёт ответа" : "есть ответ"}</span>
    <div>${esc(x.question)}</div>${x.answer ? `<div><b>Ответ:</b> ${esc(x.answer)}</div>` : ""}</div>`).join("") : `<div class="hint">Запросов нет</div>`;
}

// ---------- служба качества ----------
async function loadOpenCount() {
  const r = await api("/api/tickets");
  if (!r.ok) return;
  $("#openCount").textContent = (await r.json()).filter(x => x.status === "open").length || "";
}

$("#pollMail").onclick = async () => {
  const r = await (await api("/api/mail/poll", {method: "POST"})).json();
  loadQuality(); loadOpenCount();
  $("#pollMail").textContent = `Проверить почту сейчас (обработано писем: ${r.processed})`;
};

async function loadQuality() {
  if (!docsCache.length) docsCache = await (await api("/api/docs")).json();
  const t = await (await api("/api/tickets")).json();
  const S = await (await api("/api/settings")).json();
  const opts = `<option value="">— привязать к регламенту —</option>` + docsCache.map(d => `<option value="${d.id}">${esc(d.title)}</option>`).join("");
  const nOpen = t.filter(x => x.status === "open").length, nOver = t.filter(x => x.overdue).length;
  const summary = `<div class="sla">Ждут ответа: <b>${nOpen}</b>${nOver ? ` · <span class="over">просрочено: <b>${nOver}</b></span>` : " · просроченных нет"}</div>`;
  $("#ticketList").innerHTML = t.length ? summary + t.map(x => `<div class="t ${x.overdue ? "overdue" : ""}" data-id="${x.id}">
    <span class="st ${x.status}">Запрос № ${x.id} · ${fmt(x.created_at)} · ${esc(x.user_name)}${x.department ? " · " + esc(x.department) : ""} · ${x.status === "open" ? (x.overdue ? `<b class="over">ПРОСРОЧЕН — срок ответа был ${fmt(x.due_at)}</b>` : "открыт, ответ до " + fmt(x.due_at)) : "отвечен " + fmt(x.answered_at)}</span>
    <div><b>${esc(x.question)}</b></div>
    ${x.status === "open" && x.context && x.context.redirect_note ? `<div class="hint">Переадресовано (${esc(x.context.redirect_by || "")}): ${esc(x.context.redirect_note)} — ждём окончательного ответа</div>` : ""}
    ${x.status === "open" && x.context && x.context.pending_answer ? `<div class="hint over">Ответ владельца процесса (${esc(x.context.pending_by || "")}) на подтверждении службы качества: ${esc(x.context.pending_answer)}</div>` : ""}
    <details><summary>Письмо на ${esc(x.mail_to)}</summary><pre>${esc(x.mail_body)}</pre></details>
    ${x.status === "open" && !S.web_answer ? `<div class="hint">Ответ службы качества — письмом («Ответить всем»): агент заберёт его из почты сам.</div>` : x.status === "open" ? `<textarea placeholder="Ответ службы качества (уйдёт сотруднику и в базу знаний агента)"></textarea>
      <div><select>${opts}</select><input class="cl" placeholder="пункт, напр. 2.3" size="10">
      <label><input type="checkbox" class="kb" checked> добавить в базу знаний</label></div><button>Ответить</button>`
      : `<div><b>Ответ (${esc(x.answered_by)}):</b> ${esc(x.answer)}</div>`}</div>`).join("") : `<div class="hint">Запросов нет</div>`;
  $("#ticketList").querySelectorAll(".t button").forEach(b => b.onclick = async () => {
    const card = b.closest(".t");
    const body = {answer: card.querySelector("textarea").value.trim(), doc_id: +card.querySelector("select").value || null,
      clause_no: card.querySelector(".cl").value.trim() || null, add_to_kb: card.querySelector(".kb").checked};
    if (!body.answer) return;
    b.disabled = true; b.textContent = "Сохраняю…";
    await api(`/api/tickets/${card.dataset.id}/answer`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    loadQuality(); loadOpenCount();
  });
  const kb = await (await api("/api/kb")).json();
  $("#kbList").innerHTML = kb.length ? `<table><tr><th>№</th><th>Вопрос</th><th>Ответ службы качества</th><th>Привязка</th></tr>` +
    kb.map(a => `<tr><td>${a.id}<br><small>запрос ${a.ticket_id ?? ""}</small></td><td>${esc(a.question)}</td><td>${esc(a.answer)}</td>
      <td>${esc(a.title || "")}${a.clause_no ? ", п. " + esc(a.clause_no) : ""}</td></tr>`).join("") + "</table>" : `<div class="hint">Пока пусто</div>`;
}

// ---------- регламенты, загрузка, граф ----------
async function openDoc(docId, clause) {
  // Сначала готовим текст регламента, потом переключаем вкладку и сразу показываем пункт —
  // без промежуточного кадра с графом в начале вкладки.
  if (!docsCache.length) await loadDocs();
  const ch = await (await api("/api/docs/" + docId)).json();
  const d = docsCache.find(x => x.id === docId) || {title: "Документ"};
  $("#docView").innerHTML = `<h3>${esc(d.title)}${d.status === "superseded" ? " — утратил силу" : ""}</h3>` + ch.map(c => `<div class="cite ${c.clause_no === clause ? "hl" : ""}" id="cl-${esc(c.clause_no || c.id)}">
    <b>${esc(c.section || "")}</b>${esc(c.text)}</div>`).join("");
  document.querySelectorAll("nav button").forEach(x => x.classList.toggle("active", x.dataset.tab === "docs"));
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.id === "docs"));
  (document.getElementById("cl-" + clause) || $("#docView")).scrollIntoView({block: "center", behavior: "instant"});
}

async function loadDocs() {
  docsCache = await (await api("/api/docs")).json();
  const title = id => (docsCache.find(x => x.id === id) || {}).title || "";
  $("#docList").innerHTML = `<table><tr><th>Документ</th><th>Подразделение</th><th>Процесс</th><th>Статус</th><th>Пунктов</th><th>Ссылок</th><th>Из них вне базы</th><th></th></tr>` +
    docsCache.map(d => `<tr><td class="doclink" data-id="${d.id}">${esc(d.title)}${d.origin === "upload" ? ` <small class="hint">загружен</small>` : ""}</td>
      <td>${esc(d.department || "вся компания")}</td><td>${esc(d.process)}</td>
      <td>${d.status === "in_force" ? "действует" : `утратил силу${d.superseded_by ? ` — заменён «${esc(title(d.superseded_by))}»` : ""}`}</td>
      <td>${d.clauses}</td><td>${d.refs}</td><td>${d.dangling}</td>
      <td>${can("admin") && d.origin === "upload" ? `<button class="del" data-id="${d.id}" title="Удалить загруженный документ">удалить</button>` : ""}</td></tr>`).join("") + "</table>";
  $("#docList").querySelectorAll(".doclink").forEach(td => td.onclick = () => openDoc(+td.dataset.id, null));
  $("#docList").querySelectorAll(".del").forEach(b => b.onclick = async () => {
    if (!confirm("Удалить загруженный документ из базы агента? Если он заменял прежнюю редакцию, она снова будет действовать.")) return;
    const r = await api("/api/docs/" + b.dataset.id, {method: "DELETE"});
    if (!r.ok) { alert((await r.json()).detail || "не удалось удалить"); return; }
    loadDocs();
  });
  if (can("admin")) fillUploadForm();
  if (!can("quality")) return;   // граф и пробелы базы — для службы качества
  const g = await (await api("/api/graph")).json();
  drawGraph(g);
  $("#gaps").innerHTML = `<h3>Документы, на которые ссылаются регламенты, но их нет в базе</h3><table><tr><th>Ссылка</th><th>Сколько раз</th><th>Где</th></tr>` +
    g.gaps.map(x => `<tr><td>${esc(x.label)}</td><td>${x.n}</td><td>${x.docs.map(esc).join("<br>")}</td></tr>`).join("") + "</table>";
}

function fillUploadForm() {
  const deps = [...new Set(docsCache.map(d => d.department).filter(Boolean))];
  $("#depList").innerHTML = deps.map(d => `<option value="${esc(d)}">`).join("");
  $("#upRepl").innerHTML = `<option value="">— новый документ —</option>` +
    docsCache.filter(d => d.status === "in_force").map(d => `<option value="${d.id}">${esc(d.title)}</option>`).join("");
}

$("#upGo").onclick = async () => {
  const f = $("#upFile").files[0];
  if (!f) { alert("Выберите файл .docx"); return; }
  const qs = new URLSearchParams({filename: f.name, department: $("#upDep").value, process: $("#upProc").value});
  if ($("#upRepl").value) qs.set("replaces", $("#upRepl").value);
  $("#upGo").disabled = true;
  $("#upResult").innerHTML = `<p class="hint"><span class="spin"></span>Разбираю документ и считаю эмбеддинги пунктов — на стенде без GPU около минуты на документ…</p>`;
  try {
    const r = await api("/api/docs/upload?" + qs, {method: "POST", body: f});
    const j = await r.json();
    if (!r.ok) { $("#upResult").innerHTML = `<p class="caveat">Не загружен: ${esc(j.detail || r.status)}</p>`; return; }
    const terms = Object.entries(j.terms || {}).map(([a, n]) => `${a} — ${n}`).join("; ");
    $("#upResult").innerHTML = `<div class="upok"><b>Загружен за ${j.seconds} с: «${esc(j.title)}»</b><br>
      Подразделение: ${esc(j.department || "не определено — общий для всей компании")}; процесс: ${esc(j.process || "—")}<br>
      Пунктов: ${j.clauses} (с номером ${j.numbered}); ссылок на пункты ${j.links.refs_internal || 0}, на другие документы
      ${j.links.refs_external || 0} (вне базы ${j.links.dangling || 0}); упоминаний ролей ${j.links.roles || 0}
      ${terms ? `<br>Сокращения из документа: ${esc(terms)}` : ""}
      ${(j.warnings || []).map(w => `<div class="caveat">Внимание: ${esc(w)}</div>`).join("")}</div>`;
    loadDocs();
  } finally { $("#upGo").disabled = false; }
};

function drawGraph(g) {
  const W = 1200, rowH = 34;
  const docs = g.docs, roles = [...new Set(g.edges.filter(e => e.kind === "mentions_role").map(e => e.dst_node))];
  const gaps = g.gaps.slice(0, 14).map(x => x.dst_node);
  const H = Math.max(docs.length, roles.length, gaps.length) * rowH + 60;
  const pos = {};
  const colors = {"Дирекция ИБ": "#c8102e", "ДБРЦТ": "#1d6fd1", "Технологическая дирекция": "#1a7f37"};
  const extra = ["#b26b00", "#7a3fb8", "#00838f", "#ad1457", "#5d6b00"];
  [...new Set(docs.map(d => d.department).filter(d => d && !colors[d]))].forEach((d, i) => colors[d] = extra[i % extra.length]);
  docs.forEach((d, i) => pos["d" + d.id] = [330, 50 + i * (H - 80) / Math.max(1, docs.length - 1)]);
  roles.forEach((r, i) => pos[r] = [600, 40 + i * rowH]);
  gaps.forEach((r, i) => pos[r] = [900, 40 + i * rowH]);
  let s = `<svg viewBox="0 0 ${W} ${H}">`;
  for (const e of g.edges) {
    const a = pos["d" + e.src_doc], b = e.dst_doc ? pos["d" + e.dst_doc] : pos[e.dst_node];
    if (!a || !b || (e.dst_doc && e.dst_doc === e.src_doc)) continue;
    if (e.kind === "supersedes") {   // новая редакция → старая
      s += `<path d="M${a[0] - 9},${a[1]} C${a[0] - 60},${a[1]} ${b[0] - 60},${b[1]} ${b[0] - 9},${b[1]}" fill="none" stroke="#b26b00" stroke-width="2" stroke-dasharray="5 3"/>`;
      continue;
    }
    const col = e.kind === "mentions_role" ? "#c9c9d1" : "#5a3e9b";
    s += `<line x1="${a[0]}" y1="${a[1]}" x2="${b[0]}" y2="${b[1]}" stroke="${col}" stroke-width="${Math.min(4, 0.6 + e.n / 4)}" opacity=".7"/>`;
  }
  docs.forEach(d => { const [x, y] = pos["d" + d.id]; const c = colors[d.department] || "#444";
    s += `<circle cx="${x}" cy="${y}" r="9" fill="${d.status === "superseded" ? "#fff" : c}" stroke="${c}" stroke-width="2"/>
          <text x="${x - 14}" y="${y + 4}" text-anchor="end" font-size="12.5" ${d.status === "superseded" ? 'fill="#999" text-decoration="line-through"' : ""}>${esc(d.title.slice(0, 48))}</text>`; });
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

// ---------- аналитика ----------
async function loadStats() {
  const s = await (await api("/api/stats")).json(); const m = s.summary;
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
      s.hot.map(x => `<tr><td>${esc(x.question)}</td><td>${esc(x.answer)}</td><td>${esc(x.proposal || "—")}</td><td>${x.uses}</td></tr>`).join("") + "</table>" : ""}
    ${perfHtml(s.perf)}
    <h3>Журнал вопросов</h3>
    <table><tr><th>Время</th><th>Сотрудник</th><th>Вопрос</th><th>Итог</th><th>с</th><th>Токены</th><th>Оценка</th></tr>` +
    s.last.map(q => `<tr><td>${fmt(q.ts)}</td><td>${esc(q.user_name)}</td><td>${esc(q.question)}</td><td>${q.outcome}</td>
      <td>${(q.latency_ms / 1000).toFixed(1)}</td><td>${q.tokens_in}→${q.tokens_out}</td><td>${q.feedback ?? ""}</td></tr>`).join("") + "</table>";
}

// Узкие места: время этапов агента по журналу (последние 500 вопросов) и подсказка, что тормозит.
function perfHtml(p) {
  if (!p || !p.queries) return "";
  const sec = ms => ms == null ? "—" : (ms / 1000).toFixed(1) + " с";
  return `<h3>Производительность и узкие места (последние ${p.queries} вопросов)</h3>
    <div class="kpis"><div class="kpi"><b>${sec(p.p50_ms)}</b><span>медиана времени ответа</span></div>
    <div class="kpi"><b>${sec(p.p95_ms)}</b><span>95 % вопросов быстрее</span></div>
    <div class="kpi ${p.slow_share >= 0.3 ? "over" : ""}"><b>${Math.round(p.slow_share * 100)} %</b><span>ответов дольше минуты</span></div>
    <div class="kpi"><b>${p.gen_tokens_per_s ?? "—"}</b><span>выходных ток/с на этапе ответа (с чтением контекста)</span></div></div>
    <table><tr><th>Этап</th><th>Вопросов</th><th>Медиана</th><th>95-й перцентиль</th><th>Доля общего времени</th></tr>` +
    p.stages.map(x => `<tr><td>${esc(x.stage)}</td><td>${x.n}</td><td>${sec(x.p50_ms)}</td><td>${sec(x.p95_ms)}</td>
      <td><span class="bar" style="width:${Math.round(x.share * 120)}px"></span> ${Math.round(x.share * 100)} %</td></tr>`).join("") +
    `</table><ul class="hints">${p.hints.map(h => `<li>${esc(h)}</li>`).join("")}</ul>`;
}

// ---------- пользователи (администратор) ----------
async function loadUsers() {
  const diagHref = () => $("#diagGo").href = `/api/diag/export?level=${$("#diagLevel").value}&days=${+$("#diagDays").value || 30}`;
  $("#diagLevel").onchange = $("#diagDays").oninput = diagHref;
  const users = await (await api("/api/users")).json();
  const roleSel = (cur) => Object.entries(ROLES).map(([k, v]) => `<option value="${k}" ${k === cur ? "selected" : ""}>${esc(v)}</option>`).join("");
  $("#userList").innerHTML = `<table><tr><th>Логин</th><th>Имя</th><th>E-mail</th><th>Подразделение</th><th>Роль</th><th>Последний вход</th><th></th></tr>` +
    users.map(u => `<tr data-id="${u.id}" class="${u.active ? "" : "off"}"><td>${esc(u.login)}</td><td>${esc(u.name)}</td><td>${esc(u.email || "")}</td>
      <td>${esc(u.department || "")}</td><td><select class="urole">${roleSel(u.role)}</select></td><td>${fmt(u.last_login)}</td>
      <td><button class="ureset">сбросить пароль</button> <button class="uact">${u.active ? "отключить" : "включить"}</button></td></tr>`).join("") + "</table>";
  const row = el => el.closest("tr").dataset.id;
  const patch = async (id, body) => {
    const r = await api("/api/users/" + id, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    if (!r.ok) alert((await r.json()).detail || "не удалось");
    loadUsers();
  };
  $("#userList").querySelectorAll(".urole").forEach(s => s.onchange = () => patch(row(s), {role: s.value}));
  $("#userList").querySelectorAll(".uact").forEach(b => b.onclick = () => patch(row(b), {active: b.textContent === "включить"}));
  $("#userList").querySelectorAll(".ureset").forEach(b => b.onclick = async () => {
    if (!confirm("Сбросить пароль? Старый перестанет действовать, открытые сессии пользователя закроются.")) return;
    const r = await api(`/api/users/${row(b)}/reset`, {method: "POST"});
    const j = await r.json();
    if (!r.ok) return alert(j.detail || "не удалось");
    showPassword(j.password);
  });
  const deps = [...new Set(docsCache.map(d => d.department).filter(Boolean))];
  $("#nuDepList").innerHTML = deps.map(d => `<option value="${esc(d)}">`).join("");
  $("#nuRole").innerHTML = roleSel("employee");
}

function showPassword(pw) {
  $("#userPw").innerHTML = `<div class="upok">Пароль (показан один раз — передайте пользователю): <code>${esc(pw)}</code></div>`;
}

$("#nuGo").onclick = async () => {
  const body = {login: $("#nuLogin").value, name: $("#nuName").value, email: $("#nuEmail").value,
    department: $("#nuDep").value, role: $("#nuRole").value};
  const r = await api("/api/users", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const j = await r.json();
  if (!r.ok) { $("#userPw").innerHTML = `<p class="caveat">${esc(j.detail || r.status)}</p>`; return; }
  showPassword(j.password);
  ["#nuLogin", "#nuName", "#nuEmail", "#nuDep"].forEach(s => $(s).value = "");
  loadUsers();
};

start();
