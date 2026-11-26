"""Агентный цикл: разбор вопроса → план поиска → поиск → обход графа → проверка однозначности →
ответ строго по фрагментам → проверка цитат кодом → ответ / уточняющий вопрос / запрос в службу качества.

run() — генератор событий для интерфейса: {"type": "step", ...} по ходу работы и финальный {"type": "result", ...}.
"""
import json
import re
import time
import uuid

from . import config, db, llm, search
from .search import departments
from .ingest import ROLES

GLOSSARY = "; ".join(f"{k} — {v}" for k, v in ROLES.items())

DEP_ALIASES = {
    "Дирекция ИБ": r"\bИБ\b|информационн\w* безопасност|дирекци\w* иб",
    "ДБРЦТ": r"ДБРЦТ|бизнес-решени|цифров\w* трансформац",
    "Технологическая дирекция": r"тех\.?\s*дирекц|технологическ\w* дирекц|технической дирекц",
}

SYS_PLAN = """Ты планируешь поиск по внутренним регламентам компании (пресейл и проектная деятельность подразделений).
Верни только JSON: {"in_scope": true|false, "subqueries": ["...", "..."]}.
in_scope=false — вопрос не о работе компании (погода, общие знания, личное).
subqueries — 1–3 короткие поисковые фразы на русском, переформулирующие вопрос терминами регламентов
(роли: SS, АМ, ПА, РП, ГИП, Координатор департамента; объекты: карточка проекта, CRM, расчёт, ТКП, договор).
Ничего не добавляй от себя."""

SYS_ANSWER = f"""Ты отвечаешь сотруднику компании СТРОГО по фрагментам регламентов и разъяснениям службы качества ниже.
Правила:
1. Используй только факты из фрагментов. Ничего не добавляй из общих знаний, не обобщай, не додумывай.
2. Если во фрагментах ответа на вопрос нет — status="not_found".
3. Если ответ зависит от условия, которого нет в вопросе (несколько разных сценариев/вариантов в регламенте),
   и по вопросу нельзя выбрать один — status="clarify": задай ОДИН уточняющий вопрос и дай 2–4 варианта ответа.
4. Иначе status="answer": краткий ответ (1–2 предложения) и, если уместно, шаги «что сделать».
5. Сначала выпиши цитаты из фрагментов, относящиеся к вопросу, затем ответ, затем status.
   Каждое утверждение подтверди цитатой: id фрагмента и ДОСЛОВНАЯ фраза из него (5–12 слов, без изменений), 1–2 цитаты.
   Цитируй только фрагменты, которые прямо отвечают на вопрос; фрагменты о других ролях и процессах не используй.
Сокращения в регламентах: {GLOSSARY}. Вопрос и фрагмент могут называть роль полностью или сокращением — это одно и то же.
Верни только JSON."""

ANSWER_SCHEMA = {
    # Порядок полей = порядок генерации: сначала дословные цитаты, затем ответ, решение (status) — последним.
    "type": "object",
    "properties": {
        "citations": {"type": "array", "maxItems": 3, "items": {
            "type": "object", "properties": {"id": {"type": "string"}, "quote": {"type": "string"}},
            "required": ["id", "quote"]}},
        "answer": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        "clarify_question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        "status": {"type": "string", "enum": ["answer", "clarify", "not_found"]},
    },
    "required": ["citations", "answer", "status"],
}


def norm(s: str) -> str:
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[«»\"'“”„()\[\]—–\-:;,.!?]", " ", s)
    return " ".join(s.split())


def detect_department(text: str) -> str | None:
    for dep, pat in DEP_ALIASES.items():
        if re.search(pat, text, re.I):
            return dep
    return None


def load_dialog(dialog_id: str) -> dict:
    with db.conn() as c:
        r = c.execute("SELECT state FROM dialogs WHERE id=%s", (dialog_id,)).fetchone()
    return r["state"] if r else {}


def save_dialog(dialog_id: str, state: dict):
    with db.conn() as c:
        c.execute("INSERT INTO dialogs(id,state) VALUES (%s,%s) ON CONFLICT (id) DO UPDATE SET state=EXCLUDED.state",
                  (dialog_id, json.dumps(state, ensure_ascii=False)))


