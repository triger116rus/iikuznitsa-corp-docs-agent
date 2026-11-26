"""Вызовы локальных моделей через llama-server (OpenAI-совместимый API). Внешних API нет."""
import json
import re
import time

import httpx

from . import config

_client = httpx.Client(timeout=300)


class Usage:
    """Счётчик токенов и вызовов на один запрос пользователя (для журнала и калькулятора)."""

    def __init__(self):
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.ms = 0

    def as_dict(self):
        return {"llm_calls": self.calls, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out, "llm_ms": self.ms}


def chat_json(system: str, user: str, usage: Usage | None = None, max_tokens: int = 400, schema: dict | None = None,
              slot: int | None = None) -> dict:
    """Один вызов LLM, ответ — JSON. Thinking выключен, temperature 0, системный промпт кэшируется."""
    body = {
        "model": config.LLM_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
        "cache_prompt": True,
    }
    if slot is not None:  # свой слот на каждый тип вызова → системный промпт остаётся в кэше слота
        body["id_slot"] = slot
    if schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}}
    else:
        body["response_format"] = {"type": "json_object"}
    t0 = time.time()
    r = _client.post(config.LLM_URL + "/v1/chat/completions", json=body)
    r.raise_for_status()
    data = r.json()
    if usage is not None:
        u = data.get("usage", {})
        usage.calls += 1
        usage.tokens_in += u.get("prompt_tokens", 0)
        usage.tokens_out += u.get("completion_tokens", 0)
        usage.ms += int((time.time() - t0) * 1000)
    text = data["choices"][0]["message"]["content"]
    return parse_json(text)


def parse_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        try:
            return json.loads(m.group(0)) if m else {}
        except json.JSONDecodeError:   # оборванный вывод (лимит токенов) — пусто, агент не отвечает от себя
            return {}


def embed(texts: list[str]) -> list[list[float]]:
    out = []
    for i in range(0, len(texts), 16):
        r = _client.post(config.EMBED_URL + "/v1/embeddings", json={"model": config.EMBED_MODEL, "input": texts[i:i + 16]})
        r.raise_for_status()
        out.extend(d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"]))
    return out


def stream_json(system: str, user: str, usage: Usage | None = None, max_tokens: int = 400, schema: dict | None = None,
                slot: int | None = None):
    """То же, что chat_json, но потоком: выдаёт ("delta", накопленный_текст) по мере генерации и ("done", dict).
    Нужен, чтобы сотрудник видел ответ сразу, а не после окончания генерации."""
    body = {
        "model": config.LLM_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0, "max_tokens": max_tokens, "stream": True, "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": False}, "cache_prompt": True,
        "response_format": ({"type": "json_schema", "json_schema": {"name": "out", "schema": schema}} if schema
                            else {"type": "json_object"}),
    }
    if slot is not None:
        body["id_slot"] = slot
    t0 = time.time()
    text, u = "", {}
    with _client.stream("POST", config.LLM_URL + "/v1/chat/completions", json=body) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data: ") or line.strip() == "data: [DONE]":
                continue
            d = json.loads(line[6:])
            if d.get("usage"):
                u = d["usage"]
            for ch in d.get("choices", []):
                piece = (ch.get("delta") or {}).get("content")
                if piece:
                    text += piece
                    yield "delta", text
    if usage is not None:
        usage.calls += 1
        usage.tokens_in += u.get("prompt_tokens", 0)
        usage.tokens_out += u.get("completion_tokens", 0)
        usage.ms += int((time.time() - t0) * 1000)
    yield "done", parse_json(text)
