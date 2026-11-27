"""Инструменты агента: гибридный поиск (FTS + вектор, RRF), поиск разъяснений службы качества,
обход графа связей (рекурсивный CTE по doc_links)."""
import re

from . import db, llm

WORD = re.compile(r"[а-яёa-z0-9]{3,}", re.I)
STOP = {"кто", "что", "как", "когда", "где", "какой", "какая", "какие", "каком", "этот", "для", "при", "или",
        "если", "надо", "нужно", "можно", "должен", "должна", "проект", "проекта", "проекте"}


def tsquery(text: str) -> str:
    words = [w.lower() for w in WORD.findall(text) if w.lower() not in STOP]
    return " | ".join(dict.fromkeys(words)) or "пусто"


def hybrid(queries: list[str], department: str | None = None, k: int = 8) -> list[dict]:
    """RRF по всем под-запросам: полнотекстовый поиск (морфология russian) + вектор bge-m3.
    Только действующие редакции; если подразделение известно — только его регламенты."""
    vecs = llm.embed(queries)
    scores, rows = {}, {}
    dep_sql = "AND (d.department = %(dep)s OR d.department IS NULL)" if department else ""
    with db.conn() as c:
        for q, v in zip(queries, vecs):
            p = {"q": tsquery(q), "v": v, "dep": department}
            fts = c.execute(f"""
                SELECT ch.id FROM chunks ch JOIN documents d ON d.id=ch.doc_id
                WHERE d.status='in_force' {dep_sql} AND ch.tsv @@ to_tsquery('russian', %(q)s)
                ORDER BY ts_rank_cd(ch.tsv, to_tsquery('russian', %(q)s)) DESC LIMIT 20""", p).fetchall()
            vec = c.execute(f"""
                SELECT ch.id FROM chunks ch JOIN documents d ON d.id=ch.doc_id
                WHERE d.status='in_force' {dep_sql} AND ch.embedding IS NOT NULL
                ORDER BY ch.embedding <=> %(v)s::vector LIMIT 20""", p).fetchall()
            for lst in (fts, vec):
                for rank, r in enumerate(lst):
                    scores[r["id"]] = scores.get(r["id"], 0) + 1 / (60 + rank)
        top = sorted(scores, key=scores.get, reverse=True)[:k]
        if top:
            for r in c.execute("""
                SELECT ch.id, ch.doc_id, ch.clause_no, ch.section, ch.text, d.title, d.department, d.process
                FROM chunks ch JOIN documents d ON d.id=ch.doc_id WHERE ch.id = ANY(%s)""", (top,)).fetchall():
                rows[r["id"]] = r | {"score": round(scores[r["id"]], 4), "via": "поиск"}
    return [rows[i] for i in top if i in rows]


def kb_search(question: str, department: str | None = None, k: int = 3) -> list[dict]:
    """Разъяснения службы качества (дообучение): FTS + вектор, порог по косинусу."""
    v = llm.embed([question])[0]
    with db.conn() as c:
        rows = c.execute("""
            SELECT a.id, a.question, a.answer, a.doc_id, a.clause_no, a.department, a.ticket_id, d.title,
                   1 - (a.embedding <=> %(v)s::vector) AS sim
            FROM kb_answers a LEFT JOIN documents d ON d.id=a.doc_id
            WHERE a.embedding IS NOT NULL AND (%(dep)s::text IS NULL OR a.department IS NULL OR a.department=%(dep)s)
            ORDER BY a.embedding <=> %(v)s::vector LIMIT %(k)s""", {"v": v, "dep": department, "k": k}).fetchall()
    return [r for r in rows if r["sim"] >= 0.72]


