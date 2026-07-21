# -*- coding: utf-8 -*-
"""Сборка баннера победителей: на готовый фон впечатываются ники недели.

Chrome на сервере нет, поэтому фон (promo/award_base.png) рисуется заранее,
а имена — через Pillow. Координаты строк заданы в шаблоне promo/src/banner_award.html.
"""
import logging
import os

log = logging.getLogger("award-img")

BASE = os.path.join("promo", "award_base.png")
FONT = os.path.join("fonts", "Manrope.ttf")
ROWS_Y = [530, 618, 706]     # верх строки, как в шаблоне
ROW_H = 76
NAME_X = 190                 # правее медали
NAME_MAX_W = 470             # до колонки с суммой
NAME_COLOR = (232, 201, 106)


def _fit(draw, text: str, font, max_w: int) -> str:
    """Обрезает длинный ник многоточием, чтобы не наехал на сумму."""
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return (text + "…") if text else ""


def render(winners: list, out_path: str) -> str | None:
    """winners: [{'place':1,'name':...}, ...]. Возвращает путь или None."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        log.warning("Pillow недоступен — баннер победителей не собран")
        return None
    if not (os.path.exists(BASE) and os.path.exists(FONT)):
        log.warning("Нет фона или шрифта для баннера победителей")
        return None
    try:
        img = Image.open(BASE).convert("RGB")
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype(FONT, 30)
        try:
            font.set_variation_by_name("ExtraBold")   # шрифт вариативный
        except Exception:
            pass
        for w in winners[:3]:
            i = int(w.get("place", 0)) - 1
            if not 0 <= i < len(ROWS_Y):
                continue
            name = _fit(draw, str(w.get("name") or "участник"), font, NAME_MAX_W)
            draw.text((NAME_X, ROWS_Y[i] + ROW_H // 2), name,
                      font=font, fill=NAME_COLOR, anchor="lm")
        img.save(out_path, "PNG")
        return out_path
    except Exception:
        log.exception("Не удалось собрать баннер победителей")
        return None
