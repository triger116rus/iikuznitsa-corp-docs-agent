"""Загрузка регламентов: DOCX → пункты (фрагменты) → БД + эмбеддинги + граф связей.

Запуск: python -m app.ingest <папка или файлы .docx> [--reset] [--department=…] [--process=…] [--replaces=<id>]
Через интерфейс: вкладка «Документы» → «Загрузить регламент» (POST /api/docs/upload).
Пункт регламента («2.3. …» с вложенными списками и таблицами) — единица поиска и цитирования.
Рёбра графа (doc_links) извлекаются здесь же, при загрузке: ссылки на разделы/пункты, на другие
регламенты/процессы/инструкции (висячие, если документа нет в корпусе) и упоминания ролей.
"""
import json
import re
import sys
import pathlib

import docx
from docx.table import Table
from docx.text.paragraph import Paragraph

from . import db, llm
from .glossary import TERMS

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
    r"|(?:регламент\w*|инструкци\w*|положени\w*|политик\w*)\s+(?!Заказчика)"
    r"(?!(?:определяет|устанавливает|регулирует|распространяется|вступает|действует|описывает)\b)[а-яё][а-яё ]{3,60}"
    r"|(?:процедур\w*)\s+(?:контроля|проверки|проверок|внутренней|старта)\s[а-яё ]{3,50}"
    r"|(?:матриц\w*)\s+(?:ответственности|документооборота|коммуникаций|рисков))", re.I)
REF_STOP = re.compile(r"\s(?:и|в|на|для|при|после|до|от|–|-|является|выполняются|может|под|понимаются)\s")
SELF_REF = re.compile(r"(?:настоящ|данн|этот|этого|этом)\w*\s*$", re.I)
REF_NOT_DOC = re.compile(r"^(?:регламент\w*|инструкци\w*)\s+(?:пользователя|по|под)$|^регламент\w*$", re.I)


def expand_roles(text: str, terms: dict | None = None) -> str:
    """Для эмбеддингов: «РП» → «руководитель проекта (РП)». Сотрудники спрашивают полными названиями ролей,
    регламенты пишут сокращениями — без расшифровки векторная модель их плохо сопоставляет."""
    for abbr, name in ROLES.items():
        syns = ROLE_SYNONYMS.get(abbr, [])
        syn = ", ".join(syns[:1] + [x for x in syns if x.isascii()][:2])   # русский разговорный + английский
        text = re.sub(rf"(?<![А-ЯЁA-Zа-яё]){re.escape(abbr)}(?![А-ЯЁA-Zа-яё])",
                      f"{name.lower()} ({abbr}{', ' + syn if syn else ''})", text)
    for abbr, name in {**(terms or {}), **TERMS}.items():   # прочие сокращения: глоссарий заказчика + из документа
        if abbr in ROLES:
            continue
        text = re.sub(rf"(?<![А-ЯЁA-Zа-яё(]){re.escape(abbr)}(?![А-ЯЁA-Zа-яё])", f"{name} ({abbr})", text)
    return text


def embed_text(title: str, section: str | None, text: str, terms: dict | None = None) -> str:
    return expand_roles(f"{title}. {section or ''}. {text}", terms)


def reembed():
    """Пересчитать эмбеддинги пунктов на месте (без перезагрузки документов и без потери базы разъяснений)."""
    with db.conn() as c:
        rows = c.execute("""SELECT ch.id, d.title, ch.section, ch.text, d.terms FROM chunks ch JOIN documents d ON d.id=ch.doc_id
                            ORDER BY ch.id""").fetchall()
        vecs = llm.embed([embed_text(r["title"], r["section"], r["text"], r["terms"]) for r in rows])
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
    """Элемент списка: нумерация у самого абзаца или у его стиля («Маркированный список» Word)."""
    ppr = p._p.pPr
    if ppr is not None and ppr.numPr is not None:
        return True
    st = p.style
    while st is not None:
        sppr = st.element.pPr
        if sppr is not None and sppr.numPr is not None:
            return True
        st = st.base_style
    return False


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


