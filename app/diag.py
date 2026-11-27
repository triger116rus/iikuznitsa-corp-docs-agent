"""Диагностическая выгрузка с обезличиванием — для разбора работы агента вне контура (в т. ч. облачной моделью).

Заказчик (27.11): содержимое регламентов наружу отдавать нельзя. Поэтому тексты регламентов, ответов, цитат,
разъяснений службы качества, писем и шагов агента не выгружаются НИКОГДА. Уровни:
  metrics (по умолчанию) — без текстов: исходы, время шагов, токены, кэш, оценки сотрудников, номера пунктов
                           и документов, запросы в службу качества (статусы и сроки), версия базы, пороги агента;
  masked                 — плюс текст вопросов сотрудников с заменой e-mail, телефонов, ФИО, сумм, номеров договоров,
                           ИНН, организаций и имён пользователей на метки [EMAIL_1], [ФИО_2]… Обезличивание по шаблонам
                           ловит не всё (название клиента без «ООО» может остаться) — уровень включается осознанно.
Пользователи заменяются условными номерами u1, u2… В архив кладётся README.txt: что выгружено и сколько замен.

Запуск: python -m app.diag [--level metrics|masked] [--days 30] --out diag.zip
API (администратор): GET /api/diag/export?level=metrics&days=30
"""
import io
import json
import re
import sys
import zipfile
from datetime import datetime, timezone

from . import config, db

LEVELS = ("metrics", "masked")

PATTERNS = [   # порядок важен: сначала длинные и конкретные шаблоны
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("ТЕЛЕФОН", re.compile(r"(?:\+7|\b8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}\b")),
    ("ИНН", re.compile(r"\b(?:ИНН\s*)?\d{10}(?:\d{2})?\b")),
    ("КАРТА", re.compile(r"\b(?:\d{4}[\s-]?){3}\d{4}\b")),
    ("ОРГАНИЗАЦИЯ", re.compile(r"\b(?:ООО|АО|ПАО|ЗАО|ОАО|ИП|ГК|НКО|ФГУП|МУП)\s*[«\"“][^»\"”]{1,80}[»\"”]")),
    ("НОМЕР", re.compile(r"(?:№|N\s|#)\s*[\w/.-]*\d[\w/.-]*")),
    ("СУММА", re.compile(r"\d[\d\s]*(?:[.,]\d+)?\s*(?:руб\w*|₽|тыс\w*\.?|млн|млрд|\$|usd|eur|евро|долл\w*)", re.I)),
    ("ФИО", re.compile(r"\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.|\b[А-ЯЁ]\.\s?[А-ЯЁ]\.\s?[А-ЯЁ][а-яё]+")),
    ("ФИО", re.compile(r"\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:ович|евич|ич|овн|евн|ичн|иничн)[а-яё]{0,3}\b")),
]


