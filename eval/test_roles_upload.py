"""Проверка ролей и загрузки документов на стенде (тестовый экземпляр): матрица прав по ролям, загрузка нового
регламента нового подразделения и новой редакции, ответы агента по ним, удаление загруженного.

Запуск: python eval/test_roles_upload.py <url> <файл учёток> <папка тестовых DOCX> [--keep] [--no-ask]
Файл учёток: логин<TAB>роль<TAB>пароль (пароли в вывод не печатаются). --keep — не удалять загруженные документы.
"""
import http.cookiejar
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

url, cred_file, docs_dir = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])
creds = {l.split("\t")[0]: l.split("\t")[-1] for l in pathlib.Path(cred_file).read_text(encoding="utf-8").splitlines() if l}
fails = []


def session(user=None):
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    if user:
        call(op, "POST", "/api/login", {"login": user, "password": creds[user]})
    return op


def call(op, method, path, body=None, raw=None, headers=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url + path, data=data, method=method, headers=h | (headers or {}))
    try:
        with op.open(req, timeout=900) as r:
            text = r.read().decode("utf-8")
            return r.status, text
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def check(name, cond, info=""):
    print(("OK   " if cond else "FAIL ") + name + (f" — {info}" if info and not cond else ""))
    if not cond:
        fails.append(name)


def ask(op, q, dialog=None, nocache=True):
    st, text = call(op, "POST", "/api/ask", {"message": q, "dialog_id": dialog, "nocache": nocache})
    res = [json.loads(l) for l in text.splitlines() if l.strip()]
    return next((e for e in res if e["type"] in ("result", "error")), {"type": "error", "status": st})


# 1. Матрица прав: (метод, путь) → минимальная роль.
MATRIX = [("GET", "/api/docs", "employee"), ("GET", "/api/tickets?mine=1", "employee"), ("GET", "/api/kb", "quality"),
          ("GET", "/api/stats", "quality"), ("GET", "/api/graph", "quality"), ("GET", "/api/users", "admin")]
RANK = {None: -1, "employee": 0, "quality": 1, "admin": 2}
ops = {None: session(), "eval": session("eval"), "quality": session("quality"), "admin": session("admin")}
role_of = {None: None, "eval": "employee", "quality": "quality", "admin": "admin"}
for who, op in ops.items():
    for m, path, need in MATRIX:
        st, _ = call(op, m, path)
        expect = 200 if RANK[role_of[who]] >= RANK[need] else (401 if who is None else 403)
        check(f"права: {who or 'без входа'} {m} {path} → {expect}", st == expect, f"получено {st}")
st, _ = call(ops[None], "POST", "/api/login", {"login": "admin", "password": "wrong-password"})
check("вход с неверным паролем → 401", st == 401, str(st))
st, text = call(ops["eval"], "GET", "/api/tickets")
check("сотрудник видит только свои запросы", st == 200 and all(t.get("user_name") == "Прогон контрольных вопросов"
                                                                 for t in json.loads(text)), text[:200])

# 2. Загрузка: новое подразделение (автонумерация Word) и новая редакция.
admin = ops["admin"]
for d in json.loads(call(admin, "GET", "/api/docs")[1]):   # остатки прошлого прогона
    if d["origin"] == "upload":
        call(admin, "DELETE", f"/api/docs/{d['id']}")
docs = json.loads(call(admin, "GET", "/api/docs")[1])
presale = next(d for d in docs if d["title"] == "Пресейловая деятельность Дирекции ИБ" and d["status"] == "in_force")
uploaded = []
for f, params in [("Регламент закупок Дирекции облачных решений (тест).docx", ""),
                  ("Пресейловая деятельность Дирекции ИБ (редакция 2, тест).docx", f"&replaces={presale['id']}")]:
    t0 = time.time()
    st, text = call(admin, "POST", "/api/docs/upload?filename=" + urllib.parse.quote(f) + params,
                    raw=(docs_dir / f).read_bytes())
    j = json.loads(text)
    check(f"загрузка «{f}» → 200", st == 200, text[:300])
    if st == 200:
        uploaded.append(j["doc_id"])
        print(f"     {time.time() - t0:.0f} с: подразделение {j['department']!r}, пунктов {j['clauses']} (с номером {j['numbered']}), "
              f"сокращения {j['terms']}, связи {j['links']}, предупреждения {j['warnings']}")
