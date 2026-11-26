"""Почта службы качества (кейсы 1 и 3 так, как их описал заказчик):
- агент отправляет письмо на адрес службы качества, сотрудник — в копии (SMTP);
- служба качества отвечает «ответить всем» обычным письмом; агент забирает ответ из своего ящика (IMAP),
  отмечает запрос отвеченным и добавляет ответ в базу разъяснений — следующий такой вопрос получает ответ.
Письма связываются с запросом по метке [Регламенты #N] в теме. Без настроек SMTP/IMAP модуль ничего не делает."""
import email
import imaplib
import re
import smtplib
import threading
import time
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr

from . import config, kb

TAG = re.compile(r"\[Регламенты #(\d+)\]")
# Промежуточный ответ («Переадресовано в отдел кадров…», «не по адресу») — не разъяснение: в базу не кладём,
# запрос остаётся открытым (срок ответа продолжает идти) до окончательного ответа.
REDIRECT = re.compile(r"^\W*переадресован", re.I)
QUOTE_HEADER = re.compile(r"^.{0,160}(писал|пишет|wrote).*:\s*$", re.I)


def subject_for(ticket_id: int, question: str) -> str:
    # Только первая строка: к вопросу бывают дописаны уточнения/оговорки с новой строки, а перевод строки
    # в заголовке письма — ValueError при отправке (письмо не уходило бы молча).
    q = " ".join(question.split("\n", 1)[0].split())
    q = q if len(q) <= 70 else q[:70] + "…"
    return f"[Регламенты #{ticket_id}] Вопрос без ответа в регламентах: {q}"


def send(to: str, subject: str, body: str, cc: str | None = None) -> bool:
    if not (config.SMTP_HOST and config.MAIL_USER and config.MAIL_PASS):
        return False
    m = EmailMessage()
    m["From"] = f"ИИ-агент по регламентам <{config.MAIL_USER}>"
    m["To"] = to
    if cc:
        m["Cc"] = cc
    m["Reply-To"] = ", ".join(x for x in (config.MAIL_USER, cc) if x)
    m["Subject"] = subject
    m.set_content(body)
    with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as s:
        s.login(config.MAIL_USER, config.MAIL_PASS)
        s.send_message(m)
    return True


def _text(msg) -> str:
    """Текст ответа службы качества без цитаты исходного письма. Работает и для ответа над цитатой,
    и для ответа под цитатой (так по умолчанию отвечает веб-почта Reg.ru/Roundcube)."""
    part = None
    for p in (msg.walk() if msg.is_multipart() else [msg]):
        if p.get_content_type() == "text/plain" and not p.get_filename():
            part = p
            break
    if part is None:
        return ""
    raw = part.get_payload(decode=True) or b""
    text = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
    paras, cur = [], []
    for line in text.splitlines():
        s_ = line.strip()
        if s_.startswith(">") or QUOTE_HEADER.match(s_) or s_ in ("--", "-- "):
            continue
        if re.match(r"^-{2,}\s*(Original|Исходное)", s_, re.I):
            break
        if not s_:
            if cur:
                paras.append(" ".join(cur))
                cur = []
            continue
        cur.append(s_)
    if cur:
        paras.append(" ".join(cur))
    return "\n".join(paras).strip()


def poll_once() -> int:
    """Забрать непрочитанные ответы службы качества. Возвращает число обработанных писем."""
    if not (config.IMAP_HOST and config.MAIL_USER and config.MAIL_PASS):
        return 0
    n = 0
    with imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT) as im:
        im.login(config.MAIL_USER, config.MAIL_PASS)
        im.select("INBOX")
        _, ids = im.search(None, "UNSEEN")
        for mid in ids[0].split():
            _, data = im.fetch(mid, "(RFC822)")
            msg = email.message_from_bytes(data[0][1])
            subject = str(make_header(decode_header(msg.get("Subject", ""))))
            sender = parseaddr(msg.get("From", ""))[1].lower()
            m = TAG.search(subject)
            if not m or sender == config.MAIL_USER.lower():
                continue
            answer = _text(msg)
            if answer and REDIRECT.match(answer):
                kb.note_redirect(int(m.group(1)), answer, sender)
                n += 1
            elif answer:
                try:
                    kb.add_answer(int(m.group(1)), answer, f"Служба качества ({sender})")
                    n += 1
                except KeyError:
                    pass
    return n


def start_poller(interval: int = 30):
    if not config.IMAP_HOST:
        return

    def loop():
        while True:
            try:
                poll_once()
            except Exception as e:  # сеть/почта недоступна — пробуем в следующий раз
                print(f"[mailbox] {type(e).__name__}: {e}", flush=True)
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True, name="mailbox-poller").start()
