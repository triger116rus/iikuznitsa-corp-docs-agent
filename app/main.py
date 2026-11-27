"""Веб-приложение: чат с агентом (поток шагов NDJSON), служба качества (запросы → ответ → база знаний),
реестр документов, граф связей, аналитика, пользователи.
Права проверяются на сервере для каждого запроса (app/auth.py): сотрудник — чат, свои запросы, регламенты;
служба качества — все запросы, база разъяснений, граф, аналитика; администратор — загрузка документов, пользователи."""
import io
import json
import pathlib
import re
import time
import zipfile
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from . import agent, auth, config, db, ingest, kb, mailbox

@asynccontextmanager
async def lifespan(_app):
    db.init_schema()        # схема идемпотентна: новые таблицы/колонки (пользователи, источник документа) — при старте
    mailbox.start_poller()  # фоновый опрос почты службы качества (если настроен IMAP)
    yield


app = FastAPI(title="ИИ-агент по корпоративным регламентам", lifespan=lifespan)
STATIC = pathlib.Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class Ask(BaseModel):
    message: str
    dialog_id: str | None = None
    user_email: str | None = None      # адрес копии писем; по умолчанию — e-mail из учётной записи
    nocache: bool = False              # не отдавать ответ на повторный вопрос из журнала (прогон контрольных вопросов)


class Login(BaseModel):
    login: str
    password: str


class NewUser(BaseModel):
    login: str
    name: str
    role: str = "employee"
    email: str | None = None
    department: str | None = None


class UserPatch(BaseModel):
    name: str | None = None
    email: str | None = None
    department: str | None = None
    role: str | None = None
    active: bool | None = None


class TicketAnswer(BaseModel):
    answer: str
    answered_by: str = "Служба качества"
    doc_id: int | None = None
    clause_no: str | None = None
    add_to_kb: bool = True


class Report(BaseModel):
    text: str
    query_id: int | None = None


class Feedback(BaseModel):
    query_id: int
    value: str


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/admin")
def admin_page():
    """Вход для службы качества и администратора (тот же клиент; вкладки — по роли учётной записи)."""
    return FileResponse(STATIC / "index.html")


@app.post("/api/login")
def login(req: Login, request: Request):
    session = auth.login(req.login, req.password, request.client.host if request.client else "?")
    u = auth.get_user(session["uid"])
    r = JSONResponse({"ok": True, "user": jsonable(u)})
    r.set_cookie(auth.COOKIE, auth.sign(session), max_age=auth.SESSION_HOURS * 3600, httponly=True,
                 samesite="lax", secure=config.COOKIE_SECURE)
    return r


@app.post("/api/logout")
def logout():
    r = JSONResponse({"ok": True})
    r.delete_cookie(auth.COOKIE)
    return r


@app.get("/api/me")
def me(request: Request):
    u = auth.require(request)
    return {"user": jsonable(u), "roles": auth.ROLES}


def jsonable(u: dict) -> dict:
    return {k: (str(v) if k in ("created_at", "last_login") and v else v) for k, v in u.items()}


@app.get("/api/users")
def users(request: Request):
    auth.require(request, "admin")
    with db.conn() as c:
        return c.execute(f"SELECT {', '.join(auth.PUBLIC)} FROM users ORDER BY role DESC, login").fetchall()


@app.post("/api/users")
def add_user(req: NewUser, request: Request):
    """Новый пользователь; пароль генерируется и показывается администратору один раз."""
    auth.require(request, "admin")
    u, password = auth.create_user(req.login, req.name, req.role, req.email, req.department)
    return {"user": jsonable(u), "password": password}


@app.post("/api/users/{uid}")
def patch_user(uid: int, req: UserPatch, request: Request):
    me_ = auth.require(request, "admin")
    fields = req.model_dump(exclude_none=True)
    if uid == me_["id"] and (fields.get("role", "admin") != "admin" or fields.get("active") is False):
        raise HTTPException(400, "нельзя снять роль администратора или отключить самого себя")
    return {"user": jsonable(auth.update_user(uid, fields))}


@app.post("/api/users/{uid}/reset")
def reset_user(uid: int, request: Request):
    auth.require(request, "admin")
    return {"password": auth.reset_password(uid)}


@app.get("/healthz")
def healthz():
    with db.conn() as c:
        n = c.execute("SELECT count(*) n FROM chunks").fetchone()["n"]
    return {"ok": True, "chunks": n}


@app.post("/api/ask")
async def ask(req: Ask, request: Request):
    u = auth.require(request)
    email = (req.user_email or "").strip() or u.get("email")

    def gen():
        try:
            for ev in agent.run(req.message.strip(), req.dialog_id, u["name"], email, u.get("login"), u.get("department"),
                                req.nocache):
                yield json.dumps(ev, ensure_ascii=False, default=str) + "\n"
        except Exception as e:  # ошибка — тоже событие, интерфейс покажет её
            yield json.dumps({"type": "error", "text": f"{type(e).__name__}: {e}"}, ensure_ascii=False) + "\n"

    return StreamingResponse(iterate_in_threadpool(gen()), media_type="application/x-ndjson")


