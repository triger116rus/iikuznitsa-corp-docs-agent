"""Прогон контрольных вопросов через API агента и отчёт о качестве.

Запуск: python eval/run_eval.py [--url https://<стенд>] [--only A1,C1] [--out eval/report.md]
Метрики: верный исход (ответ / уточнение / запрос в службу качества), верный пункт в цитатах,
галлюцинации (ответ там, где ответа в регламентах нет), время и токены на вопрос.
Если агент задал уточняющий вопрос, скрипт отвечает за пользователя (поле reply) и проверяет итог.
"""
import argparse
import json
import pathlib
import time
import urllib.request


def ask(url, msg, dialog=None):
    body = json.dumps({"message": msg, "dialog_id": dialog, "user_name": "eval"}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url + "/api/ask", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        for line in r:
            if line.strip():
                ev = json.loads(line)
                if ev["type"] in ("result", "error"):
                    return ev
    return {"type": "error", "text": "нет результата"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default=str(pathlib.Path(__file__).parent / "report.md"))
    a = ap.parse_args()
    qs = json.loads((pathlib.Path(__file__).parent / "questions.json").read_text(encoding="utf-8"))
    if a.only:
        qs = [q for q in qs if q["id"] in a.only.split(",")]
    rows = []
    for q in qs:
        t0 = time.time()
        r = ask(a.url, q["q"])
        status, final, turns = r.get("status"), r, 1
        if status == "clarify" and q.get("reply"):
            final = ask(a.url, q["reply"], r.get("dialog_id"))
            turns = 2
        ok_first = status == q["expect"] or (q["expect"] == "escalated" and r.get("also_escalated"))
        ok_final = final.get("status") == q.get("then", q["expect"]) or             (q["expect"] == "escalated" and final.get("also_escalated"))   # ответ с оговоркой + письмо в СК
        cited = [c.get("clause_no") for c in final.get("citations", []) if c.get("kind") == "chunk"]
        ok_clause = ((not q.get("clauses")) or any(c in q["clauses"] for c in cited)) and \
            (not q.get("kb") or any(c.get("kind") == "kb" for c in final.get("citations", [])))
        hallucination = q["expect"] in ("escalated", "out_of_scope") and final.get("status") == "answer"             and not final.get("also_escalated")
        rows.append({"id": q["id"], "case": q["case"], "q": q["q"], "first": status, "final": final.get("status"),
                     "ok": bool(ok_first and ok_final and ok_clause), "ok_clause": ok_clause, "cited": cited,
                     "hallucination": bool(hallucination), "sec": round(time.time() - t0, 1), "turns": turns,
                     "tokens_in": final.get("tokens_in", 0) + (r.get("tokens_in", 0) if turns == 2 else 0),
                     "tokens_out": final.get("tokens_out", 0) + (r.get("tokens_out", 0) if turns == 2 else 0),
                     "answer": (final.get("answer") or final.get("text") or "")[:300]})
        print(f"{q['id']}: {'OK ' if rows[-1]['ok'] else 'ERR'} {status} -> {final.get('status')} пункты {cited} {rows[-1]['sec']} с", flush=True)
    n = len(rows)
    ok = sum(r["ok"] for r in rows)
    hall = sum(r["hallucination"] for r in rows)
    lines = [f"# Отчёт eval — {time.strftime('%Y-%m-%d %H:%M')}", "",
             f"Верно: **{ok} из {n}**; галлюцинаций (ответ там, где ответа нет): **{hall}**; "
             f"среднее время на вопрос: {sum(r['sec'] for r in rows) / max(n, 1):.0f} с; "
             f"токенов в среднем: {sum(r['tokens_in'] for r in rows) // max(n, 1)} → {sum(r['tokens_out'] for r in rows) // max(n, 1)}", "",
             "| id | Кейс | Вопрос | Исход | Пункты в цитатах | Верно | с |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['id']} | {r['case']} | {r['q']} | {r['first']} → {r['final']} | {', '.join(c for c in r['cited'] if c) or '—'} | "
                     f"{'да' if r['ok'] else 'нет'} | {r['sec']} |")
    lines += ["", "## Ответы", ""] + [f"- **{r['id']}**: {r['answer']}" for r in rows]
    pathlib.Path(a.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Итого {ok}/{n}, галлюцинаций {hall}. Отчёт: {a.out}")


if __name__ == "__main__":
    main()
