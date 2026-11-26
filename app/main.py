"""Веб-приложение: чат с агентом (поток шагов NDJSON), служба качества (запросы → ответ → база знаний),
реестр документов, граф связей, аналитика."""
import json
import pathlib

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import iterate_in_threadpool

from . import agent, db, llm

app = FastAPI(title="ИИ-агент по корпоративным регламентам")
STATIC = pathlib.Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class Ask(BaseModel):
    message: str
    dialog_id: str | None = None
    user_name: str = "Сотрудник"


class TicketAnswer(BaseModel):
    answer: str
    answered_by: str = "Служба качества"
    doc_id: int | None = None
    clause_no: str | None = None
    add_to_kb: bool = True


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
            for ev in agent.run(req.message.strip(), req.dialog_id, req.user_name):
                yield json.dumps(ev, ensure_ascii=False, default=str) + "\n"
        except Exception as e:  # ошибка — тоже событие, интерфейс покажет её
            yield json.dumps({"type": "error", "text": f"{type(e).__name__}: {e}"}, ensure_ascii=False) + "\n"

    return StreamingResponse(iterate_in_threadpool(gen()), media_type="application/x-ndjson")


@app.get("/api/tickets")
def tickets(user_name: str | None = None):
    with db.conn() as c:
        return c.execute("""SELECT id, created_at, user_name, department, question, context, status, answer, answered_by,
                                   answered_at, mail_to, mail_body FROM tickets
                            WHERE %(u)s::text IS NULL OR user_name=%(u)s ORDER BY id DESC LIMIT 100""",
                         {"u": user_name}).fetchall()


@app.post("/api/tickets/{tid}/answer")
def answer_ticket(tid: int, req: TicketAnswer):
    """Кейс 3: служба качества отвечает → ответ уходит пользователю и пополняет базу знаний агента
    (разъяснение с эмбеддингом + ребро графа explains к пункту регламента)."""
    with db.conn() as c:
        t = c.execute("SELECT * FROM tickets WHERE id=%s", (tid,)).fetchone()
        if not t:
            raise HTTPException(404, "нет такого запроса")
        c.execute("UPDATE tickets SET status='answered', answer=%s, answered_by=%s, answered_at=now() WHERE id=%s",
                  (req.answer, req.answered_by, tid))
        kb_id = None
        if req.add_to_kb:
            v = llm.embed([f"{t['question']}\n{req.answer}"])[0]
            dep = t["department"]
            if req.doc_id:
                dep = c.execute("SELECT department FROM documents WHERE id=%s", (req.doc_id,)).fetchone()["department"]
            kb_id = c.execute("""INSERT INTO kb_answers(ticket_id,question,answer,doc_id,clause_no,department,approved_by,embedding)
                                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                              (tid, t["question"], req.answer, req.doc_id, req.clause_no, dep, req.answered_by, v)).fetchone()["id"]
            dst = None
            if req.doc_id and req.clause_no:
                r = c.execute("SELECT id FROM chunks WHERE doc_id=%s AND clause_no=%s", (req.doc_id, req.clause_no)).fetchone()
                dst = r["id"] if r else None
            if req.doc_id:
                c.execute("""INSERT INTO doc_links(src_answer,kind,dst_doc,dst_chunk,origin,evidence)
                             VALUES (%s,'explains',%s,%s,'manual',%s)""", (kb_id, req.doc_id, dst, req.answer[:200]))
    return {"ok": True, "kb_answer_id": kb_id}


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
                                round(avg(latency_ms)) AS avg_ms, round(avg(tokens_in)) AS avg_in,
                                round(avg(tokens_out)) AS avg_out, round(avg(llm_calls),1) AS avg_calls
                         FROM query_log""").fetchone()
        last = c.execute("""SELECT id, ts, user_name, question, outcome, latency_ms, tokens_in, tokens_out, llm_calls, feedback
                            FROM query_log ORDER BY id DESC LIMIT 30""").fetchall()
        tk = c.execute("SELECT count(*) FILTER (WHERE status='open') AS open, count(*) AS total FROM tickets").fetchone()
    return {"summary": s, "last": last, "tickets": tk}


@app.post("/api/feedback")
def feedback(req: Feedback):
    with db.conn() as c:
        c.execute("UPDATE query_log SET feedback=%s WHERE id=%s", (req.value, req.query_id))
    return {"ok": True}