@app.get("/api/tickets")
def tickets(request: Request, mine: int = 0):
    """Служба качества видит все запросы; сотрудник (и любой, кто попросил mine=1) — только свои, по учётной записи."""
    u = auth.require(request)
    login_ = u["login"] if mine or auth.RANK[u["role"]] < auth.RANK["quality"] else None
    with db.conn() as c:
        # Контроль срока: у каждого письма в службу качества есть срок ответа; просроченные видны всем в интерфейсе.
        return c.execute("""SELECT id, created_at, user_name, department, question, context, status, answer, answered_by,
                                   answered_at, mail_to, mail_body,
                                   created_at + make_interval(hours => %(h)s) AS due_at,
                                   (status = 'open' AND now() > created_at + make_interval(hours => %(h)s)) AS overdue
                            FROM tickets
                            WHERE %(u)s::text IS NULL OR user_login=%(u)s ORDER BY id DESC LIMIT 100""",
                         {"u": login_, "h": config.ANSWER_SLA_HOURS}).fetchall()


@app.post("/api/tickets/{tid}/answer")
def answer_ticket(tid: int, req: TicketAnswer, request: Request):
    """Запасной путь кейса 3 (основной — ответ письмом, см. mailbox.py): ответ службы качества → база разъяснений."""
    u = auth.require(request, "quality")
    if not config.WEB_ANSWER:
        raise HTTPException(403, "на этом стенде служба качества отвечает только письмом")
    try:
        kb_id = kb.add_answer(tid, req.answer, f"{u['name']} (служба качества)", req.doc_id, req.clause_no, req.add_to_kb)
    except KeyError:
        raise HTTPException(404, "нет такого запроса")
    notified = mailbox.notify_employee(tid, req.answer)
    return {"ok": True, "kb_answer_id": kb_id, "notified": notified}


@app.get("/api/settings")
def settings():
    return {"web_answer": config.WEB_ANSWER, "sla_hours": config.ANSWER_SLA_HOURS, "auth": config.AUTH}


@app.post("/api/mail/poll")
def mail_poll(request: Request):
    """Забрать ответы службы качества из почты сейчас (иначе — фоновый опрос раз в 30 с)."""
    auth.require(request, "quality")
    return {"processed": mailbox.poll_once()}


@app.get("/api/kb")
def kb_list(request: Request):
    auth.require(request, "quality")
    with db.conn() as c:
        return c.execute("""SELECT a.id, a.ticket_id, a.question, a.answer, a.clause_no, a.department, a.approved_by,
                                   a.created_at, d.title FROM kb_answers a LEFT JOIN documents d ON d.id=a.doc_id
                            ORDER BY a.id DESC""").fetchall()


@app.get("/api/docs")
def docs(request: Request):
    auth.require(request)
    with db.conn() as c:
        return c.execute("""SELECT d.id, d.title, d.department, d.process, d.status, d.file_name, d.origin, d.superseded_by,
                                   (SELECT count(*) FROM chunks WHERE doc_id=d.id) AS clauses,
                                   (SELECT count(*) FROM doc_links WHERE src_doc=d.id AND kind='references') AS refs,
                                   (SELECT count(*) FROM doc_links WHERE src_doc=d.id AND kind='references' AND dst_node IS NOT NULL) AS dangling
                            FROM documents d ORDER BY d.department, d.title""").fetchall()


def check_upload_token(request: Request):
    auth.require(request, "admin")


