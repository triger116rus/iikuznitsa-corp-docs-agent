"""Агентный цикл: разбор вопроса → план поиска → поиск → проверка однозначности → обход графа →
ответ строго по фрагментам → проверка цитат кодом → ответ / уточняющий вопрос / письмо в службу качества.

run() — генератор событий для интерфейса: {"type": "step", ...} по ходу работы и финальный {"type": "result", ...}.
Требования заказчика (брифинг 26.11): не галлюцинировать; уточнять подразделение; не больше 2–3 уточнений;
не знает подразделение — все варианты с источниками; спорный случай — ответ с оговоркой; нет ответа — письмо
в службу качества с сотрудником в копии; вопрос не о регламентах — «обратитесь к руководителю».
"""
import json
import re
import time
import uuid

from . import config, db, llm, mailbox, search
from .ingest import ROLES, ROLE_SYNONYMS
from .glossary import TERMS

GLOSSARY = "; ".join(f"{k} — {v}" for k, v in ROLES.items())   # постоянная часть промпта (кэшируется)


def terms_in(text: str) -> str:
    """Расшифровки из глоссария заказчика только для сокращений, которые есть в найденных пунктах и вопросе."""
    found = [f"{a} — {n}" for a, n in TERMS.items()
             if a not in ROLES and re.search(rf"(?<![А-ЯЁA-Z]){re.escape(a)}(?![А-ЯЁA-Z])", text)]
    return ("\nСокращения во фрагментах (глоссарий): " + "; ".join(found)) if found else ""


MAX_CLARIFY = 2
RELEVANCE_MIN = 0.56   # ниже — в регламентах нет близких пунктов (замер 26.11: ответы 0.57+, «отпуск» 0.49)
AMBIGUITY_GAP = 0.05  # подразделения с близостью в пределах 0.05 от лучшей — ответ неоднозначен

DEP_ALIASES = {
    "Дирекция ИБ": r"\bИБ\b|\bДИБ\b|информационн\w* безопасност|дирекци\w* иб",
    "ДБРЦТ": r"ДБРЦТ|бизнес-решени|цифров\w* трансформац",
    "Технологическая дирекция": r"тех\.?\s*дирекц|технологическ\w* дирекц|технической дирекц",
}
ALL_VARIANTS = "Не знаю — покажите все варианты"
ALL_RE = re.compile(r"не знаю|все вариант|несколько|комплексн|обе|оба|все подразделени", re.I)

SYS_PLAN = """Ты планируешь поиск по внутренним регламентам компании (пресейл и проектная деятельность подразделений).
Верни только JSON: {"in_scope": true|false, "subqueries": ["...", "..."]}.
in_scope=false — вопрос НЕ о правилах и процессах компании: личная премия, зарплата, бонус за сделку, курсы валют,
погода, новости, общие знания, болтовня. Любой вопрос о правилах и процессах компании (проекты, пресейл, договоры,
отпуск, командировки, документы), даже если ответа в регламентах может не быть, — in_scope=true.
subqueries — 1–3 короткие поисковые фразы на русском, переформулирующие вопрос терминами регламентов
(роли: SS, АМ, ПА, РП, ГИП, Координатор департамента; объекты: карточка проекта, CRM, расчёт, ТКП, договор).
Ничего не добавляй от себя."""

