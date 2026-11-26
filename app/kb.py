"""Дообучение: ответ службы качества → база разъяснений агента (эмбеддинг + ребро графа explains к пункту).
Источник ответа — письмо службы качества (mailbox.py) или форма в интерфейсе (main.py)."""
from . import db, llm


def add_answer(ticket_id: int, answer: str, answered_by: str, doc_id: int | None = None,
               clause_no: str | None = None, add_to_kb: bool = True) -> int | None:
    with db.conn() as c:
        t = c.execute("SELECT * FROM tickets WHERE id=%s", (ticket_id,)).fetchone()
        if not t:
            raise KeyError(ticket_id)
        c.execute("UPDATE tickets SET status='answered', answer=%s, answered_by=%s, answered_at=now() WHERE id=%s",
                  (answer, answered_by, ticket_id))
        if not add_to_kb or (t["context"] or {}).get("type") == "inconsistency":
            return None
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