W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def numbering_map(document) -> dict:
    """numId → {уровень: (формат, шаблон номера, начало)} из numbering.xml: номера пунктов, которые Word
    проставляет автонумерацией (их нет в тексте абзаца — без этого такие пункты терялись, ревью Fable v0.4)."""
    try:
        el = document.part.numbering_part.element
    except Exception:
        return {}
    abstract = {}
    for a in el.findall(W_NS + "abstractNum"):
        lv = {}
        for l in a.findall(W_NS + "lvl"):
            fmt, txt, st = (l.find(W_NS + t) for t in ("numFmt", "lvlText", "start"))
            lv[int(l.get(W_NS + "ilvl"))] = (fmt.get(W_NS + "val") if fmt is not None else "decimal",
                                             txt.get(W_NS + "val") if txt is not None else "",
                                             int(st.get(W_NS + "val")) if st is not None else 1)
        abstract[a.get(W_NS + "abstractNumId")] = lv
    nums = {}
    for n in el.findall(W_NS + "num"):
        a = n.find(W_NS + "abstractNumId")
        if a is not None:
            nums[n.get(W_NS + "numId")] = abstract.get(a.get(W_NS + "val"), {})
    return nums


def auto_number(p: Paragraph, nums: dict, counters: dict) -> str | None:
    """Номер автонумерации Word для абзаца («2.3» или «2»), если нумерация десятичная;
    для маркированных списков и «а)» — None (это перечень внутри пункта)."""
    ppr = p._p.pPr
    if ppr is None or ppr.numPr is None or ppr.numPr.numId is None:
        return None
    nid = str(ppr.numPr.numId.val)
    il = int(ppr.numPr.ilvl.val) if ppr.numPr.ilvl is not None else 0
    levels = nums.get(nid)
    if not levels or il not in levels:
        return None
    cnt = counters.setdefault(nid, {})
    cnt[il] = cnt.get(il, levels[il][2] - 1) + 1
    for deeper in [k for k in cnt if k > il]:
        del cnt[deeper]
    fmt, tmpl, _ = levels[il]
    if fmt != "decimal" or "%" not in tmpl:
        return None
    label = tmpl
    for k in range(il + 1):
        label = label.replace(f"%{k + 1}", str(cnt.get(k, levels.get(k, ("", "", 1))[2])))
    label = label.strip().rstrip(".)")
    return label if re.fullmatch(r"\d+(?:\.\d+)*", label) else None


def is_heading(p: Paragraph) -> bool:
    name = (p.style.name if p.style is not None else "") or ""
    return name.lower().startswith(("heading", "заголовок"))


# Сокращения, которые документ расшифровывает сам: «Руководитель проекта (РП)» или строка «РП – Руководитель проекта».
TERM_PAREN = re.compile(r"([А-ЯЁ][а-яё-]+(?:\s+[а-яё-]+){0,4})\s*\(([А-ЯЁA-Z]{2,6})\)")
TERM_LINE = re.compile(r"^\s*([А-ЯЁA-Z][А-ЯЁA-Z0-9/-]{1,7})\s*[–—-]\s*(.{4,100}?)[.;]?\s*$")
ROLE_WORDS = re.compile(r"^(руководител|менеджер|архитектор|инженер|администратор|координатор|директор|специалист|"
                        r"бухгалтер|аналитик|владел|ответственн|начальник|юрист|куратор|эксперт)", re.I)


def extract_terms(paragraphs: list[str]) -> dict:
    """Сокращение → расшифровка из текста документа (дополняет справочник ролей и глоссарий заказчика)."""
    found = {}
    for t in paragraphs:
        m = TERM_LINE.match(t)
        if m:
            found.setdefault(m.group(1), m.group(2).strip())
        for m in TERM_PAREN.finditer(t):
            name, abbr = m.group(1).strip(), m.group(2)
            words = name.split()
            initials = "".join(w[0] for w in words if len(w) > 2).upper()
            if abbr.upper() in (initials, initials[-len(abbr):]):
                found.setdefault(abbr, name)
    return found