def fragment_label(r: dict) -> str:
    where = f"{r['title']}, п. {r['clause_no']}" if r.get("clause_no") else r["title"]
    return f"{where} ({r.get('department') or 'вся компания'})"


def run(message: str, dialog_id: str | None = None, user_name: str = "Сотрудник"):
    t0 = time.time()
    usage = llm.Usage()
    steps = []
    dialog_id = dialog_id or uuid.uuid4().hex[:12]
    state = load_dialog(dialog_id)

    def step(text, **extra):
        ev = {"type": "step", "text": text, "ms": int((time.time() - t0) * 1000)} | extra
        steps.append(ev)
        return ev

    # 0. Контекст диалога: ответ на уточняющий вопрос продолжает исходный вопрос.
    pending = state.get("pending")
    if pending:
        question = pending["question"]
        department = pending.get("department")
        clar = state.setdefault("clarifications", [])
        if pending["kind"] == "department":
            department = detect_department(message) or next((d for d in pending["options"] if norm(d) in norm(message)), None)
            clar.append(f"Подразделение: {department or message}")
        else:
            clar.append(f"{pending.get('clarify_question', 'Уточнение')} — {message}")
        yield step(f"Продолжаю вопрос с учётом уточнения: «{message}»")
    else:
        question, department, state = message, None, {"clarifications": []}
    department = department or detect_department(question + " " + " ".join(state.get("clarifications", [])))
    full_q = question + ("\nУточнения пользователя: " + "; ".join(state["clarifications"]) if state.get("clarifications") else "")
    n_clar = len(state.get("clarifications", []))

    # 1. Разбор вопроса и план поиска (вызов LLM № 1).
    yield step("Разбираю вопрос и составляю план поиска", tool="plan")
    plan = llm.chat_json(SYS_PLAN, full_q, usage, max_tokens=150, slot=0)
    subqueries = [q for q in plan.get("subqueries", []) if isinstance(q, str)][:3]
    if plan.get("in_scope") is False and not search.kb_search(full_q, department):
        # Вне регламентов и нет разъяснения службы качества → сразу запрос в службу качества.
        yield step("Вопрос вне регламентов, разъяснений службы качества по нему нет", tool="check")
        result = finish_not_found(question, department, user_name, "вопрос вне действующих регламентов", [], [], steps, usage)
        yield step(f"Создан запрос № {result['ticket_id']} в службу качества, письмо на {config.QUALITY_EMAIL}", tool="escalate")
        yield from finalize(dialog_id, {}, question, result, steps, usage, t0, user_name)
        return
    queries = [question] + subqueries
    yield step(f"Подразделение: {department or 'не указано'}; под-запросы: " + " · ".join(subqueries), tool="plan",
               detail={"department": department, "subqueries": subqueries})

    # 2. Гибридный поиск (FTS + вектор) по действующим регламентам + разъяснения службы качества.
    hits = search.hybrid(queries, department, k=config.TOP_K)
    kb = search.kb_search(full_q, department)
    yield step(f"Нашёл {len(hits)} пунктов в {len({h['doc_id'] for h in hits})} регламентах"
               + (f" и {len(kb)} разъяснений службы качества" if kb else ""), tool="search",
               detail=[fragment_label(h) for h in hits])

    # 3. Проверка однозначности по подразделению (кодом): ответ в разных подразделениях разный.
    if not department and n_clar < 2 and hits:
        deps = []
        for h in hits[:8]:
            if h["department"] and h["department"] not in deps and h["score"] >= hits[0]["score"] * 0.6:
                deps.append(h["department"])
        if len(deps) >= 2 and not kb:
            q = "Порядок отличается в разных подразделениях. О каком подразделении ваш вопрос?"
            state["pending"] = {"kind": "department", "question": question, "options": deps}
            yield step(f"Нашёл разные правила в подразделениях: {', '.join(deps)} — нужно уточнение", tool="check")
            result = {"status": "clarify", "answer": q, "clarify_question": q, "options": deps, "citations": [],
                      "sources": [fragment_label(h) for h in hits[:5]]}
            yield from finalize(dialog_id, state, question, result, steps, usage, t0, user_name)
            return

    # 4. Обход графа связей: ссылки на другие пункты, подпункты, разъяснения, пробелы базы.
    g = search.expand([h["id"] for h in hits[:4]])
    seen = {h["id"] for h in hits}
    extra = [r for r in g["linked"] + g["children"] if r["id"] not in seen]
    gaps = g["gaps"]
    yield step(f"Граф связей: +{len(g['linked'])} пунктов по ссылкам, +{len(g['children'])} подпунктов"
               + (f"; ссылки на документы вне базы: {', '.join(x['dst_ref_text'] for x in gaps[:3])}" if gaps else ""),
               tool="graph", detail={"linked": [fragment_label(r) + ' — ' + r['via'] for r in extra],
                                     "gaps": [x["dst_ref_text"] for x in gaps]})

    # 5. Сборка доказательств: пункты (с подпунктами и связанными), разъяснения. Бюджет токенов.
    evidence, budget = {}, config.MAX_EVIDENCE_TOKENS
    for r in hits[:4] + extra[:1]:
        text = r["text"] if len(r["text"]) <= 500 else r["text"][:500] + " …"
        cost = len(text) // 3
        if budget - cost < 0 and evidence:
            continue
        budget -= cost
        evidence[f"F{len(evidence) + 1}"] = r | {"kind": "chunk", "text": text}
    for a in kb + [e for e in g["explains"] if e["id"] not in {k["id"] for k in kb}]:
        evidence[f"K{a['id']}"] = {"kind": "kb", "text": f"Вопрос: {a['question']}\nОтвет службы качества: {a['answer']}",
                                   "title": a.get("title") or "Разъяснение службы качества", "clause_no": a.get("clause_no"),
                                   "department": a.get("department"), "ticket_id": a.get("ticket_id"), "id": a["id"]}
    ctx = "\n\n".join(
        f"[{fid}] {('Разъяснение службы качества (запрос № ' + str(e['ticket_id']) + ')') if e['kind'] == 'kb' else fragment_label(e)}"
        f"{(' · раздел: ' + e['section']) if e.get('section') else ''}\n{e['text']}" for fid, e in evidence.items())

    # 6. Ответ строго по фрагментам (вызов LLM № 2).
    yield step(f"Формулирую ответ по {len(evidence)} фрагментам", tool="answer")
    user = f"Вопрос: {full_q}\n\nФрагменты:\n{ctx}"
    out = llm.chat_json(SYS_ANSWER, user, usage, max_tokens=350, schema=ANSWER_SCHEMA, slot=1)

    # 7. Проверка кодом: цитаты дословно есть во фрагментах, ссылки на существующие фрагменты.
    status = out.get("status", "not_found")
    valid, bad = [], []
    for cit in out.get("citations", []):
        e = evidence.get(str(cit.get("id", "")).strip("[] "))
        quote = cit.get("quote", "")
        if e and len(norm(quote)) >= 8 and norm(quote) in norm(e["text"]):
            valid.append({"id": cit["id"], "quote": quote, "source": fragment_label(e) if e["kind"] == "chunk"
                          else f"Разъяснение службы качества (запрос № {e['ticket_id']})", "doc_id": e.get("doc_id"),
                          "clause_no": e.get("clause_no"), "kind": e["kind"]})
        else:
            bad.append(cit)
    verdict = {"answer": "ответ найден", "clarify": "нужно уточнение", "not_found": "ответа во фрагментах нет"}.get(status, status)
    yield step(f"Модель: {verdict}. Проверил цитаты кодом: подтверждено {len(valid)}, отклонено {len(bad)}", tool="verify",
               detail=[f"отклонена: [{b.get('id')}] {b.get('quote')}" for b in bad] + ([f"модель: {out.get('answer', '')[:300]}"] if status != "answer" else []))

    if status == "answer" and not valid:
        status = "not_found"
        yield step("Ответ не подтверждён дословными цитатами — не даю недостоверный ответ", tool="verify")
    if status == "clarify" and n_clar >= 2:
        status = "not_found"
    if status == "clarify":
        q = out.get("clarify_question") or out.get("answer")
        state["pending"] = {"kind": "llm", "question": question, "department": department, "clarify_question": q,
                            "options": out.get("options", [])}
        result = {"status": "clarify", "answer": q, "clarify_question": q, "options": out.get("options", []),
                  "citations": valid, "sources": [fragment_label(e) for e in evidence.values() if e["kind"] == "chunk"][:4]}
        yield from finalize(dialog_id, state, question, result, steps, usage, t0, user_name)
        return
    if status == "not_found":
        reason = "в регламентах нет подтверждённого ответа"
        result = finish_not_found(full_q, department, user_name, reason, list(evidence.values()), gaps, steps, usage)
        yield step(f"Создан запрос № {result['ticket_id']} в службу качества, письмо на {config.QUALITY_EMAIL}", tool="escalate")
        yield from finalize(dialog_id, {}, question, result, steps, usage, t0, user_name)
        return
    result = {"status": "answer", "answer": out.get("answer", ""), "steps": out.get("steps", []), "citations": valid,
              "gaps": [x["dst_ref_text"] for x in gaps], "used_kb": any(c["kind"] == "kb" for c in valid)}
    yield from finalize(dialog_id, {}, question, result, steps, usage, t0, user_name)


