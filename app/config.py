"""Настройки из переменных окружения (значения по умолчанию — стенд в изолированном контуре)."""
import os

DB_DSN = os.getenv("DB_DSN", "dbname=corpdocs")                      # peer-аутентификация пользователя ОС
LLM_URL = os.getenv("LLM_URL", "http://127.0.0.1:8080")              # llama-server, Qwen3-30B-A3B
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3-30b-a3b")
EMBED_URL = os.getenv("EMBED_URL", "http://127.0.0.1:8081")          # llama-server --embedding, bge-m3
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")
QUALITY_EMAIL = os.getenv("QUALITY_EMAIL", "quality@company.local")  # адрес службы качества
SMTP_HOST = os.getenv("SMTP_HOST", "")                               # пусто — письмо только в журнал (демо)
TOP_K = int(os.getenv("TOP_K", "6"))
MAX_EVIDENCE_TOKENS = int(os.getenv("MAX_EVIDENCE_TOKENS", "800"))