def expand(chunk_ids: list[int], depth: int = 2) -> dict:
    """Обход графа от найденных пунктов:
    - references с целью в корпусе → связанные пункты (рекурсивно, глубина ≤ depth);
    - вложенные подпункты найденного пункта (структура регламента);
    - висячие ссылки (цель вне корпуса) → пробелы базы;
    - разъяснения службы качества, привязанные к пункту/документу."""
    if not chunk_ids:
        return {"linked": [], "children": [], "gaps": [], "explains": []}
    with db.conn() as c:
        linked = c.execute("""
            WITH RECURSIVE walk(chunk_id, depth, path) AS (
                SELECT l.dst_chunk, 1, ARRAY[l.src_chunk, l.dst_chunk]
                FROM doc_links l WHERE l.src_chunk = ANY(%(ids)s) AND l.kind='references' AND l.dst_chunk IS NOT NULL
              UNION
                SELECT l.dst_chunk, w.depth + 1, w.path || l.dst_chunk
                FROM walk w JOIN doc_links l ON l.src_chunk = w.chunk_id
                WHERE l.kind='references' AND l.dst_chunk IS NOT NULL AND w.depth < %(depth)s
                  AND NOT l.dst_chunk = ANY(w.path)
            )
            SELECT DISTINCT ON (ch.id) ch.id, ch.doc_id, ch.clause_no, ch.section, ch.text, d.title, d.department,
                   d.process, w.depth, w.path
            FROM walk w JOIN chunks ch ON ch.id=w.chunk_id JOIN documents d ON d.id=ch.doc_id
            WHERE d.status='in_force' AND NOT ch.id = ANY(%(ids)s)
            ORDER BY ch.id, w.depth LIMIT 6""", {"ids": chunk_ids, "depth": depth}).fetchall()
        children = c.execute("""
            SELECT ch.id, ch.doc_id, ch.clause_no, ch.section, ch.text, d.title, d.department, d.process
            FROM chunks base JOIN chunks ch ON ch.doc_id=base.doc_id AND ch.clause_no LIKE base.clause_no || '.%%'
              AND ch.clause_no NOT LIKE base.clause_no || '.%%.%%'
            JOIN documents d ON d.id=ch.doc_id
            WHERE base.id = ANY(%(ids)s) AND base.clause_no IS NOT NULL AND NOT ch.id = ANY(%(ids)s)
            ORDER BY ch.doc_id, ch.ord LIMIT 6""", {"ids": chunk_ids}).fetchall()
        # Структура регламента как граф: раздел → его первый пункт (обычно формулирует правило раздела).
        leads = c.execute("""
            SELECT DISTINCT ON (ch.id) ch.id, ch.doc_id, ch.clause_no, ch.section, ch.text, d.title, d.department, d.process
            FROM chunks base JOIN chunks ch ON ch.doc_id=base.doc_id AND ch.section=base.section
              AND ch.clause_no = split_part(base.clause_no, '.', 1) || '.1'
            JOIN documents d ON d.id=ch.doc_id
            WHERE base.id = ANY(%(ids)s) AND base.clause_no IS NOT NULL AND NOT ch.id = ANY(%(ids)s)
            LIMIT 3""", {"ids": chunk_ids}).fetchall()
        gaps = c.execute("""
            SELECT DISTINCT ON (l.dst_node) l.dst_node, l.dst_ref_text, l.src_chunk, ch.clause_no, d.title
            FROM doc_links l JOIN chunks ch ON ch.id=l.src_chunk JOIN documents d ON d.id=l.src_doc
            WHERE l.src_chunk = ANY(%(ids)s) AND l.kind='references' AND l.dst_node LIKE 'doc:%%'""",
                         {"ids": chunk_ids}).fetchall()
        explains = c.execute("""
            SELECT DISTINCT a.id, a.question, a.answer, a.doc_id, a.clause_no, a.ticket_id, d.title
            FROM doc_links l JOIN kb_answers a ON a.id=l.src_answer LEFT JOIN documents d ON d.id=a.doc_id
            WHERE l.kind='explains' AND l.dst_chunk = ANY(%(ids)s)""", {"ids": chunk_ids}).fetchall()
    for r in linked:
        r["via"] = f"граф: ссылка (глубина {r['depth']})"
    for r in children:
        r["via"] = "граф: подпункт"
    for r in leads:
        r["via"] = "граф: первый пункт раздела"
    return {"linked": list(leads) + list(linked), "children": list(children), "gaps": list(gaps), "explains": list(explains)}


def departments() -> list[str]:
    with db.conn() as c:
        return [r["department"] for r in c.execute(
            "SELECT DISTINCT department FROM documents WHERE department IS NOT NULL AND status='in_force' ORDER BY 1").fetchall()]


def department_scores(queries: list[str]) -> dict:
    """Лучшая косинусная близость к вопросу в регламентах каждого подразделения.
    Если в нескольких подразделениях есть почти одинаково близкие пункты — ответ зависит от подразделения.
    Документы без подразделения (общие для компании) — под ключом '' (раньше не учитывались, и найденный в них
    пункт уходил в службу качества как «близких пунктов нет», ревью Fable v0.4)."""
    vecs = llm.embed(queries)
    best = {}
    with db.conn() as c:
        for v in vecs:
            for r in c.execute("""
                SELECT coalesce(d.department, '') AS department, max(1 - (ch.embedding <=> %(v)s::vector)) AS sim
                FROM chunks ch JOIN documents d ON d.id=ch.doc_id
                WHERE d.status='in_force' AND ch.embedding IS NOT NULL
                GROUP BY 1""", {"v": v}).fetchall():
                best[r["department"]] = max(best.get(r["department"], 0), float(r["sim"]))
    return best
