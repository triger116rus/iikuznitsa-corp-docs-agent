"""Загрузка регламентов: DOCX → пункты (фрагменты) → БД + эмбеддинги + граф связей.

Запуск: python -m app.ingest <папка или файлы .docx> [--reset]
Пункт регламента («2.3. …» с вложенными списками и таблицами) — единица поиска и цитирования.
Рёбра графа (doc_links) извлекаются здесь же, при загрузке: ссылки на разделы/пункты, на другие
регламенты/процессы/инструкции (висячие, если документа нет в корпусе) и упоминания ролей.
"""
import re
import sys
import pathlib

import docx
from docx.table import Table
from docx.text.paragraph import Paragraph

from . import db, llm

# Подразделения: шаблон в названии файла/документа → каноническое имя (область действия регламента).
DEPARTMENTS = [
    (r"\bИБ\b|информационн\w+ безопасност", "Дирекция ИБ"),
    (r"ДБРЦТ|бизнес-решений и цифровой трансформации", "ДБРЦТ"),
    (r"Тех\.?\s*дирекц|Технологическ\w+ дирекц|Технической дирекц", "Технологическая дирекция"),
]
PROCESSES = [
    (r"[Пп]ресейл", "Пресейл"),
    (r"[Пп]роектн\w+ деятельност", "Проектная деятельность"),
]

CLAUSE = re.compile(r"^(\d+(?:\.\d+)+)\.?\s+(.*)$", re.S)
SECTION = re.compile(r"^(\d+)\.\s+(.{2,120})$")
# Роли (узлы графа role:<сокращение>): связывают пункты разных регламентов об одной роли.
ROLES = {"SS": "Менеджер по продаже решений", "АМ": "Аккаунт-менеджер", "ПА": "Пресейл-архитектор",
         "РП": "Руководитель проекта", "АП": "Администратор проекта", "ГИП": "Главный инженер проекта",
         "РСП": "Руководитель сервисного проекта", "BDM": "Менеджер по развитию бизнеса", "РМ": "Ресурс-менеджер",
         "КД": "Координатор департамента"}
# Синонимы ролей из живой речи сотрудников (подсказка заказчика 26.11: «РП называют проджект-менеджером
# или по-английски»). Используются в поиске (вариант вопроса) и в эмбеддингах пунктов.
ROLE_SYNONYMS = {
    "РП": ["проджект-менеджер", "проджект менеджер", "проджект", "project manager", "PM", "ПМ", "менеджер проекта"],
    "SS": ["сейлз", "сейл", "sales", "solution sales", "продавец решений", "менеджер по продажам"],
    "АМ": ["аккаунт", "account manager", "KAM", "менеджер по работе с клиентом"],
    "ПА": ["presale architect", "пресейл архитектор", "архитектор пресейла", "пресейлер"],
    "ГИП": ["chief engineer", "главный инженер", "техлид проекта"],
    "АП": ["project administrator", "администратор"],
    "РМ": ["resource manager", "ресурсник", "менеджер ресурсов"],
    "КД": ["координатор"],
    "BDM": ["business development manager", "менеджер по развитию"],
    "РСП": ["service project manager", "руководитель сервиса"],
}
ROLE_ALIASES = {"КД": r"Координатор\w* департамента", "РМ": r"(?<![А-ЯЁ])РМ(?![А-ЯЁ])|Ресурс-менеджер\w*"}
INTERNAL_REF = re.compile(r"(?:см\.|согласно|в соответствии с)\s*(?:раздел\w*|п\.|пункт\w*)\s*(\d+(?:\.\d+)*)", re.I)
# Ссылки на другие документы и именованные процессы (цели вне корпуса → висячие рёбра = пробелы базы):
#   процесс «Арбитраж расчёта себестоимости», регламент подбора субподрядчика, процедуры контроля маржинальности …
EXTERNAL_REF = re.compile(
    r"((?:процесс\w*|процедур\w*)\s+«[^»]{3,80}»?"
    r"|(?:регламент\w*|инструкци\w*|положени\w*|политик\w*)\s+(?!Заказчика)[а-яё][а-яё ]{3,60}"
    r"|(?:процедур\w*)\s+(?:контроля|проверки|проверок|внутренней|старта)\s[а-яё ]{3,50}"
    r"|(?:матриц\w*)\s+(?:ответственности|документооборота|коммуникаций|рисков))", re.I)
REF_STOP = re.compile(r"\s(?:и|в|на|для|при|после|до|от|–|-|является|выполняются|может|под|понимаются)\s")
REF_NOT_DOC = re.compile(r"^(?:регламент\w*|инструкци\w*)\s+(?:пользователя|по|под)$|^регламент\w*$", re.I)


