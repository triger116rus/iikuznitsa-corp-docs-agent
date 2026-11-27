"""Почта службы качества (кейсы 1 и 3 так, как их описал заказчик):
- агент отправляет письмо на адрес службы качества, сотрудник — в копии (SMTP);
- служба качества отвечает «ответить всем» обычным письмом; агент забирает ответ из своего ящика (IMAP),
  отмечает запрос отвеченным и добавляет ответ в базу разъяснений — следующий такой вопрос получает ответ.
Письма связываются с запросом по метке [Регламенты #N] в теме. Без настроек SMTP/IMAP модуль ничего не делает."""
import os
import email
import imaplib
import re
import smtplib
import threading
import time
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr

from . import config, kb

TAG = re.compile(r"\[Регламенты #(\d+)\]")
# Промежуточный ответ («Переадресовано в отдел кадров…», «не по адресу») — не разъяснение: в базу не кладём,
# запрос остаётся открытым (срок ответа продолжает идти) до окончательного ответа.
REDIRECT = re.compile(r"^\W*переадресован", re.I)
CONFIRM = re.compile(r"^\W*подтвержда", re.I)   # служба качества подтверждает ответ владельца процесса
REVOKE = re.compile(r"^\W*отозва", re.I)        # служба качества отзывает разъяснение из базы
QUOTE_HEADER = re.compile(r"^.{0,160}(писал|пишет|wrote).*:\s*$", re.I)


def subject_for(ticket_id: int, question: str) -> str:
    # Только первая строка: к вопросу бывают дописаны уточнения/оговорки с новой строки, а перевод строки
    # в заголовке письма — ValueError при отправке (письмо не уходило бы молча).
    q = " ".join(question.split("\n", 1)[0].split())
    q = q if len(q) <= 70 else q[:70] + "…"
    return f"[Регламенты #{ticket_id}] Вопрос без ответа в регламентах: {q}"


MAIL_PER_HOUR = int(os.getenv("MAIL_PER_HOUR", "40"))        # всего писем в час со стенда
MAIL_PER_HOUR_CC = int(os.getenv("MAIL_PER_HOUR_CC", "8"))   # копий на один адрес в час
_sent: list[tuple[float, str]] = []
_sent_lock = threading.Lock()


class MailLimit(Exception):
    """Превышен лимит писем: стенд открыт в интернете, адрес копии вводит пользователь — защита ящика от рассылки."""


def _check_limit(cc: str | None):
    now = time.time()
    with _sent_lock:
        _sent[:] = [x for x in _sent if now - x[0] < 3600]
        if len(_sent) >= MAIL_PER_HOUR:
            raise MailLimit("лимит писем в час")
        if cc and sum(1 for _, a in _sent if a == cc.lower()) >= MAIL_PER_HOUR_CC:
            raise MailLimit("лимит писем на адрес копии")
        _sent.append((now, (cc or "").lower()))


def send(to: str, subject: str, body: str, cc: str | None = None) -> bool:
    if not (config.SMTP_HOST and config.MAIL_USER and config.MAIL_PASS):
        return False
    if cc and not re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+\.[a-z]{2,}", cc, re.I):
        cc = None                     # некорректный адрес копии — письмо уходит только службе качества
    _check_limit(cc)
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
            if sender and sender == kb.ticket_employee_email(int(m.group(1))):
                continue   # пишет сам сотрудник (например, «Спасибо» через «Ответить всем») — не разъяснение
            tid = int(m.group(1))
            from_quality = sender == config.QUALITY_EMAIL.lower()
            rcpt = {a.lower() for _, a in getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []))}
            try:
                if not answer:
                    continue
                if REDIRECT.match(answer):
                    kb.note_redirect(tid, answer, sender)
                elif from_quality and REVOKE.match(answer):
                    kb.revoke(tid, sender)                      # служба качества отзывает ошибочное разъяснение
                    notify_employee(tid, None, revoked=True)
                elif from_quality and CONFIRM.match(answer):
                    p = kb.pending_answer(tid)
                    if p:
                        kb.confirm_pending(tid, sender)         # служба качества подтвердила ответ владельца процесса
                        notify_employee(tid, p["a"], confirmed=True)
                    else:
                        continue                                # подтверждать нечего — «Подтверждаю» не разъяснение
                elif from_quality:
                    kb.add_answer(tid, answer, f"Служба качества ({sender})")
                    notify_employee(tid, answer, already=rcpt)  # не нажали «Ответить всем» — сотрудник узнает от агента
                else:
                    # Ответ владельца процесса (письмо переслала служба качества) — в базу только после подтверждения.
                    kb.note_pending(tid, answer, sender)
                    ask_confirmation(tid, answer, sender)
                n += 1
            except KeyError:
                pass
    return n


def notify_employee(tid: int, answer: str | None, already=(), confirmed: bool = False, revoked: bool = False) -> bool:
    """Письмо сотруднику, что на его запрос ответили: держать окно агента открытым не нужно.
    Не шлём, если сотрудник уже получил этот ответ (был среди получателей письма службы качества) или e-mail неизвестен."""
    c = kb.ticket_contact(tid)
    to = c["email"]
    if not to or to in already or to in (config.QUALITY_EMAIL.lower(), (config.MAIL_USER or "").lower()):
        return False
    q = " ".join(c["question"].split("\n", 1)[0].split())
    if revoked:
        subj, head = "Разъяснение отозвано", (f"Служба качества отозвала разъяснение по вашему запросу № {tid}. "
                                             "Запрос снова открыт — окончательный ответ придёт отдельным письмом.")
        body_answer = ""
    else:
        subj = "Ответ на ваш вопрос"
        head = (f"Служба качества подтвердила ответ владельца процесса на ваш запрос № {tid}." if confirmed
                else f"Служба качества ответила на ваш запрос № {tid}.")
        body_answer = f"Ответ:\n{answer}\n\n"
    body = (f"Здравствуйте!\n\n{head}\n\nВаш вопрос: «{q}»\n\n{body_answer}"
            f"{'Статус запроса' if revoked else 'Ответ'} также виден в ИИ-агенте по регламентам в разделе «Мои запросы»."
            "\n\nИИ-агент по регламентам")
    try:
        sent = send(to, f"[Регламенты #{tid}] {subj}", body)
    except Exception as e:   # почта/лимит: ответ всё равно есть в «Мои запросы»
        print(f"[mailbox] уведомление сотруднику #{tid} не отправлено: {type(e).__name__}", flush=True)
        return False
    if sent:
        kb.note_notified(tid, to)
    return sent


def ask_confirmation(tid: int, answer: str, by: str) -> None:
    """Просим службу качества подтвердить ответ владельца процесса перед добавлением в базу разъяснений."""
    body = (f"Владелец процесса ({by}) ответил на запрос № {tid}:\n\n«{answer}»\n\n"
            "Ответ добавится в базу разъяснений агента только после подтверждения службы качества.\n"
            "Если ответ верный — ответьте на это письмо словом «Подтверждаю». Если нет — ответьте своим текстом "
            "(он попадёт в базу вместо ответа владельца процесса).\n\nИИ-агент по регламентам")
    try:
        send(config.QUALITY_EMAIL, f"[Регламенты #{tid}] Ответ владельца процесса — на подтверждение", body)
    except Exception as e:
        print(f"[mailbox] подтверждение #{tid} не отправлено: {type(e).__name__}", flush=True)


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
