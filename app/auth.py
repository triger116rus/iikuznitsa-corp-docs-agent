"""Учётные записи и роли: сотрудник → служба качества → администратор (каждая следующая роль видит всё,
что предыдущая). Пароли — PBKDF2-SHA256 с солью; сессия — подписанная cookie (HMAC), без сторонних зависимостей.
Прототип: учётки в БД агента. В продукте — вход через корпоративный каталог (AD/SSO), роли — из групп каталога.

Командная строка (на стенде):
  python -m app.auth add <логин> "<Имя Фамилия>" <employee|quality|admin> [--email=…] [--department=…] [--out=<файл>]
  python -m app.auth reset <логин> [--out=<файл>]
Пароль генерируется и пишется в файл --out (права 600), в вывод не печатается.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time

from fastapi import HTTPException, Request

from . import config, db

ROLES = {"employee": "сотрудник", "quality": "служба качества", "admin": "администратор"}
RANK = {"employee": 0, "quality": 1, "admin": 2}
COOKIE = "corpdocs_session"
SESSION_HOURS = 12
_secret = config.SESSION_SECRET or secrets.token_hex(32)   # без секрета в окружении — сессии до перезапуска


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"pbkdf2${salt}${h}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, _h = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


def new_password() -> str:
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(12))


def sign(data: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()
    return raw + "." + hmac.new(_secret.encode(), raw.encode(), "sha256").hexdigest()


def unsign(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    raw, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(hmac.new(_secret.encode(), raw.encode(), "sha256").hexdigest(), sig):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(raw.encode()))
    except Exception:
        return None
    return data if data.get("exp", 0) > time.time() else None


PUBLIC = ("id", "login", "name", "email", "department", "role", "active", "created_at", "last_login")


def get_user(user_id: int) -> dict | None:
    with db.conn() as c:
        return c.execute(f"SELECT {', '.join(PUBLIC)} FROM users WHERE id=%s AND active", (user_id,)).fetchone()


def current_user(request: Request) -> dict | None:
    data = unsign(request.cookies.get(COOKIE))
    if not data:
        return None
    u = get_user(data["uid"])
    # Пароль сброшен или роль изменена после входа — старая сессия недействительна.
    return u if u and data.get("v") == _version(u["id"]) else None


def _version(user_id: int) -> str:
    with db.conn() as c:
        r = c.execute("SELECT pw_hash, role FROM users WHERE id=%s", (user_id,)).fetchone()
    return hashlib.sha256(f"{r['pw_hash']}|{r['role']}".encode()).hexdigest()[:12] if r else ""


def require(request: Request, role: str = "employee") -> dict:
    """Проверка прав на сервере для каждого запроса к API: 401 — не вошёл, 403 — роли не хватает."""
    if not config.AUTH:
        return {"id": None, "login": "dev", "name": "Разработчик", "email": None, "department": None, "role": "admin"}
    u = current_user(request)
    if not u:
        raise HTTPException(401, "нужно войти")
    if RANK[u["role"]] < RANK[role]:
        raise HTTPException(403, f"нужна роль «{ROLES[role]}»")
    return u


# Ограничение попыток входа: не больше 8 неудачных за 10 минут с одного адреса.
_fails: dict[str, list[float]] = {}
_fails_lock = threading.Lock()


def login(login_: str, password: str, ip: str) -> dict:
    now = time.time()
    with _fails_lock:
        recent = [t for t in _fails.get(ip, []) if now - t < 600]
        _fails[ip] = recent
        if len(recent) >= 8:
            raise HTTPException(429, "слишком много попыток входа — подождите 10 минут")
    with db.conn() as c:
        r = c.execute("SELECT id, pw_hash FROM users WHERE login=%s AND active", (login_.strip().lower(),)).fetchone()
        if not r or not check_password(password, r["pw_hash"]):
            with _fails_lock:
                _fails[ip].append(now)
            raise HTTPException(401, "неверный логин или пароль")
        c.execute("UPDATE users SET last_login=now() WHERE id=%s", (r["id"],))
    return {"uid": r["id"], "v": _version(r["id"]), "exp": int(now + SESSION_HOURS * 3600)}


def create_user(login_: str, name: str, role: str, email: str | None = None, department: str | None = None,
                password: str | None = None) -> tuple[dict, str]:
    if role not in ROLES:
        raise HTTPException(400, "роль: employee, quality или admin")
    login_ = login_.strip().lower()
    if not login_ or not login_.replace(".", "").replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, "логин — латиница, цифры, точка, дефис")
    password = password or new_password()
    with db.conn() as c:
        if c.execute("SELECT 1 FROM users WHERE login=%s", (login_,)).fetchone():
            raise HTTPException(409, "такой логин уже есть")
        u = c.execute(f"""INSERT INTO users(login,name,email,department,role,pw_hash) VALUES (%s,%s,%s,%s,%s,%s)
                          RETURNING {', '.join(PUBLIC)}""",
                      (login_, name.strip(), (email or "").strip() or None, (department or "").strip() or None,
                       role, hash_password(password))).fetchone()
    return u, password


def update_user(user_id: int, fields: dict) -> dict:
    allowed = {k: v for k, v in fields.items() if k in ("name", "email", "department", "role", "active")}
    if "role" in allowed and allowed["role"] not in ROLES:
        raise HTTPException(400, "роль: employee, quality или admin")
    with db.conn() as c:
        for k, v in allowed.items():
            c.execute(f"UPDATE users SET {k}=%s WHERE id=%s", (v if v != "" else None, user_id))
        u = c.execute(f"SELECT {', '.join(PUBLIC)} FROM users WHERE id=%s", (user_id,)).fetchone()
    if not u:
        raise HTTPException(404, "нет такого пользователя")
    return u


def reset_password(user_id: int) -> str:
    password = new_password()
    with db.conn() as c:
        r = c.execute("UPDATE users SET pw_hash=%s WHERE id=%s RETURNING id", (hash_password(password), user_id)).fetchone()
    if not r:
        raise HTTPException(404, "нет такого пользователя")
    return password


def _write_secret(path: str, line: str):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(line + "\n")


if __name__ == "__main__":
    db.init_schema()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opt = lambda name: next((a.split("=", 1)[1] for a in sys.argv if a.startswith(f"--{name}=")), None)
    out = opt("out")
    if not out:
        sys.exit("нужен --out=<файл> — пароль в вывод не печатается")
    if args[0] == "add":
        u, pw = create_user(args[1], args[2], args[3], opt("email"), opt("department"))
    elif args[0] == "reset":
        with db.conn() as c:
            uid = c.execute("SELECT id FROM users WHERE login=%s", (args[1],)).fetchone()["id"]
        pw, u = reset_password(uid), {"login": args[1], "role": ""}
    else:
        sys.exit(__doc__)
    _write_secret(out, f"{u['login']}\t{u.get('role', '')}\t{pw}")
    print(f"учётная запись {u['login']}: пароль записан в {out}")