def expand_roles(text: str) -> str:
    """Для эмбеддингов: «РП» → «руководитель проекта (РП)». Сотрудники спрашивают полными названиями ролей,
    регламенты пишут сокращениями — без расшифровки векторная модель их плохо сопоставляет."""
    for abbr, name in ROLES.items():
        syns = ROLE_SYNONYMS.get(abbr, [])
        syn = ", ".join(syns[:1] + [x for x in syns if x.isascii()][:2])   # русский разговорный + английский
        text = re.sub(rf"(?<![А-ЯЁA-Zа-яё]){re.escape(abbr)}(?![А-ЯЁA-Zа-яё])",
                      f"{name.lower()} ({abbr}{', ' + syn if syn else ''})", text)
    return text


def embed_text(title: str, section: str | None, text: str) -> str:
    return expand_roles(f"{title}. {section or ''}. {text}")


def reembed():
    """Пересчитать эмбеддинги пунктов на месте (без перезагрузки документов и без потери базы разъяснений)."""
    with db.conn() as c:
        rows = c.execute("""SELECT ch.id, d.title, ch.section, ch.text FROM chunks ch JOIN documents d ON d.id=ch.doc_id
                            ORDER BY ch.id""").fetchall()
        vecs = llm.embed([embed_text(r["title"], r["section"], r["text"]) for r in rows])
        for r, v in zip(rows, vecs):
            c.execute("UPDATE chunks SET embedding=%s WHERE id=%s", (v, r["id"]))
    print(f"[reembed] пунктов: {len(rows)}")


def node_key(ref: str) -> str:
    """Нормализованный ключ узла: «регламентом подбора субподрядчика» и «регламенту подбора…» → один узел."""
    words = re.sub(r"[«»]", "", ref.lower()).split()
    return " ".join(w[:6] for w in words)


def detect(patterns, text, default=None):
    for pat, name in patterns:
        if re.search(pat, text):
            return name
    return default


def iter_blocks(document):
    """Абзацы и таблицы в порядке следования в документе."""
    body = document.element.body
    for el in body.iterchildren():
        if el.tag.endswith("}p"):
            yield Paragraph(el, document)
        elif el.tag.endswith("}tbl"):
            yield Table(el, document)


def is_list_item(p: Paragraph) -> bool:
    ppr = p._p.pPr
    return ppr is not None and ppr.numPr is not None


def table_text(t: Table) -> str:
    rows = [[c.text.strip() for c in r.cells] for r in t.rows]
    if not rows:
        return ""
    head, body = rows[0], rows[1:]
    lines = []
    for col, h in enumerate(head):
        vals = [r[col] for r in body if col < len(r) and r[col]]
        if vals:
            lines.append(f"{h}: " + "; ".join(v.lstrip("- ").rstrip(";") for v in vals))
    return "\n".join(lines)


def parse_docx(path: pathlib.Path):
    d = docx.Document(str(path))
    chunks, section, cur = [], None, None
    parents = {}  # clause_no → короткий текст пункта-заголовка (для контекста вложенных)

    def flush():
        if cur and cur["text"].strip():
            chunks.append(cur)

    for b in iter_blocks(d):
        if isinstance(b, Table):
            tt = table_text(b)
            if tt and cur is not None:
                cur["text"] += "\n" + tt
            continue
        text = b.text.strip()
        if not text:
            continue
        m = CLAUSE.match(text)
        if m and not is_list_item(b):
            flush()
            no, body = m.group(1), m.group(2).strip()
            parent = no.rsplit(".", 1)[0]
            ctx = parents.get(parent)
            sec = section + (f" → {parent}. {ctx}" if ctx and "." in parent else "") if section else None
            cur = {"clause_no": no, "section": sec, "text": f"{no}. {body}"}
            if len(body) < 120 and body.endswith(":"):
                parents[no] = body.rstrip(":")
            continue
        m = SECTION.match(text)
        if m and not is_list_item(b) and len(text) < 120:
            flush()
            cur = None
            section = f"{m.group(1)}. {m.group(2)}"
            continue
        if cur is not None:
            cur["text"] += ("\n— " if is_list_item(b) else "\n") + text
        elif section:
            cur = {"clause_no": None, "section": section, "text": text}
    flush()
    first = next((p.text.strip() for p in d.paragraphs if p.text.strip()), path.stem)
    return first, chunks