class Masker:
    """Заменяет персональные и коммерческие данные метками; одинаковое значение — одна и та же метка."""

    def __init__(self, names: list[str]):
        self.labels: dict[tuple[str, str], str] = {}
        self.counts: dict[str, int] = {}
        # Имена пользователей агента — тоже персональные данные: слова с заглавной буквы от 4 букв («Иванова»),
        # с учётом регистра, чтобы не задеть обычные слова из служебных имён («Прогон контрольных вопросов»).
        words = {w for n in names for w in re.split(r"[\s.]+", n or "") if len(w) >= 4 and w[0].isupper()}
        self.name_re = re.compile(r"\b(?:" + "|".join(map(re.escape, sorted(words, key=len, reverse=True)))
                                  + r")[а-яё]{0,3}\b") if words else None   # с падежным окончанием: «Петрову»

    def _label(self, kind: str, value: str) -> str:
        key = (kind, value.lower())
        if key not in self.labels:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            self.labels[key] = f"[{kind}_{self.counts[kind]}]"
        return self.labels[key]

    def __call__(self, text: str | None) -> str | None:
        if not text:
            return text
        for kind, rx in PATTERNS:
            text = rx.sub(lambda m: self._label(kind, m.group(0)), text)
        if self.name_re:
            text = self.name_re.sub(lambda m: self._label("ИМЯ", m.group(0)), text)
        return text


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def export(level: str = "metrics", days: int = 30) -> bytes:
    """Zip-архив выгрузки за последние days дней."""
    if level not in LEVELS:
        raise ValueError(f"level: {LEVELS}")
    with db.conn() as c:
        users = c.execute("SELECT login, name, role, department FROM users ORDER BY id").fetchall()
        logs = c.execute("""SELECT id, ts, dialog_id, user_login, question, outcome, steps, citations, tokens_in, tokens_out,
                                   llm_calls, latency_ms, feedback, result, cache_key IS NOT NULL AS cache_source, corpus_ver
                            FROM query_log WHERE ts > now() - make_interval(days => %s) ORDER BY id""", (days,)).fetchall()
        tickets = c.execute("""SELECT id, created_at, user_login, department, question, context, status, answered_by,
                                      answered_at FROM tickets WHERE created_at > now() - make_interval(days => %s)
                               ORDER BY id""", (days,)).fetchall()
        kb = c.execute("SELECT id, ticket_id, doc_id, clause_no, department, created_at FROM kb_answers ORDER BY id").fetchall()
        docs = c.execute("""SELECT d.id, d.department, d.process, d.status, d.superseded_by, d.origin, d.loaded_at,
                                   (SELECT count(*) FROM chunks ch WHERE ch.doc_id = d.id) AS clauses
                            FROM documents d ORDER BY d.id""").fetchall()
    uid = {u["login"]: f"u{i}" for i, u in enumerate(users, 1)}
    role = {u["login"]: u["role"] for u in users}
    mask = Masker([u["name"] for u in users])
    q_text = (lambda t: mask(t)) if level == "masked" else (lambda t: None)

    def user(login):
        return {"user": uid.get(login, "u?" if login else None), "role": role.get(login)}

    out_logs = []
    for r in logs:
        res = r["result"] or {}
        out_logs.append({
            "id": r["id"], "ts": _iso(r["ts"]), "dialog": (r["dialog_id"] or "")[:6], **user(r["user_login"]),
            "question": q_text(r["question"]), "outcome": r["outcome"], "status": res.get("status"),
            "latency_ms": r["latency_ms"], "llm_calls": r["llm_calls"], "tokens_in": r["tokens_in"],
            "tokens_out": r["tokens_out"], "feedback": r["feedback"],
            "steps": [{"tool": s.get("tool"), "ms": s.get("ms")} for s in (r["steps"] or [])],   # текст шагов — нет
            "citations": [{"doc_id": x.get("doc_id"), "clause_no": x.get("clause_no"), "kind": x.get("kind")}
                          for x in (r["citations"] or [])],
            "caveat": bool(res.get("caveat")), "conflict": bool(res.get("conflict")), "used_kb": bool(res.get("used_kb")),
            "from_cache": res.get("from_cache"), "semantic_cache": bool(res.get("cache_question")),
            "cache_source": r["cache_source"], "corpus_ver": (r["corpus_ver"] or "")[:8],
            "ticket_id": res.get("ticket_id"), "see_also": len(res.get("see_also") or []),
        })
    out_tickets = []
    for t in tickets:
        ctx = t["context"] or {}
        by = (t["answered_by"] or "").lower()
        out_tickets.append({
            "id": t["id"], "created_at": _iso(t["created_at"]), **user(t["user_login"]), "department": t["department"],
            "question": q_text(t["question"]), "status": t["status"], "answered_at": _iso(t["answered_at"]),
            "answered_by": ("владелец процесса, подтверждено" if "подтвержд" in by else
                            "служба качества" if by else None),
            "type": ctx.get("type"), "mail_sent": ctx.get("sent"), "redirected": bool(ctx.get("redirect_note")),
            "pending_owner_answer": bool(ctx.get("pending_answer")), "revoked": bool(ctx.get("revoked_by")),
            "employee_notified": bool(ctx.get("notified_to")), "gaps": len(ctx.get("gaps") or []),
        })
    out_kb = [{"id": k["id"], "ticket_id": k["ticket_id"], "doc_id": k["doc_id"], "clause_no": k["clause_no"],
               "department": k["department"], "created_at": _iso(k["created_at"])} for k in kb]
    out_docs = [{k: _iso(v) for k, v in d.items()} for d in docs]

    lat = sorted(x["latency_ms"] for x in out_logs if x["latency_ms"] is not None)
    pct = lambda p: lat[min(len(lat) - 1, int(p * len(lat)))] if lat else None
    by_outcome: dict[str, int] = {}
    for x in out_logs:
        by_outcome[x["outcome"]] = by_outcome.get(x["outcome"], 0) + 1
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "level": level, "days": days,
        "queries": len(out_logs), "outcomes": by_outcome,
        "latency_ms": {"p50": pct(0.5), "p90": pct(0.9), "max": lat[-1] if lat else None},
        "tokens_avg": {"in": round(sum(x["tokens_in"] or 0 for x in out_logs) / max(1, len(out_logs))),
                       "out": round(sum(x["tokens_out"] or 0 for x in out_logs) / max(1, len(out_logs)))},
        "cache": {"exact": sum(1 for x in out_logs if x["from_cache"] and not x["semantic_cache"]),
                  "semantic": sum(1 for x in out_logs if x["semantic_cache"]),
                  "semantic_other_question": sum(1 for x in out_logs if x["semantic_cache"]
                                                 and x["feedback"] == "other_question")},
        "feedback": {"up": sum(1 for x in out_logs if x["feedback"] == "up"),
                     "down": sum(1 for x in out_logs if x["feedback"] == "down")},
        "tickets": {"total": len(out_tickets), "open": sum(1 for t in out_tickets if t["status"] == "open")},
        "kb_answers": len(out_kb), "documents_in_force": sum(1 for d in out_docs if d["status"] == "in_force"),
        # Только несекретные настройки агента — по ним разбирают пороги и качество.
        "settings": _settings(),
    }
    readme = (
        "Диагностическая выгрузка ИИ-агента по регламентам\n"
        f"Уровень: {level}; период: {days} дн.; сформирована {summary['generated_at']}\n\n"
        "Не выгружается никогда: тексты регламентов и пунктов, ответы агента, цитаты, разъяснения службы качества,\n"
        "письма, тексты шагов агента, имена, логины и e-mail пользователей (заменены на u1, u2…), пароли и ключи.\n"
        + ("Тексты вопросов: не выгружаются.\n" if level == "metrics" else
           "Тексты вопросов: выгружены с обезличиванием по шаблонам. Замены: "
           + (", ".join(f"{k} — {v}" for k, v in sorted(mask.counts.items())) or "нет") + ".\n"
           "ВНИМАНИЕ: шаблоны ловят не всё (например, название клиента без «ООО»). Перед передачей наружу\n"
           "просмотрите query_log.jsonl и tickets.jsonl.\n")
        + "\nФайлы: summary.json — сводка и пороги; query_log.jsonl — запросы; tickets.jsonl — запросы в службу\n"
          "качества; kb_meta.jsonl — разъяснения (только привязка к пункту); documents.jsonl — регламенты (без текста).\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", readme)
        z.writestr("summary.json", json.dumps(summary, ensure_ascii=False, indent=1, default=str))
        for name, rows in (("query_log", out_logs), ("tickets", out_tickets), ("kb_meta", out_kb), ("documents", out_docs)):
            z.writestr(f"{name}.jsonl", "\n".join(json.dumps(x, ensure_ascii=False, default=str) for x in rows))
    return buf.getvalue()


def _settings() -> dict:
    from . import agent
    return {"RELEVANCE_MIN": agent.RELEVANCE_MIN, "AMBIGUITY_GAP": agent.AMBIGUITY_GAP, "MAX_CLARIFY": agent.MAX_CLARIFY,
            "KB_CONFLICT_SIM": agent.KB_CONFLICT_SIM, "SEM_CACHE_SIM": agent.SEM_CACHE_SIM, "TOP_K": config.TOP_K,
            "MAX_EVIDENCE_TOKENS": config.MAX_EVIDENCE_TOKENS, "ANSWER_SLA_HOURS": config.ANSWER_SLA_HOURS,
            "LLM_MODEL": config.LLM_MODEL, "EMBED_MODEL": config.EMBED_MODEL}


if __name__ == "__main__":
    args = sys.argv[1:]
    opt = lambda k, d: args[args.index(k) + 1] if k in args else d
    data = export(opt("--level", "metrics"), int(opt("--days", "30")))
    out = opt("--out", "diag.zip")
    with open(out, "wb") as f:
        f.write(data)
    print(f"готово: {out}, {len(data) // 1024} КБ")
