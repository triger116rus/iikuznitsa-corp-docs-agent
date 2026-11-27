"""Производительность и узкие места: время этапов работы агента по журналу запросов (без изменения цикла агента).

Каждый шаг агента пишется в журнал с моментом появления (ms от начала запроса). Интервал между шагами относится
к этапу, который его закрыл (шаг «План: …» появляется после вызова модели-планировщика), а интервал после шага
«Составляю ответ…» — к генерации ответа моделью. По этим интервалам считаются медиана и 95-й перцентиль каждого
этапа, доля этапа в общем времени и скорость генерации — и подсказка, что именно тормозит.
"""
LABELS = {"plan": "план (модель)", "search": "поиск", "check": "проверки кодом", "graph": "граф связей",
          "answer": "ответ модели", "verify": "сверка цитат", "escalate": "письмо в службу качества",
          "cache": "ответ из кэша", None: "прочее"}
SLOW_MS = 60_000          # «долгий» ответ — дольше минуты
LIMIT = 500               # последних запросов в расчёте


def stage_times(steps: list, latency_ms: int | None) -> dict[str, int]:
    out: dict[str, int] = {}
    prev_ms, prev_tool = 0, None
    for s in steps or []:
        ms, tool = s.get("ms") or 0, s.get("tool")
        label = LABELS["answer"] if prev_tool == "answer" else LABELS.get(tool, LABELS[None])
        out[label] = out.get(label, 0) + max(0, ms - prev_ms)
        prev_ms, prev_tool = ms, tool
    if latency_ms and latency_ms > prev_ms:   # хвост: генерация после последнего шага или запись результата
        label = LABELS["answer"] if prev_tool == "answer" else "прочее"
        out[label] = out.get(label, 0) + latency_ms - prev_ms
    return out


def _pct(vals: list, p: float):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(p * len(vals)))] if vals else None


def bottlenecks(rows: list[dict]) -> dict:
    """rows — записи query_log (steps, latency_ms, tokens_out, outcome)."""
    per: dict[str, list[int]] = {}
    total = 0
    tps = []
    for r in rows:
        st = stage_times(r.get("steps"), r.get("latency_ms"))
        for k, v in st.items():
            per.setdefault(k, []).append(v)
            total += v
        gen = st.get(LABELS["answer"], 0)
        if gen > 3000 and r.get("tokens_out"):
            tps.append(r["tokens_out"] / (gen / 1000))   # грубо: в интервал входит и чтение контекста моделью
    stages = sorted(({"stage": k, "n": len(v), "p50_ms": _pct(v, 0.5), "p95_ms": _pct(v, 0.95),
                      "share": round(sum(v) / total, 3) if total else 0} for k, v in per.items()),
                    key=lambda x: -x["share"])
    lat = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
    res = {"queries": len(rows), "p50_ms": _pct(lat, 0.5), "p95_ms": _pct(lat, 0.95),
           "slow_share": round(sum(1 for x in lat if x > SLOW_MS) / len(lat), 3) if lat else 0,
           "gen_tokens_per_s": round(_pct(tps, 0.5), 1) if tps else None, "stages": stages}
    res["hints"] = hints(res)
    return res


def hints(r: dict) -> list[str]:
    out = []
    by = {s["stage"]: s for s in r["stages"]}
    gen = by.get(LABELS["answer"])
    if gen and gen["share"] >= 0.5:
        out.append(f"Главное узкое место — ответ модели: чтение найденных пунктов и генерация ({gen['share']:.0%} времени"
                   + (f", ≈ {r['gen_tokens_per_s']} выходных ток/с с учётом чтения" if r["gen_tokens_per_s"] else "")
                   + "). Ускоряет GPU или более быстрая модель; при росте нагрузки — второй сервер модели.")
    if gen and gen["p50_ms"] and gen["p95_ms"] and gen["p95_ms"] > 2.5 * gen["p50_ms"]:
        out.append("Большой разброс времени ответа модели (95-й перцентиль в 2,5+ раза выше медианы) — вероятно, вопросы ждут "
                   "в очереди к модели: ответы выполняются по одному. Нужен второй слот на GPU или второй сервер.")
    plan = by.get(LABELS["plan"])
    if plan and plan["p50_ms"] and plan["p50_ms"] > 15_000:
        out.append("План (первый вызов модели) дольше 15 с — модель занята другим запросом или медленно читает контекст.")
    srch = by.get(LABELS["search"])
    if srch and srch["p95_ms"] and srch["p95_ms"] > 5_000:
        out.append("Поиск дольше 5 с — проверьте службу эмбеддингов (llama-embed) и индексы PostgreSQL.")
    mail = by.get(LABELS["escalate"])
    if mail and mail["p95_ms"] and mail["p95_ms"] > 10_000:
        out.append("Отправка письма дольше 10 с — медленный почтовый сервер.")
    if r["slow_share"] >= 0.3:
        out.append(f"{r['slow_share']:.0%} вопросов отвечены дольше минуты — сотрудники это заметят "
                   "(ориентир заказчика — секунды).")
    cache = by.get(LABELS["cache"])
    if cache:
        out.append(f"Ответов из кэша: {cache['n']} — без вызова модели.")
    return out or ["Узких мест не видно."]


def from_db(c, days: int | None = None) -> dict:
    q = "SELECT steps, latency_ms, tokens_out, outcome FROM query_log"
    if days:
        rows = c.execute(q + " WHERE ts > now() - make_interval(days => %s) ORDER BY id DESC LIMIT %s",
                         (days, LIMIT)).fetchall()
    else:
        rows = c.execute(q + " ORDER BY id DESC LIMIT %s", (LIMIT,)).fetchall()
    return bottlenecks(rows)