st, _ = call(ops["eval"], "POST", "/api/docs/upload?filename=x.docx", raw=b"x")
check("сотрудник не может загружать документы → 403", st == 403, str(st))
docs = json.loads(call(admin, "GET", "/api/docs")[1])
check("прежняя редакция утратила силу", next(d for d in docs if d["id"] == presale["id"])["status"] == "superseded")
if uploaded:
    cloud = next(d for d in docs if d["id"] == uploaded[0])
    check("подразделение нового документа — «Дирекция облачных решений»", cloud["department"] == "Дирекция облачных решений",
          str(cloud["department"]))
    st, text = call(admin, "GET", f"/api/docs/{uploaded[0]}")
    nos = [c["clause_no"] for c in json.loads(text)]
    check("вступление сохранено, пункты из автонумерации Word: 1.1–3.3, включая 3.1.1",
          nos == [None, "1.1", "1.2", "2.1", "2.2", "2.3", "3.1", "3.1.1", "3.2", "3.3"], str(nos))

# 3. Ответы агента по загруженным документам.
if "--no-ask" not in sys.argv and uploaded:
    emp = ops["eval"]
    cases = [
        ("За сколько дней до нужной даты РП должен создать заявку на закупку облачных ресурсов в Дирекции облачных решений?",
         "answer", ["10"]),
        ("Что приложить к заявке на закупку облачных ресурсов в Дирекции облачных решений?", "answer", ["500"]),
        ("В течение какого срока ПА должен принять заявку на пресейл в работу в Дирекции ИБ?", "answer", ["2 рабоч"]),
    ]
    for q, expect, must in cases:
        r = ask(emp, q)
        cites = [(c.get("source"), c.get("clause_no")) for c in r.get("citations", [])]
        ok = r.get("status") == expect and all(m in (r.get("answer") or "") for m in must)
        check(f"агент: «{q[:70]}…» → {expect} {must}", ok, f"{r.get('status')}: {r.get('answer')} {cites}")
        print(f"     {r.get('latency_ms', 0) / 1000:.0f} с, {cites}")
    # Продолжение темы и подразделение из профиля.
    r1 = ask(emp, "Кто назначает руководителя проекта в Дирекции ИБ?")
    r2 = ask(emp, "А в ДБРЦТ?", r1.get("dialog_id"))
    check("продолжение темы «А в ДБРЦТ?» → ответ по ДБРЦТ", r2.get("status") == "answer"
          and any("ДБРЦТ" in (c.get("source") or "") for c in r2.get("citations", [])), f"{r2.get('status')}: {r2.get('answer')}")
    r3 = ask(session("emp.ib"), "Кто назначает руководителя проекта?")
    check("подразделение из профиля (Дирекция ИБ) — без уточнения", r3.get("status") == "answer" and r3.get("scope_note"),
          f"{r3.get('status')}: {r3.get('answer')}")
    # Повторный вопрос: второй раз — мгновенно, тем же проверенным ответом.
    q5 = "Кто назначает руководителя проекта в Дирекции ИБ?"
    ask(emp, q5, nocache=False)
    t0 = time.time()
    r5 = ask(emp, q5, nocache=False)
    check("повторный вопрос — мгновенный ответ из журнала", r5.get("from_cache") and time.time() - t0 < 10,
          f"{r5.get('status')} from_cache={r5.get('from_cache')} {time.time() - t0:.1f} с")
    r4 = ask(emp, "Где найти регламент пресейла Дирекции ИБ?")
    check("«где найти регламент» → название документа", r4.get("status") == "answer" and "Пресейл" in (r4.get("answer") or ""),
          f"{r4.get('status')}: {r4.get('answer')}")

# 4. Удаление загруженного: прежняя редакция снова действует.
if "--keep" not in sys.argv:
    for doc_id in uploaded:
        st, _ = call(admin, "DELETE", f"/api/docs/{doc_id}")
        check(f"удаление загруженного документа {doc_id} → 200", st == 200, str(st))
    docs = json.loads(call(admin, "GET", "/api/docs")[1])
    check("после удаления прежняя редакция снова действует",
          next(d for d in docs if d["id"] == presale["id"])["status"] == "in_force")
    st, _ = call(admin, "DELETE", f"/api/docs/{presale['id']}")
    check("исходный регламент удалить нельзя → 403", st == 403, str(st))

print(f"\nИтого ошибок: {len(fails)}" + (": " + "; ".join(fails) if fails else ""))