def parse_docx(path: pathlib.Path):
    """→ (заголовок документа, пункты, все непустые абзацы). Номер пункта — из текста («2.3. …») или из
    автонумерации Word; раздел — «N. Заголовок», автонумерация первого уровня или стиль заголовка;
    текст до первого раздела не теряется."""
    d = docx.Document(str(path))
    nums, counters = numbering_map(d), {}
    chunks, section, cur = [], None, None
    parents = {}  # clause_no → короткий текст пункта-заголовка (для контекста вложенных)
    first = next((p.text.strip() for p in d.paragraphs if p.text.strip()), path.stem)
    paragraphs, title_skipped = [], False

    def flush():
        if cur and cur["text"].strip():
            chunks.append(cur)

    def open_clause(no, body):
        nonlocal cur
        parent = no.rsplit(".", 1)[0]
        ctx = parents.get(parent)
        sec = section + (f" → {parent}. {ctx}" if ctx and "." in parent else "") if section else None
        cur = {"clause_no": no, "section": sec, "text": f"{no}. {body}"}
        if len(body) < 120 and body.endswith(":"):
            parents[no] = body.rstrip(":")

    for b in iter_blocks(d):
        if isinstance(b, Table):
            tt = table_text(b)
            if tt and cur is not None:
                cur["text"] += "\n" + tt
            elif tt:
                cur = {"clause_no": None, "section": section, "text": tt}
            continue
        text = b.text.strip()
        if not text:
            continue
        paragraphs.append(text)
        if not title_skipped and text == first:
            title_skipped = True       # заголовок документа — не пункт
            continue
        m = CLAUSE.match(text)
        if m and not is_list_item(b):
            flush()
            open_clause(m.group(1), m.group(2).strip())
            continue
        m = SECTION.match(text)
        if m and not is_list_item(b) and len(text) < 120:
            flush()
            cur = None
            section = f"{m.group(1)}. {m.group(2)}"
            continue
        auto = auto_number(b, nums, counters)
        if auto and "." in auto:                      # «2.3» автонумерацией Word — пункт
            flush()
            open_clause(auto, text)
            continue
        if (is_heading(b) or (auto and cur is None) or (auto and section_like(text, cur))):
            flush()                                   # «2. Назначение РП» автонумерацией или стилем — раздел
            cur = None
            section = f"{auto}. {text}" if auto else text
            continue
        if cur is not None:
            cur["text"] += ("\n— " if is_list_item(b) else "\n") + text
        else:
            cur = {"clause_no": None, "section": section, "text": text}
    flush()
    return first, chunks, paragraphs


def section_like(text: str, cur: dict | None) -> bool:
    """Абзац автонумерации первого уровня — заголовок раздела, а не элемент перечня: короткий, без точки/двоеточия
    в конце и идёт не сразу после пункта, открывающего перечень («…при одном из условий:»)."""
    if len(text) >= 120 or text.endswith((":", ";", ",", ".")):
        return False
    return not (cur and cur["text"].rstrip().endswith((":", ";")))


GENITIVE = (("Дирекции", "Дирекция"), ("Департамента", "Департамент"), ("Управления", "Управление"),
            ("Отдела", "Отдел"), ("Службы", "Служба"), ("Бухгалтерии", "Бухгалтерия"))


def guess_department(label: str) -> str | None:
    """Подразделение нового документа по названию: «Регламент закупок Дирекции облачных решений» →
    «Дирекция облачных решений», «Порядок … Бухгалтерии» → «Бухгалтерия». Не нашли — None (предупреждение)."""
    m = re.search(r"\b((?:Дирекци|Департамент|Управлени|Отдел|Служб)\w*(?:\s+(?!по\b|для\b|на\b|и\b|в\b)[\w-]{2,}){1,4})"
                  r"|\b(Бухгалтери\w*|Казначейств\w*)", label)
    if not m:
        return None
    name = (m.group(1) or m.group(2)).strip()
    word = name.split()[0]
    for case, base in GENITIVE:
        if word.lower() == case.lower():
            return base + name[len(word):]
    return name


