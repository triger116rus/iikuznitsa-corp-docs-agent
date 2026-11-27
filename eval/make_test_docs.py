"""Тестовые документы для проверки загрузки (созданы для проверки, к регламентам заказчика отношения не имеют).

1) «Регламент закупок Дирекции облачных решений (тест).docx» — подразделения нет в исходной базе; пункты пронумерованы
   многоуровневой автонумерацией Word (номеров нет в тексте абзацев), сокращения расшифрованы в тексте,
   есть ссылка на пункт и на документ вне базы.
2) «<регламент заказчика> (редакция 2, тест).docx» — новая редакция исходного регламента с одним изменённым сроком:
   после загрузки с «Заменяет редакцию» агент должен отвечать по новой редакции.

Запуск: python eval/make_test_docs.py <папка с исходными DOCX заказчика> <папка для тестовых DOCX>
"""
import copy
import pathlib
import sys

import docx
from docx.oxml import OxmlElement
from docx.oxml.ns import qn


def add_multilevel_numbering(d) -> str:
    """Многоуровневый список Word «1.», «1.1.», «1.1.1.» (как в реальных регламентах) → numId."""
    numbering = d.part.numbering_part.element
    aid = str(900)
    a = OxmlElement("w:abstractNum")
    a.set(qn("w:abstractNumId"), aid)
    for il, tmpl in enumerate(["%1.", "%1.%2.", "%1.%2.%3."]):
        lvl = OxmlElement("w:lvl")
        lvl.set(qn("w:ilvl"), str(il))
        for tag, val in (("w:start", "1"), ("w:numFmt", "decimal"), ("w:lvlText", tmpl)):
            e = OxmlElement(tag)
            e.set(qn("w:val"), val)
            lvl.append(e)
        a.append(lvl)
    numbering.insert(0, a)
    n = OxmlElement("w:num")
    nid = "900"
    n.set(qn("w:numId"), nid)
    ref = OxmlElement("w:abstractNumId")
    ref.set(qn("w:val"), aid)
    n.append(ref)
    numbering.append(n)
    return nid


def numbered(d, text, nid, ilvl, style=None):
    p = d.add_paragraph(text, style=style)
    ppr = p._p.get_or_add_pPr()
    numpr = OxmlElement("w:numPr")
    for tag, val in (("w:ilvl", str(ilvl)), ("w:numId", nid)):
        e = OxmlElement(tag)
        e.set(qn("w:val"), val)
        numpr.append(e)
    ppr.append(numpr)
    return p


def bullet(d, text):
    return d.add_paragraph(text, style="List Bullet")


def make_procurement(out: pathlib.Path):
    d = docx.Document()
    nid = add_multilevel_numbering(d)
    d.add_paragraph("Регламент закупок Дирекции облачных решений", style="Title")
    d.add_paragraph("Тестовый документ для проверки загрузки новых регламентов. Настоящий регламент определяет порядок "
                    "закупки облачных ресурсов и лицензий для проектов Дирекции облачных решений.")
    numbered(d, "Термины и сокращения", nid, 0, "Heading 1")
    numbered(d, "Менеджер по закупкам (МЗ) — сотрудник, который ведёт закупку от заявки до договора с поставщиком.", nid, 1)
    numbered(d, "Облачный архитектор (ОА) — сотрудник, который подтверждает техническую спецификацию закупки.", nid, 1)
    numbered(d, "Заявка на закупку", nid, 0, "Heading 1")
    numbered(d, "Заявку на закупку облачных ресурсов создаёт РП в системе закупок не позднее чем за 10 рабочих дней "
                "до даты, когда ресурсы нужны проекту.", nid, 1)
    numbered(d, "К заявке прикладываются:", nid, 1)
    bullet(d, "техническая спецификация, подтверждённая ОА;")
    bullet(d, "расчёт стоимости на весь срок проекта;")
    bullet(d, "согласие Владельца бюджета, если стоимость превышает 500 000 рублей.")
    numbered(d, "Если заявка подана позже срока из п. 2.1, МЗ согласует ускоренную закупку с руководителем Дирекции "
                "облачных решений.", nid, 1)
    numbered(d, "Выбор поставщика", nid, 0, "Heading 1")
    numbered(d, "МЗ запрашивает предложения не менее чем у трёх поставщиков, кроме случаев единственного поставщика.", nid, 1)
    numbered(d, "Единственный поставщик допускается только при наличии обоснования ОА.", nid, 2)
    numbered(d, "Проверка поставщика выполняется согласно Положению о проверке контрагентов.", nid, 1)
    numbered(d, "Договор с поставщиком подписывает руководитель Дирекции облачных решений.", nid, 1)
    d.core_properties.author = "test"
    d.save(out / "Регламент закупок Дирекции облачных решений (тест).docx")


def make_new_edition(src: pathlib.Path, out: pathlib.Path, old: str, new: str) -> pathlib.Path:
    """Копия регламента заказчика, в которой изменён один срок (old → new) — «новая редакция»."""
    d = docx.Document(str(src))
    hits = 0
    for p in d.paragraphs:
        if old in p.text:
            for r in p.runs:
                if old in r.text:
                    r.text = r.text.replace(old, new)
                    hits += 1
    assert hits, f"в {src.name} не найдено «{old}»"
    target = out / f"{src.stem} (редакция 2, тест).docx"
    d.save(target)
    return target


if __name__ == "__main__":
    src_dir, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    make_procurement(out)
    make_new_edition(src_dir / "Пресейловая деятельность Дирекции ИБ.docx", out,
                     "в течение 1 рабочего дня", "в течение 2 рабочих дней")
    print("готово:", *[p.name for p in out.glob("*.docx")], sep="\n  ")