SYS_ANSWER = f"""Ты отвечаешь сотруднику компании СТРОГО по фрагментам регламентов и разъяснениям службы качества ниже.
Правила:
1. Используй только факты из фрагментов. Ничего не добавляй из общих знаний, не обобщай, не додумывай.
2. Если во фрагментах ответа на вопрос нет — status="not_found".
3. Если ответ зависит от условия, которого нет в вопросе (несколько разных сценариев в регламенте),
   и по вопросу нельзя выбрать один — status="clarify": задай ОДИН уточняющий вопрос и дай 2–4 варианта ответа.
4. Иначе status="answer": ответ простым человеческим языком, без канцелярита и без искажения смысла, 1–3 предложения;
   если уместно — шаги «что сделать». caveat — пустая строка, если на заданный вопрос ответ полный.
   Только если фрагменты отвечают на САМ вопрос частично или регламенты противоречат друг другу — ответь по ним
   и в caveat одной фразой напиши оговорку: какой части ответа в регламенте нет. О том, что не спрашивали, не пиши.
5. Сначала выпиши цитаты из фрагментов, затем ответ, затем status. Каждое утверждение подтверди цитатой:
   id фрагмента и ДОСЛОВНАЯ фраза из него (5–12 слов, без изменений), 1–3 цитаты.
   Цитируй только фрагменты, которые прямо отвечают на вопрос. Числа, суммы, сроки и пороги из вопроса, которых
   нет во фрагментах, не подтверждай: ответь тем, что есть в регламенте, и в caveat скажи, что такого условия там нет.
6. Сотрудники часто спрашивают с неверной посылкой (не та роль, не тот срок, не тот этап). Не соглашайся с ней:
   ответь, как написано в регламенте, и мягко поправь («Это делает не АМ, а SS: …»). На вопрос «кто делает X» отвечай по фрагменту,
   где назван исполнитель именно действия X (тот, кто X выполняет), а не фрагменту, где X лишь упоминается.
Сокращения в регламентах: {GLOSSARY}. Вопрос и фрагмент могут называть роль полностью или сокращением — это одно и то же.
Верни только JSON."""

ANSWER_SCHEMA = {
    # Порядок полей = порядок генерации: сначала дословные цитаты, затем ответ, решение (status) — последним.
    "type": "object",
    "properties": {
        "citations": {"type": "array", "maxItems": 4, "items": {
            "type": "object", "properties": {"id": {"type": "string"}, "quote": {"type": "string"}},
            "required": ["id", "quote"]}},
        "answer": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        "caveat": {"type": "string"},
        "clarify_question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        "status": {"type": "string", "enum": ["answer", "clarify", "not_found"]},
    },
    "required": ["citations", "answer", "status"],
}

PARTIAL_ANSWER = re.compile(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)')

OUT_OF_SCOPE = ("Этот вопрос не касается корпоративной базы регламентов, поэтому ответ на него я не даю. "
                "Обратитесь, пожалуйста, к своему руководителю.")


ROLE_STEMS = {abbr: name.lower()[:-2] for abbr, name in ROLES.items()}  # «руководитель проекта» → «руководитель проек»


def with_abbreviations(text: str) -> str | None:
    """Вариант вопроса с сокращениями ролей, как они записаны в регламентах (РП, SS, ГИП…). None — если замен нет."""
    out = text
    for abbr, name in ROLES.items():
        words = name.lower().split()
        pat = r"(?<![а-яё])" + r"\s+".join(w[:max(4, len(w) - 2)] + r"[а-яё-]*" for w in words)
        out = re.sub(pat, abbr, out, flags=re.I)
    for abbr, name in sorted(TERMS.items(), key=lambda t: -len(t[1])):   # длинные названия первыми: ТКП раньше КП
        if abbr in ROLES or len(name) < 6:
            continue
        words = re.sub(r"\(.*?\)", "", name).lower().split()
        if words:
            pat = r"(?<![а-яё])" + r"\s+".join(w[:max(4, len(w) - 2)] + r"[а-яё-]*" for w in words)
            out = re.sub(pat, abbr, out, flags=re.I)
    for abbr, syns in ROLE_SYNONYMS.items():   # проджект-менеджер, project manager, PM → РП
        for syn in sorted(syns, key=len, reverse=True):
            out = re.sub(rf"(?<![\w-]){re.escape(syn)}[а-яё]*(?![\w-])", abbr, out, flags=re.I)
    return out if out != text else None


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