def load(paths: list[pathlib.Path], reset: bool = False, department: str | None = None, process: str | None = None,
         replaces: int | None = None, origin: str = "initial") -> list[dict]:
    """Загрузка документов. department/process — явно (форма загрузки), иначе — по названию документа.
    replaces — id документа, который новая редакция заменяет (старая → superseded, ребро supersedes)."""
    db.init_schema()
    report = []
    with db.conn() as c:
        if reset:
            c.execute("TRUNCATE doc_links, chunks, documents RESTART IDENTITY CASCADE")
        loaded = []
        for path in paths:
            first, chunks, paragraphs = parse_docx(path)
            title = path.stem
            label = f"{title} {first}"
            dep = department or detect(DEPARTMENTS, label) or guess_department(label)
            proc = process or detect(PROCESSES, label)
            terms = {k: v for k, v in extract_terms(paragraphs).items() if k not in ROLES}
            warnings = []
            if not dep:
                warnings.append("подразделение не определено — документ считается общим для всей компании")
            if not any(ch["clause_no"] for ch in chunks):
                warnings.append("нумерованных пунктов не найдено — ссылки будут на раздел без номера пункта")
            if not chunks:
                warnings.append("текст не извлечён — документ пуст или это скан")
            c.execute("DELETE FROM documents WHERE file_name=%s", (path.name,))
            doc_id = c.execute(
                "INSERT INTO documents(title,file_name,department,process,owner_contact,origin,terms) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (title, path.name, dep, proc, "Служба качества", origin,
                 json.dumps(terms, ensure_ascii=False))).fetchone()["id"]
            vecs = llm.embed([embed_text(title, ch["section"], ch["text"], terms) for ch in chunks]) if chunks else []
            for i, (ch, v) in enumerate(zip(chunks, vecs)):
                ch["id"] = c.execute(
                    "INSERT INTO chunks(doc_id,clause_no,section,text,tokens,embedding,ord) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (doc_id, ch["clause_no"], ch["section"], ch["text"], len(ch["text"]) // 4, v, i)).fetchone()["id"]
            if replaces:
                c.execute("UPDATE documents SET status='superseded', superseded_by=%s WHERE id=%s AND id<>%s",
                          (doc_id, replaces, doc_id))
                c.execute("INSERT INTO doc_links(src_doc,kind,dst_doc,origin) VALUES (%s,'supersedes',%s,'manual')",
                          (doc_id, replaces))
            loaded.append((doc_id, title, chunks))
            report.append({"doc_id": doc_id, "title": title, "department": dep, "process": proc,
                           "clauses": len(chunks), "numbered": sum(1 for ch in chunks if ch["clause_no"]),
                           "terms": terms, "warnings": warnings})
            print(f"[ingest] {path.name}: {dep} / {proc}, пунктов {len(chunks)}"
                  + (f", сокращений из текста {len(terms)}" if terms else "")
                  + "".join(f"; ВНИМАНИЕ: {w}" for w in warnings))
        links = build_links(c, loaded, all_roles(c))
        for r in report:
            r["links"] = links.get(r["doc_id"], {})
    return report


def all_roles(c) -> dict:
    """Справочник ролей для графа: базовые роли + сокращения-роли, которые расшифровали сами документы."""
    roles = dict(ROLES)
    for r in c.execute("SELECT terms FROM documents WHERE terms IS NOT NULL").fetchall():
        for abbr, name in (r["terms"] or {}).items():
            if ROLE_WORDS.match(name):
                roles.setdefault(abbr, name)
    return roles


def build_links(c, loaded, roles):
    titles = {r["id"]: r["title"] for r in c.execute("SELECT id,title FROM documents").fetchall()}
    n_int = n_ext = n_dang = n_role = 0
    per_doc = {}
    for doc_id, title, chunks in loaded:
        before = (n_int, n_ext, n_dang, n_role)
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
                if SELF_REF.search(t[max(0, m.start() - 15):m.start()]):
                    continue                      # «настоящий регламент …» — сам документ, а не ссылка
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
        per_doc[doc_id] = {"refs_internal": n_int - before[0], "refs_external": n_ext - before[1],
                           "dangling": n_dang - before[2], "roles": n_role - before[3]}
    print(f"[graph] ссылок на разделы/пункты {n_int}, на документы/процессы {n_ext} (висячих {n_dang}), упоминаний ролей {n_role}; ролей {len(roles)}: "
          + ", ".join(f"{k}={v}" for k, v in sorted(roles.items())))
    return per_doc


def rebuild_links():
    """Пересборка графа по уже загруженным пунктам (без повторного извлечения текста и эмбеддингов)."""
    with db.conn() as c:
        loaded = []
        for d in c.execute("SELECT id, title FROM documents ORDER BY id").fetchall():
            chunks = c.execute("SELECT id, clause_no, section, text FROM chunks WHERE doc_id=%s ORDER BY ord", (d["id"],)).fetchall()
            loaded.append((d["id"], d["title"], [dict(ch) for ch in chunks]))
        build_links(c, loaded, all_roles(c))


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
    opt = lambda name: next((a.split("=", 1)[1] for a in sys.argv if a.startswith(f"--{name}=")), None)
    load(files, reset="--reset" in sys.argv, department=opt("department"), process=opt("process"),
         replaces=int(opt("replaces")) if opt("replaces") else None, origin=opt("origin") or "initial")
