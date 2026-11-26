"""Глоссарий сокращений заказчика (файл data/glossary.tsv: «СОКР<TAB>расшифровка»; данные заказчика — вне репозитория).
Используется: расшифровка сокращений в эмбеддингах пунктов, вариант вопроса с сокращениями, подсказка модели."""
import os
import pathlib
import re


def load() -> dict[str, str]:
    path = pathlib.Path(os.getenv("GLOSSARY_FILE", pathlib.Path(__file__).parent.parent / "data" / "glossary.tsv"))
    terms = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "\t" in line and not line.startswith("#"):
                abbr, name = line.split("\t", 1)
                abbr, name = abbr.strip(), name.strip()
                if abbr and name and re.fullmatch(r"[A-ZА-ЯЁ0-9-]{2,6}", abbr):
                    terms[abbr] = name
    return terms


TERMS = load()