def run(message: str, dialog_id: str | None = None, user_name: str = "Сотрудник", user_email: str | None = None):
    t0 = time.time()
    usage = llm.Usage()
    steps = []
    dialog_id = dialog_id or uuid.uuid4().hex[:12]
    state = load_dialog(dialog_id)

    def step(text, **extra):
        ev = {"type": "step", "text": text, "ms": int((time.time() - t0) * 1000)} | extra
        steps.append(ev)
        return ev

    def done(result, keep_state=None):
        return finalize(dialog_id, keep_state or {}, question, result, steps, usage, t0, user_name)

    # 0. Контекст диалога: ответ на уточняющий вопрос продолжает исходный вопрос.
    pending = state.get("pending")
    all_variants = False
    if pending:
        question = pending["question"]
        department = pending.get("department")
        clar = state.setdefault("clarifications", [])
        if pending["kind"] == "department":
            department = detect_department(message) or next((d for d in pending["options"] if norm(d) in norm(message)), None)
            if not department and ALL_RE.search(message):
                all_variants = True
                state["variants"] = pending["options"]
            clar.append(f"Подразделение: {department or ('все варианты' if all_variants else message)}")
        else:
            clar.append(f"{pending.get('clarify_question', 'Уточнение')} — {message}")
        state.pop("pending", None)
        yield step(f"Продолжаю вопрос с учётом уточнения: «{message}»")
    else:
        question, department, state = message, None, {"clarifications": []}
    department = department or detect_department(question + " " + " ".join(state.get("clarifications", [])))
    full_q = question + ("\nУточнения пользователя: " + "; ".join(state["clarifications"]) if state.get("clarifications") else "")
    n_clar = len(state.get("clarifications", []))

    # 0б. Мгновенно (1–2 с, без LLM): где в регламентах искать ответ — ссылки на пункты.
    #     Заказчику важно: часто сотруднику нужно просто найти регламент и пункт.
    quick = search.hybrid([full_q], department, k=4)
    if quick:
        yield {"type": "sources", "ms": int((time.time() - t0) * 1000),
               "items": [{"label": fragment_label(h), "doc_id": h["doc_id"], "clause_no": h["clause_no"],
                          "snippet": h["text"][:160]} for h in quick]}

    # 1. Разбор вопроса и план поиска (вызов LLM № 1).
    yield step("Разбираю вопрос и составляю план поиска", tool="plan")
    plan = llm.chat_json(SYS_PLAN, full_q, usage, max_tokens=150, slot=0)
    subqueries = [q for q in plan.get("subqueries", []) if isinstance(q, str)][:3]
    # «Вне регламентов» — только если и модель так считает, и в регламентах нет близких пунктов
    # (страховка от ошибки модели: «арбитраж при низкой маржинальности» — рабочий процесс, а не болтовня).
    if (plan.get("in_scope") is False and not search.kb_search(full_q, department)
            and max(search.department_scores([question]).values() or [0]) < RELEVANCE_MIN):
        yield step("Вопрос не о правилах и процессах компании — регламенты к нему не относятся", tool="check")
        yield from done({"status": "out_of_scope", "answer": OUT_OF_SCOPE, "citations": []})
        return
    queries = [question] + subqueries + [q for q in [with_abbreviations(question)] if q]
    yield step(f"Подразделение: {department or ('все варианты' if all_variants else 'не указано')}; под-запросы: "
               + " · ".join(subqueries), tool="plan", detail={"department": department, "subqueries": subqueries})

    # 2. Гибридный поиск (FTS + вектор) по действующим регламентам + разъяснения службы качества.
    if all_variants:
        hits = []
        for dep in state.get("variants", []):
            hits += search.hybrid(queries, dep, k=2)
    else:
        hits = search.hybrid(queries, department, k=config.TOP_K)
    kb = search.kb_search(full_q, department)
    yield step(f"Нашёл {len(hits)} пунктов в {len({h['doc_id'] for h in hits})} регламентах"
               + (f" и {len(kb)} разъяснений службы качества" if kb else ""), tool="search",
               detail=[fragment_label(h) for h in hits])

    # 2б. Нет даже близких пунктов и разъяснений → сразу письмо в службу качества (без долгого вызова LLM).
    scores = search.department_scores([question] + subqueries[:1])
    top = max(scores.values()) if scores else 0
    if top < RELEVANCE_MIN and not kb:
        yield step(f"Близких пунктов в регламентах нет (лучшая близость {top:.2f} < {RELEVANCE_MIN}) — "
                   "не отвечаю от себя", tool="check")
        result = escalate(full_q, department, user_name, user_email, [])
        yield step(result["step"], tool="escalate")
        yield from done(result)
        return

    # 3. Проверка однозначности по подразделению (кодом): в разных подразделениях порядок разный.
    if not department and not all_variants and n_clar < MAX_CLARIFY and hits and not kb:
        deps = [d for d, v in sorted(scores.items(), key=lambda x: -x[1]) if v >= top - AMBIGUITY_GAP and v >= 0.5]
        yield step("Близость вопроса к регламентам подразделений: "
                   + ", ".join(f"{d} {v:.2f}" for d, v in sorted(scores.items(), key=lambda x: -x[1])), tool="check")
        if len(deps) >= 2:
            q = "Порядок отличается в разных подразделениях. О каком подразделении ваш вопрос?"
            state["pending"] = {"kind": "department", "question": question, "options": deps}
            yield step(f"Нашёл разные правила в подразделениях: {', '.join(deps)} — нужно уточнение", tool="check")
            yield from done({"status": "clarify", "answer": q, "clarify_question": q, "options": deps + [ALL_VARIANTS],
                             "citations": [], "sources": [fragment_label(h) for h in hits[:5]]}, state)
            return

    # 4. Обход графа связей: ссылки на другие пункты, подпункты, разъяснения, документы вне базы.
    g = search.expand([h["id"] for h in hits[:4]])
    seen = {h["id"] for h in hits}
    extra = [r for r in g["linked"] + g["children"] if r["id"] not in seen]
    gaps = g["gaps"]
    yield step(f"Граф связей: +{len(g['linked'])} пунктов по ссылкам, +{len(g['children'])} подпунктов"
               + (f", разъяснений к пунктам: {len(g['explains'])}" if g["explains"] else "")
               + (f"; ссылки на документы вне базы: {', '.join(x['dst_ref_text'] for x in gaps[:3])}" if gaps else ""),
               tool="graph", detail={"linked": [fragment_label(r) + ' — ' + r['via'] for r in extra],
                                     "gaps": [x["dst_ref_text"] for x in gaps]})

    # 5. Сборка доказательств: пункты (с подпунктами и связанными), разъяснения. Бюджет токенов.
    evidence, budget = {}, config.MAX_EVIDENCE_TOKENS + (400 if all_variants else 0)
    for r in (hits[:6] if all_variants else hits[:4] + extra[:1]):
        text = r["text"] if len(r["text"]) <= 500 else r["text"][:500] + " …"
        cost = len(text) // 3
        if budget - cost < 0 and evidence:
            continue
        budget -= cost
        evidence[f"F{len(evidence) + 1}"] = r | {"kind": "chunk", "text": text}
    for a in kb + [e for e in g["explains"] if e["id"] not in {k["id"] for k in kb}]:
        evidence[f"K{a['id']}"] = {"kind": "kb", "text": f"Вопрос: {a['question']}\nОтвет службы качества: {a['answer']}",
                                   "title": a.get("title") or "Разъяснение службы качества", "clause_no": a.get("clause_no"),
                                   "department": a.get("department"), "ticket_id": a.get("ticket_id"), "id": a["id"],
                                   "doc_id": a.get("doc_id")}
    ctx = "\n\n".join(
        f"[{fid}] {('Разъяснение службы качества (запрос № ' + str(e['ticket_id']) + ')') if e['kind'] == 'kb' else fragment_label(e)}"
        f"{(' · раздел: ' + e['section']) if e.get('section') else ''}\n{e['text']}" for fid, e in evidence.items())

    # 6. Ответ строго по фрагментам (вызов LLM № 2).
    yield step(f"Формулирую ответ по {len(evidence)} фрагментам", tool="answer")
    task = ("\nСотрудник не знает подразделение: дай ответ ОТДЕЛЬНО для каждого подразделения из фрагментов "
            "(«В <подразделение>: …»), каждое — со своей цитатой. Уточняющих вопросов не задавай." if all_variants else "")
    if n_clar >= MAX_CLARIFY:
        task += "\nЛимит уточнений исчерпан: уточняющих вопросов больше не задавай (status только answer или not_found)."
    out, shown = {}, ""
    for kind, val in llm.stream_json(SYS_ANSWER, f"Вопрос: {full_q}{task}{terms_in(full_q + ' ' + ctx)}\n\nФрагменты:\n{ctx}",
                                     usage, max_tokens=400, schema=ANSWER_SCHEMA, slot=1):
        if kind == "done":
            out = val
            continue
        m = PARTIAL_ANSWER.search(val)
        if m and len(m.group(1)) - len(shown) >= 12:  # текст ответа — по мере генерации
            shown = m.group(1)
            yield {"type": "partial", "answer": shown.replace("\\n", " ").replace('\\"', '"')}

    # 7. Проверка кодом: цитаты дословно есть во фрагментах, ссылки на существующие фрагменты.
    status = out.get("status", "not_found")
    valid, bad = [], []
    for cit in out.get("citations", []):
        e = evidence.get(str(cit.get("id", "")).strip("[] "))
        quote = cit.get("quote", "")
        if e and len(norm(quote)) >= 8 and norm(quote) in norm(e["text"]):
            valid.append({"id": cit["id"], "quote": quote, "source": fragment_label(e) if e["kind"] == "chunk"
                          else f"Практика применения: разъяснение службы качества (запрос № {e['ticket_id']})",
                          "doc_id": e.get("doc_id"), "clause_no": e.get("clause_no"), "kind": e["kind"]})
        else:
            bad.append(cit)
    verdict = {"answer": "ответ найден", "clarify": "нужно уточнение", "not_found": "ответа во фрагментах нет"}.get(status, status)
    yield step(f"Модель: {verdict}. Проверил цитаты кодом: подтверждено {len(valid)}, отклонено {len(bad)}", tool="verify",
               detail=[f"отклонена: [{b.get('id')}] {b.get('quote')}" for b in bad])

    # Числа в ответе (суммы, сроки, проценты) должны быть в цитируемых пунктах — иначе оговорка кодом.
    if status == "answer" and valid:
        cited_text = " ".join(evidence[str(c["id"]).strip("[] ")]["text"] for c in valid)
        clause_nos = {str(e.get("clause_no")) for e in evidence.values()}
        missing = [n for n in dict.fromkeys(re.findall(r"\d+(?:[.,]\d+)?", out.get("answer", "")))
                   if n not in clause_nos and n not in cited_text and n.replace(",", ".") not in cited_text]
        if missing:
            note = "В регламенте не указано: " + ", ".join(missing) + " — это условие из вопроса, регламент его не подтверждает."
            out["caveat"] = (out.get("caveat", "") + " " + note).strip()
            yield step(f"Проверка чисел кодом: в цитатах нет {', '.join(missing)} — добавил оговорку", tool="verify")
    if status == "answer" and not valid:
        status = "not_found"
        yield step("Ответ не подтверждён дословными цитатами — недостоверный ответ не показываю", tool="verify")
    if status == "clarify" and n_clar >= MAX_CLARIFY:
        status = "not_found"
    if status == "clarify":
        q = out.get("clarify_question") or out.get("answer")
        state["pending"] = {"kind": "llm", "question": question, "department": department, "clarify_question": q,
                            "options": out.get("options", [])}
        yield from done({"status": "clarify", "answer": q, "clarify_question": q, "options": out.get("options", []),
                         "citations": valid}, state)
        return
    if status == "not_found":
        result = escalate(full_q, department, user_name, user_email, gaps)
        yield step(result["step"], tool="escalate")
        yield from done(result)
        return
    gap_names = [x["dst_ref_text"] for x in gaps]
    result = {"status": "answer", "answer": out.get("answer", ""), "steps": out.get("steps", []),
              "caveat": out.get("caveat", "").strip(), "citations": valid, "gaps": gap_names,
              "used_kb": any(c["kind"] == "kb" for c in valid)}
    # Ответ с оговоркой (неполный/спорный) → дополнительно письмо в службу качества: заказчик — «сотрудник не должен
    # остаться без ответа: если ответ неоднозначен, в любом случае отправляем в службу качества».
    if result["caveat"] and not result["used_kb"]:
        esc = escalate(full_q + f"\n(Агент ответил частично, оговорка: {result['caveat']})", department, user_name, user_email, gaps)
        result["also_escalated"] = True
        result["ticket_id"] = esc["ticket_id"]
        result["caveat"] += (f" Я также отправил вопрос в службу качества ({config.QUALITY_EMAIL})"
                             + (f", вы в копии" if user_email else "") + " — точный ответ придёт на почту.")
        yield step(esc["step"] + " (ответ с оговоркой)", tool="escalate")
    yield from done(result)