@app.post("/api/docs/upload")
async def upload_doc(request: Request, filename: str, department: str = "", process: str = "", replaces: int | None = None):
    """Новый регламент или новая редакция (DOCX): разбор на пункты, эмбеддинги, граф связей — сразу в поиске агента.
    Подразделение — из формы (иначе по названию документа); replaces — документ, который утратит силу."""
    check_upload_token(request)
    name = pathlib.Path(filename).name
    if not name.lower().endswith(".docx") or not re.fullmatch(r"[\w .,()«»№-]{3,150}\.docx", name, re.I):
        raise HTTPException(400, "нужен файл .docx с обычным именем")
    body = await request.body()
    if len(body) > config.UPLOAD_MAX_MB * 1024 * 1024:
        raise HTTPException(413, f"файл больше {config.UPLOAD_MAX_MB} МБ")
    try:
        zipfile.ZipFile(io.BytesIO(body)).getinfo("word/document.xml")
    except Exception:
        raise HTTPException(400, "это не документ Word (.docx)")
    folder = pathlib.Path(config.UPLOAD_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(body)
    t0 = time.time()
    try:
        report = await run_in_threadpool(ingest.load, [path], False, department.strip() or None,
                                         process.strip() or None, replaces, "upload")
    except Exception as e:
        raise HTTPException(400, f"не удалось разобрать документ: {type(e).__name__}: {e}")
    agent._deps["t"] = 0   # новое подразделение сразу узнаётся в вопросах
    return {"ok": True, "seconds": round(time.time() - t0, 1), **report[0]}


@app.delete("/api/docs/{doc_id}")
def delete_doc(doc_id: int, request: Request):
    """Удалить загруженный через интерфейс документ (исходные регламенты заказчика не удаляются).
    Если он заменял прежнюю редакцию — она снова действует."""
    check_upload_token(request)
    with db.conn() as c:
        d = c.execute("SELECT id, origin FROM documents WHERE id=%s", (doc_id,)).fetchone()
        if not d:
            raise HTTPException(404, "нет такого документа")
        if d["origin"] != "upload":
            raise HTTPException(403, "исходные регламенты удалять нельзя — только загруженные через интерфейс")
        c.execute("UPDATE documents SET status='in_force', superseded_by=NULL WHERE superseded_by=%s", (doc_id,))
        c.execute("UPDATE kb_answers SET doc_id=NULL WHERE doc_id=%s", (doc_id,))
        c.execute("DELETE FROM doc_links WHERE src_answer IS NOT NULL AND dst_doc=%s", (doc_id,))
        c.execute("DELETE FROM documents WHERE id=%s", (doc_id,))
    agent._deps["t"] = 0
    return {"ok": True}


@app.get("/api/docs/{doc_id}")
def doc(doc_id: int, request: Request):
    auth.require(request)
    with db.conn() as c:
        return c.execute("SELECT id, clause_no, section, text FROM chunks WHERE doc_id=%s ORDER BY ord", (doc_id,)).fetchall()


@app.get("/api/graph")
def graph(request: Request):
    """Граф для вкладки «Граф»: документы, роли, внешние (отсутствующие) документы и рёбра между ними."""
    auth.require(request, "quality")
    with db.conn() as c:
        docs_ = c.execute("SELECT id, title, department, status FROM documents ORDER BY department, id").fetchall()
        edges = c.execute("""
            SELECT l.src_doc, l.kind, l.dst_doc, l.dst_node, min(l.dst_ref_text) AS label, count(*) AS n
            FROM doc_links l WHERE l.src_doc IS NOT NULL GROUP BY 1,2,3,4""").fetchall()
        gaps = c.execute("""
            SELECT l.dst_node, min(l.dst_ref_text) AS label, count(*) AS n, array_agg(DISTINCT d.title) AS docs
            FROM doc_links l JOIN documents d ON d.id=l.src_doc WHERE l.dst_node LIKE 'doc:%%'
            GROUP BY 1 ORDER BY 3 DESC""").fetchall()
    return {"docs": docs_, "edges": edges, "gaps": gaps}


@app.get("/api/stats")
def stats(request: Request):
    auth.require(request, "quality")
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
def report(req: Report, request: Request):
    """Сотрудник не меняет регламенты, но может сообщить о несоответствии/противоречии → запрос в службу качества."""
    u = auth.require(request)
    with db.conn() as c:
        q = c.execute("SELECT question, answer, citations FROM query_log WHERE id=%s", (req.query_id,)).fetchone() if req.query_id else None
        body = f"Сотрудник {u['name']} сообщает о несоответствии в регламентах:\n\n{req.text}\n"
        if q:
            body += (f"\nВопрос: {q['question']}\nОтвет агента: {q['answer']}\nИсточники: "
                     + "; ".join(c_["source"] for c_ in (q["citations"] or [])))
        tid = c.execute("""INSERT INTO tickets(user_name,user_login,question,context,mail_to,mail_body)
                           VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (u["name"], u.get("login"), "Несоответствие: " + req.text, json.dumps({"type": "inconsistency", "query_id": req.query_id}),
                         agent.config.QUALITY_EMAIL, body)).fetchone()["id"]
        subject = f"[Регламенты #{tid}] Сообщение о несоответствии в регламентах"
        try:
            sent = mailbox.send(config.QUALITY_EMAIL, subject, body + "\n\nИИ-агент по регламентам")
        except Exception:
            sent = False
        c.execute("UPDATE tickets SET context = context || %s WHERE id=%s", (json.dumps({"sent": sent}), tid))
    return {"ok": True, "ticket_id": tid, "mail_sent": sent}


@app.get("/api/diag/export")
def diag_export(request: Request, level: str = "metrics", days: int = 30):
    """Диагностическая выгрузка с обезличиванием (см. app/diag.py): тексты регламентов и ответов не выгружаются."""
    auth.require(request, "admin")
    from . import diag
    if level not in diag.LEVELS:
        raise HTTPException(400, f"level: {', '.join(diag.LEVELS)}")
    data = diag.export(level, max(1, min(days, 365)))
    name = f"corpdocs-diag-{level}-{time.strftime('%Y%m%d-%H%M')}.zip"
    return StreamingResponse(io.BytesIO(data), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/feedback")
def feedback(req: Feedback, request: Request):
    u = auth.require(request)
    with db.conn() as c:   # оценить можно только свой ответ
        c.execute("UPDATE query_log SET feedback=%s WHERE id=%s AND (user_login=%s OR %s)",
                  (req.value, req.query_id, u.get("login"), not config.AUTH))
    return {"ok": True}
