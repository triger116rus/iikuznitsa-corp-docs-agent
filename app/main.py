"""Веб-приложение: чат с агентом (поток шагов NDJSON), служба качества (запросы → ответ → база знаний),
реестр документов, граф связей, аналитика."""
import json
import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import iterate_in_threadpool

from . import agent, config, db, kb, mailbox

@asynccontextmanager
async def lifespan(_app):
    mailbox.start_poller()  # фоновый опрос почты службы качества (если настроен IMAP)
    yield


app = FastAPI(title="ИИ-агент по корпоративным регламентам", lifespan=lifespan)
STATIC = pathlib.Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class Ask(BaseModel):
    message: str
    dialog_id: str | None = None
    user_name: str = "Сотрудник"
    user_email: str | None = None


class TicketAnswer(BaseModel):
    answer: str
    answered_by: str = "Служба качества"
    doc_id: int | None = None
    clause_no: str | None = None
    add_to_kb: bool = True


class Report(BaseModel):
    text: str
    query_id: int | None = None
    user_name: str = "Сотрудник"


class Feedback(BaseModel):
    query_id: int
    value: str


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
def healthz():
    with db.conn() as c:
        n = c.execute("SELECT count(*) n FROM chunks").fetchone()["n"]
    return {"ok": True, "chunks": n}


@app.post("/api/ask")
async def ask(req: Ask):
    def gen():
        try:
            for ev in agent.run(req.message.strip(), req.dialog_id, req.user_name, (req.user_email or "").strip() or None):
                yield json.dumps(ev, ensure_ascii=False, default=str) + "\n"
        except Exception as e:  # ошибка — тоже событие, интерфейс покажет её
            yield json.dumps({"type": "error", "text": f"{type(e).__name__}: {e}"}, ensure_ascii=False) + "\n"

    return StreamingResponse(iterate_in_threadpool(gen()), media_type="application/x-ndjson")


@app.get("/api/tickets")
def tickets(user_name: str | None = None):
    with db.conn() as c:
        # Контроль срока: у каждого письма в службу качества есть срок ответа; просроченные видны всем в интерфейсе.
        return c.execute("""SELECT id, created_at, user_name, department, question, context, status, answer, answered_by,
                                   answered_at, mail_to, mail_body,
                                   created_at + make_interval(hours => %(h)s) AS due_at,
                                   (status = 'open' AND now() > created_at + make_interval(hours => %(h)s)) AS overdue
                            FROM tickets
                            WHERE %(u)s::text IS NULL OR user_name=%(u)s ORDER BY id DESC LIMIT 100""",
                         {"u": user_name, "h": config.ANSWER_SLA_HOURS}).fetchall()


@app.post("/api/tickets/{tid}/answer")
def answer_ticket(tid: int, req: TicketAnswer):
    """Запасной путь кейса 3 (основной — ответ письмом, см. mailbox.py): ответ службы качества → база разъяснений."""
    try:
        kb_id = kb.add_answer(tid, req.answer, req.answered_by, req.doc_id, req.clause_no, req.add_to_kb)
    except KeyError:
        raise HTTPException(404, "нет такого запроса")
    return {"ok": True, "kb_answer_id": kb_id}


@app.post("/api/mail/poll")
def mail_poll():
    """Забрать ответы службы качества из почты сейчас (иначе — фоновый опрос раз в 30 с)."""
    return {"processed": mailbox.poll_once()}


@app.get("/api/kb")
def kb_list():
    with db.conn() as c:
        return c.execute("""SELECT a.id, a.ticket_id, a.question, a.answer, a.clause_no, a.department, a.approved_by,
                                   a.created_at, d.title FROM kb_answers a LEFT JOIN documents d ON d.id=a.doc_id
                            ORDER BY a.id DESC""").fetchall()


@app.get("/api/docs")
def docs():
    with db.conn() as c:
        return c.execute("""SELECT d.id, d.title, d.department, d.process, d.status, d.file_name,
                                   (SELECT count(*) FROM chunks WHERE doc_id=d.id) AS clauses,
                                   (SELECT count(*) FROM doc_links WHERE src_doc=d.id AND kind='references') AS refs,
                                   (SELECT count(*) FROM doc_links WHERE src_doc=d.id AND kind='references' AND dst_node IS NOT NULL) AS dangling
                            FROM documents d ORDER BY d.department, d.title""").fetchall()


@app.get("/api/docs/{doc_id}")
def doc(doc_id: int):
    with db.conn() as c:
        return c.execute("SELECT id, clause_no, section, text FROM chunks WHERE doc_id=%s ORDER BY ord", (doc_id,)).fetchall()


@app.get("/api/graph")
def graph():
    """Граф для вкладки «Граф»: документы, роли, внешние (отсутствующие) документы и рёбра между ними."""
    with db.conn() as c:
        docs_ = c.execute("SELECT id, title, department FROM documents").fetchall()
        edges = c.execute("""
            SELECT l.src_doc, l.kind, l.dst_doc, l.dst_node, min(l.dst_ref_text) AS label, count(*) AS n
            FROM doc_links l WHERE l.src_doc IS NOT NULL GROUP BY 1,2,3,4""").fetchall()
        gaps = c.execute("""
            SELECT l.dst_node, min(l.dst_ref_text) AS label, count(*) AS n, array_agg(DISTINCT d.title) AS docs
            FROM doc_links l JOIN documents d ON d.id=l.src_doc WHERE l.dst_node LIKE 'doc:%%'
            GROUP BY 1 ORDER BY 3 DESC""").fetchall()
    return {"docs": docs_, "edges": edges, "gaps": gaps}