def finish_not_found(question, department, user_name, reason, evidence, gaps, steps, usage) -> dict:
    """Кейс 1: нет ответа в регламентах → запрос в службу качества (письмо), пользователю — куда обратиться."""
    near = [fragment_label(e) for e in evidence if e.get("kind") == "chunk"][:3]
    gap_txt = [x["dst_ref_text"] for x in gaps]
    body = (f"Служба качества, добрый день.\n\nСотрудник {user_name} ({department or 'подразделение не указано'}) задал вопрос, "
            f"ответа на который нет в действующих регламентах:\n\n«{question}»\n\nПричина: {reason}.\n"
            + (f"Ближайшие пункты: {'; '.join(near)}.\n" if near else "")
            + (f"В регламентах есть ссылки на документы, которых нет в базе: {'; '.join(gap_txt)}.\n" if gap_txt else "")
            + "\nПрошу дать ответ — после подтверждения он будет добавлен в базу знаний агента.\n\nИИ-агент по регламентам")
    with db.conn() as c:
        tid = c.execute("""INSERT INTO tickets(user_name,department,question,context,mail_to,mail_body)
                           VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (user_name, department, question, json.dumps({"near": near, "gaps": gap_txt, "reason": reason},
                                                                     ensure_ascii=False), config.QUALITY_EMAIL, body)).fetchone()["id"]
    send_mail(config.QUALITY_EMAIL, f"Запрос № {tid}: вопрос без ответа в регламентах", body)
    ans = (f"В действующих регламентах нет однозначного ответа на этот вопрос, поэтому я не буду отвечать от себя. "
           f"Я создал запрос № {tid} в службу качества и отправил письмо на {config.QUALITY_EMAIL}. "
           f"Ответ придёт в раздел «Мои запросы»; после подтверждения службой качества он появится в базе знаний.")
    if gap_txt:
        ans += f" Возможно, ответ есть в документе, которого нет в базе: {gap_txt[0]}."
    return {"status": "escalated", "answer": ans, "ticket_id": tid, "near": near, "gaps": gap_txt, "citations": []}


def send_mail(to, subject, body):
    """Демо: письмо пишется в журнал (таблица tickets.mail_body). При заданном SMTP_HOST — реальная отправка."""
    if not config.SMTP_HOST:
        return
    import smtplib
    from email.message import EmailMessage
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "agent@company.local", to, subject
    m.set_content(body)
    with smtplib.SMTP(config.SMTP_HOST) as s:
        s.send_message(m)


def finalize(dialog_id, state, question, result, steps, usage, t0, user_name):
    save_dialog(dialog_id, state)
    latency = int((time.time() - t0) * 1000)
    outcome = {"answer": "answered", "clarify": "clarify", "escalated": "escalated"}.get(result["status"], result["status"])
    with db.conn() as c:
        qid = c.execute("""INSERT INTO query_log(dialog_id,user_name,question,outcome,steps,citations,answer,tokens_in,tokens_out,
                           llm_calls,latency_ms) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (dialog_id, user_name, question, outcome, json.dumps(steps, ensure_ascii=False, default=str),
                         json.dumps(result.get("citations", []), ensure_ascii=False), result.get("answer"),
                         usage.tokens_in, usage.tokens_out, usage.calls, latency)).fetchone()["id"]
    yield {"type": "result", "dialog_id": dialog_id, "query_id": qid, "latency_ms": latency} | usage.as_dict() | result