def load(paths: list[pathlib.Path], reset: bool = False):
    db.init_schema()
    with db.conn() as c:
        if reset:
            c.execute("TRUNCATE doc_links, chunks, documents RESTART IDENTITY CASCADE")
        roles = ROLES
        loaded = []
        for path in paths:
            first, chunks = parse_docx(path)
            title = path.stem
            label = f"{title} {first}"
            dep = detect(DEPARTMENTS, label)
            proc = detect(PROCESSES, label)
            c.execute("DELETE FROM documents WHERE file_name=%s", (path.name,))
            doc_id = c.execute(
                "INSERT INTO documents(title,file_name,department,process,owner_contact) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (title, path.name, dep, proc, "Служба качества")).fetchone()["id"]
            texts = [embed_text(title, ch["section"], ch["text"]) for ch in chunks]
            vecs = llm.embed(texts)
            for i, (ch, v) in enumerate(zip(chunks, vecs)):
                ch["id"] = c.execute(
                    "INSERT INTO chunks(doc_id,clause_no,section,text,tokens,embedding,ord) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (doc_id, ch["clause_no"], ch["section"], ch["text"], len(ch["text"]) // 4, v, i)).fetchone()["id"]
            loaded.append((doc_id, title, chunks))
            print(f"[ingest] {path.name}: {dep} / {proc}, пунктов {len(chunks)}")
        build_links(c, loaded, roles)


def build_links(c, loaded, roles):
    titles = {r["id"]: r["title"] for r in c.execute("SELECT id,title FROM documents").fetchall()}
    n_int = n_ext = n_dang = n_role = 0
    for doc_id, title, chunks in loaded:
        c.execute("DELETE FROM doc_links WHERE src_doc=%s AND origin='regex'", (doc_id,))
        by_no = {ch["clause_no"]: ch["id"] for ch in chunks if ch["clause_no"]}
        sec_first = {}
        for ch in chunks:
            if ch["section"]:
                sec_first.setdefault(ch["section"].split(".")[0], ch["id"])
        for ch in chunks:
            t = ch["text"]
            for m in INTERNAL_REF.finditer(t):
                no = m.group(1)
                dst = by_no.get(no) or sec_first.get(no)
                c.execute("INSERT INTO doc_links(src_doc,src_chunk,kind,dst_doc,dst_chunk,dst_ref_text,evidence) VALUES (%s,%s,'references',%s,%s,%s,%s)",
                          (doc_id, ch["id"], doc_id if dst else None, dst, m.group(0), m.group(0)))
                n_int += 1
            for m in EXTERNAL_REF.finditer(t):
                ref = REF_STOP.split(" " + m.group(1).strip() + " ")[0].strip()
                ref = " ".join(ref.split()[:7])
                if REF_NOT_DOC.match(ref) or len(ref.split()) < 2:
                    continue
                hit = c.execute("SELECT id, similarity(title,%s) s FROM documents ORDER BY s DESC LIMIT 1", (ref,)).fetchone()
                dst_doc = hit["id"] if hit and hit["s"] > 0.5 and hit["id"] != doc_id else None
                c.execute("INSERT INTO doc_links(src_doc,src_chunk,kind,dst_doc,dst_node,dst_ref_text,evidence) VALUES (%s,%s,'references',%s,%s,%s,%s)",
                          (doc_id, ch["id"], dst_doc, None if dst_doc else "doc:" + node_key(ref), ref, t[max(0, m.start() - 60): m.end() + 20]))
                n_ext += 1
                n_dang += dst_doc is None
            for abbr in roles:
                pat = ROLE_ALIASES.get(abbr) or rf"(?<![А-ЯЁA-Z]){re.escape(abbr)}(?![А-ЯЁA-Z])"
                if re.search(pat, t):
                    c.execute("INSERT INTO doc_links(src_doc,src_chunk,kind,dst_node,dst_ref_text) VALUES (%s,%s,'mentions_role',%s,%s)",
                              (doc_id, ch["id"], "role:" + abbr, roles[abbr]))
                    n_role += 1
    print(f"[graph] ссылок на разделы/пункты {n_int}, на документы/процессы {n_ext} (висячих {n_dang}), упоминаний ролей {n_role}; ролей {len(roles)}: "
          + ", ".join(f"{k}={v}" for k, v in sorted(roles.items())))


def rebuild_links():
    """Пересборка графа по уже загруженным пунктам (без повторного извлечения текста и эмбеддингов)."""
    with db.conn() as c:
        loaded = []
        for d in c.execute("SELECT id, title FROM documents ORDER BY id").fetchall():
            chunks = c.execute("SELECT id, clause_no, section, text FROM chunks WHERE doc_id=%s ORDER BY ord", (d["id"],)).fetchall()
            loaded.append((d["id"], d["title"], [dict(ch) for ch in chunks]))
        build_links(c, loaded, ROLES)


if __name__ == "__main__":
    if "--reembed" in sys.argv:
        reembed()
        sys.exit(0)
    if "--links" in sys.argv:
        rebuild_links()
        sys.exit(0)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    files = []
    for a in args:
        p = pathlib.Path(a)
        files += sorted(p.glob("*.docx")) if p.is_dir() else [p]
    load(files, reset="--reset" in sys.argv)