@app.get("/api/stats")
def stats():
    with db.conn() as c:
        s = c.execute("""SELECT count(*) AS queries,
                                count(*) FILTER (WHERE outcome='answered') AS answered,
                                count(*) FILTER (WHERE outcome='clarify') AS clarify,
                                count(*) FILTER (WHERE outcome='escalated') AS escalated,
                                count(*) FILTER (WHERE outcome='out_of_scope') AS out_of_scope,
                                round(avg(latency_ms)) AS avg_ms, round(avg(tokens_in)) AS avg_in,
                                round(avg(tokens_out)) AS avg_out, round(avg(llm_calls),1) AS avg_calls
                         FROM query_log""").fetchone()
        last = c.execute("""SELECT id, ts, user_name, question, outcome, latency_ms, tokens_in, tokens_out, llm_calls, feedback
                            FROM query_log ORDER BY id DESC LIMIT 30""").fetchall()
        tk = c.execute("""SELECT count(*) FILTER (WHERE status='open') AS open, count(*) AS total,
                               count(*) FILTER (WHERE status='open' AND now() > created_at + make_interval(hours => %s)) AS overdue
                        FROM tickets""", (config.ANSWER_SLA_HOURS,)).fetchone()
        # Разъяснения, которые агент использует чаще всего, — кандидаты на внесение в регламент (инициатива бота).
        hot = c.execute("""SELECT a.id, a.question, a.answer, a.clause_no, d.title, count(*) AS uses
                            FROM query_log q, jsonb_array_elements(q.citations) cit
                            JOIN kb_answers a ON 'K' || a.id = cit->>'id' LEFT JOIN documents d ON d.id=a.doc_id
                            WHERE q.outcome='answered' GROUP BY 1,2,3,4,5 ORDER BY uses DESC LIMIT 10""").fetchall()
        for h in hot:
            h["proposal"] = proposal(c, h)
    return {"summary": s, "last": last, "tickets": tk, "hot": hot}


# Порог «разъяснение относится к этому разделу» выше порога поиска: замер 26.11 — несвязанные разделы
# дают 0,59–0,62 (отпуск → «Согласование сметы», командировка → «Формирование команды»).
PROPOSAL_MIN = 0.70


def proposal(c, h) -> str:
    """Предложение службе качества: куда внести разъяснение. Собирается кодом по шаблону из уже утверждённого
    ответа (без вызова LLM): регламент, к которому привязано разъяснение, иначе — ближайший по смыслу раздел
    действующего регламента; если близкого раздела нет — нужен новый пункт в профильном регламенте."""
    text = f"«{h['answer']}»"
    if h["title"]:
        return f"Внести в «{h['title']}»" + (f", п. {h['clause_no']}" if h["clause_no"] else "") + f": {text}"
    near = c.execute("""SELECT d.title, ch.section, 1 - (ch.embedding <=> a.embedding) AS sim
                        FROM kb_answers a, chunks ch JOIN documents d ON d.id = ch.doc_id
                        WHERE a.id = %s AND d.status = 'in_force'
                        ORDER BY ch.embedding <=> a.embedding LIMIT 1""", (h["id"],)).fetchone()
    if near and near["sim"] >= PROPOSAL_MIN:
        return f"Внести в «{near['title']}», раздел «{near['section']}»: {text}"
    return f"Регламента по этой теме в базе нет — добавить пункт в профильный регламент: {text}"


@app.post("/api/report")
def report(req: Report):
    """Сотрудник не меняет регламенты, но может сообщить о несоответствии/противоречии → запрос в службу качества."""
    with db.conn() as c:
        q = c.execute("SELECT question, answer, citations FROM query_log WHERE id=%s", (req.query_id,)).fetchone() if req.query_id else None
        body = f"Сотрудник {req.user_name} сообщает о несоответствии в регламентах:\n\n{req.text}\n"
        if q:
            body += (f"\nВопрос: {q['question']}\nОтвет агента: {q['answer']}\nИсточники: "
                     + "; ".join(c_["source"] for c_ in (q["citations"] or [])))
        tid = c.execute("""INSERT INTO tickets(user_name,question,context,mail_to,mail_body)
                           VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                        (req.user_name, "Несоответствие: " + req.text, json.dumps({"type": "inconsistency", "query_id": req.query_id}),
                         agent.config.QUALITY_EMAIL, body)).fetchone()["id"]
    return {"ok": True, "ticket_id": tid}


@app.post("/api/feedback")
def feedback(req: Feedback):
    with db.conn() as c:
        c.execute("UPDATE query_log SET feedback=%s WHERE id=%s", (req.value, req.query_id))
    return {"ok": True}
