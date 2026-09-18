# -*- coding: utf-8 -*-
"""Розыгрыш (лотерея Magic Market) — конфигурация и чистая логика.

Механика:
  • Участник подписан на канал и чат (проверяет бот) и приводит рефералов,
    которые тоже подписываются на канал и чат. Каждые PER_TICKET (3)
    подтверждённых реферала дают участнику одно «число» (билет) в текущем круге.
  • Рефералы запоминаются НАВСЕГДА: одного и того же приглашённого нельзя
    использовать в двух розыгрышах — пара referrer→referral уникальна глобально.
  • В момент дедлайна сервер тянет случайные выигрышные числа: приз за каждое
    число (у кого несколько чисел — может занять несколько мест).

Всё в UAH (₴). Значения переопределяются через env.
"""
import os

# сколько подтверждённых рефералов даёт одно число (билет)
PER_TICKET = int(os.getenv("LOTTERY_PER_TICKET", "3") or 3)
# длительность одного круга в днях (таймер)
DAYS = int(os.getenv("LOTTERY_DAYS", "7") or 7)

# ── автоцикл: неделя розыгрыша / неделя перерыва (стартует сам, без админки) ──
AUTO = os.getenv("LOTTERY_AUTO", "1") != "0"
ROUND_DAYS = int(os.getenv("LOTTERY_ROUND_DAYS", "7") or 7)   # активная неделя
BREAK_DAYS = int(os.getenv("LOTTERY_BREAK_DAYS", "7") or 7)   # неделя перерыва


def _parse_prizes(raw: str) -> list[int]:
    out = []
    for p in str(raw or "").split(","):
        p = p.strip()
        if p.isdigit():
            out.append(int(p))
    return out


# призовые места сверху вниз (1-е, 2-е, 3-е …). Фонд 8000 ₴ на 5 мест.
PRIZES = _parse_prizes(os.getenv("LOTTERY_PRIZES", "3000,2000,1500,1000,500")) \
    or [3000, 2000, 1500, 1000, 500]
PRIZES_STR = ",".join(map(str, PRIZES))
# призы автоцикла (по умолчанию те же 8000 ₴ на 5 мест)
AUTO_PRIZES = _parse_prizes(os.getenv("LOTTERY_AUTO_PRIZES", "")) or list(PRIZES)


def tickets_for(confirmed: int) -> int:
    """Сколько чисел даёт указанное число подтверждённых рефералов."""
    return max(0, int(confirmed) // PER_TICKET)


def pick_winners(pool: list, k: int, rng) -> list:
    """Из списка билетов вытянуть до k выигрышных (числа не повторяются).
    rng — random.Random (детерминируется в тестах)."""
    nums = list(pool)
    rng.shuffle(nums)
    return nums[:min(int(k), len(nums))]