def escalate(question, department, user_name, user_email, gaps) -> dict:
    """Кейс 1: ответа в регламентах нет → письмо в службу качества, сотрудник в копии (как просил заказчик).
    Запрос сохраняется у агента: ответ службы качества (письмом) пополнит базу разъяснений — кейс 3."""
    gap_txt = [x["dst_ref_text"] for x in gaps]
    with db.conn() as c:
        tid = c.execute("""INSERT INTO tickets(user_name,department,question,context,mail_to)
                           VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                        (user_name, department, question, json.dumps({"gaps": gap_txt, "user_email": user_email},
                                                                     ensure_ascii=False), config.QUALITY_EMAIL)).fetchone()["id"]
        subject = mailbox.subject_for(tid, question)
        body = (f"Здравствуйте!\n\nСотрудник {user_name}"
                + (f" ({department})" if department else "") + " задал вопрос ИИ-агенту по регламентам:\n\n"
                f"«{question}»\n\nВ действующих регламентах ответа нет, поэтому агент не стал отвечать от себя.\n"
                + (f"Возможно, ответ содержится в документе, которого нет в базе агента: {'; '.join(gap_txt)}.\n" if gap_txt else "")
                + "\nПожалуйста, ответьте на это письмо всем адресатам: сотрудник получит ответ, "
                  "а агент добавит его в базу разъяснений и будет использовать для похожих вопросов.\n\n"
                  "ИИ-агент по регламентам")
        try:
            sent = mailbox.send(config.QUALITY_EMAIL, subject, body, cc=user_email)
        except Exception as e:
            sent = False
            body += f"\n\n[не отправлено: {type(e).__name__}]"
        c.execute("UPDATE tickets SET mail_body=%s, context=context || %s WHERE id=%s",
                  (f"Тема: {subject}\nКому: {config.QUALITY_EMAIL}" + (f"\nКопия: {user_email}" if user_email else "")
                   + f"\n\n{body}", json.dumps({"sent": sent}), tid))
    who = f" и поставил вас в копию ({user_email})" if user_email else ""
    ans = (f"В действующих регламентах нет ответа на этот вопрос, поэтому я не буду отвечать от себя. "
           f"Я отправил ваш вопрос в службу качества ({config.QUALITY_EMAIL}){who} — ответ придёт на почту. "
           "Когда служба качества ответит, ответ попадёт в базу знаний, и на такой вопрос я смогу отвечать сам.")
    if gap_txt:
        ans += f" Возможно, ответ есть в документе, которого нет в моей базе: {gap_txt[0]}."
    if not user_email:
        ans += " Укажите свой e-mail в панели справа, чтобы получать копию."
    step = (f"Письмо в службу качества {config.QUALITY_EMAIL}" + (f", копия {user_email}" if user_email else "")
            + (" — отправлено" if sent else " — сформировано (почта не настроена)"))
    return {"status": "escalated", "answer": ans, "ticket_id": tid, "gaps": gap_txt, "citations": [], "mail_sent": sent,
            "step": step}


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
    result.pop("step", None)
    yield {"type": "result", "dialog_id": dialog_id, "query_id": qid, "latency_ms": latency} | usage.as_dict() | result
