"""Дообучение: ответ службы качества → база разъяснений агента (эмбеддинг + ребро графа explains к пункту).
Источник ответа — письмо службы качества (mailbox.py) или форма в интерфейсе (main.py)."""
from . import db, llm


def add_answer(ticket_id: int, answer: str, answered_by: str, doc_id: int | None = None,
               clause_no: str | None = None, add_to_kb: bool = True) -> int | None:
    with db.conn() as c:
        t = c.execute("SELECT * FROM tickets WHERE id=%s", (ticket_id,)).fetchone()
        if not t:
            raise KeyError(ticket_id)
        c.execute("""UPDATE tickets SET status='answered', answer=%s, answered_by=%s, answered_at=now(),
                     context = coalesce(context, '{}'::jsonb) - 'pending_answer' - 'pending_by' WHERE id=%s""",
                  (answer, answered_by, ticket_id))
        if not add_to_kb or (t["context"] or {}).get("type") == "inconsistency":
            return None
        # Повторный ответ службы качества в той же ветке — уточнение: заменяет прежнее разъяснение, а не дублирует его.
        old = [r["id"] for r in c.execute("SELECT id FROM kb_answers WHERE ticket_id=%s", (ticket_id,)).fetchall()]
        if old:
            c.execute("DELETE FROM doc_links WHERE src_answer = ANY(%s)", (old,))
            c.execute("DELETE FROM kb_answers WHERE id = ANY(%s)", (old,))
        v = llm.embed([f"{t['question']}\n{answer}"])[0]
        dep = t["department"]
        if doc_id:
            dep = c.execute("SELECT department FROM documents WHERE id=%s", (doc_id,)).fetchone()["department"]
        kb_id = c.execute("""INSERT INTO kb_answers(ticket_id,question,answer,doc_id,clause_no,department,approved_by,embedding)
                             VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                          (ticket_id, t["question"], answer, doc_id, clause_no, dep, answered_by, v)).fetchone()["id"]
        if doc_id:
            dst = None
            if clause_no:
                r = c.execute("SELECT id FROM chunks WHERE doc_id=%s AND clause_no=%s", (doc_id, clause_no)).fetchone()
                dst = r["id"] if r else None
            c.execute("""INSERT INTO doc_links(src_answer,kind,dst_doc,dst_chunk,origin,evidence)
                         VALUES (%s,'explains',%s,%s,'manual',%s)""", (kb_id, doc_id, dst, answer[:200]))
    return kb_id


def note_redirect(ticket_id: int, text: str, by: str) -> None:
    """Промежуточный ответ службы качества (вопрос переадресован): отмечаем в запросе, в базу разъяснений не добавляем."""
    import json
    with db.conn() as c:
        c.execute("UPDATE tickets SET context = coalesce(context, '{}'::jsonb) || %s WHERE id=%s AND status='open'",
                  (json.dumps({"redirect_note": text[:500], "redirect_by": by}, ensure_ascii=False), ticket_id))


def ticket_employee_email(ticket_id: int) -> str:
    """E-mail сотрудника, задавшего вопрос (в копии письма) — его ответы в базу разъяснений не берём."""
    with db.conn() as c:
        r = c.execute("SELECT context->>'user_email' AS e FROM tickets WHERE id=%s", (ticket_id,)).fetchone()
    return ((r or {}).get("e") or "").strip().lower()


def note_pending(ticket_id: int, text: str, by: str) -> None:
    """Ответ владельца процесса (не службы качества): ждёт подтверждения службы качества, в базу пока не идёт."""
    import json
    with db.conn() as c:
        r = c.execute("UPDATE tickets SET context = coalesce(context, '{}'::jsonb) || %s WHERE id=%s RETURNING id",
                      (json.dumps({"pending_answer": text[:2000], "pending_by": by}, ensure_ascii=False), ticket_id)).fetchone()
    if not r:
        raise KeyError(ticket_id)


def pending_answer(ticket_id: int) -> dict | None:
    with db.conn() as c:
        r = c.execute("SELECT context->>'pending_answer' AS a, context->>'pending_by' AS b FROM tickets WHERE id=%s",
                      (ticket_id,)).fetchone()
    return r if r and r["a"] else None


def confirm_pending(ticket_id: int, by: str) -> int | None:
    """Служба качества подтвердила ответ владельца процесса → в базу разъяснений."""
    p = pending_answer(ticket_id)
    if not p:
        raise KeyError(ticket_id)
    with db.conn() as c:
        c.execute("UPDATE tickets SET status='open', context = context - 'pending_answer' - 'pending_by' WHERE id=%s",
                  (ticket_id,))
    return add_answer(ticket_id, p["a"], f"{p['b']}, подтверждено службой качества ({by})")


def revoke(ticket_id: int, by: str) -> None:
    """Служба качества отзывает разъяснение: убираем из базы и графа, запрос снова открыт."""
    import json
    with db.conn() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM kb_answers WHERE ticket_id=%s", (ticket_id,)).fetchall()]
        if ids:
            c.execute("DELETE FROM doc_links WHERE src_answer = ANY(%s)", (ids,))
            c.execute("DELETE FROM kb_answers WHERE id = ANY(%s)", (ids,))
        r = c.execute("""UPDATE tickets SET status='open', answer=NULL, answered_by=NULL, answered_at=NULL,
                         context = coalesce(context, '{}'::jsonb) || %s WHERE id=%s RETURNING id""",
                      (json.dumps({"revoked_by": by}, ensure_ascii=False), ticket_id)).fetchone()
    if not r:
        raise KeyError(ticket_id)
