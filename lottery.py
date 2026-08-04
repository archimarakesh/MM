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
DAYS = int(os.getenv("LOTTERY_DAYS", "14") or 14)


def _parse_prizes(raw: str) -> list[int]:
    out = []
    for p in str(raw or "").split(","):
        p = p.strip()
        if p.isdigit():
            out.append(int(p))
    return out


# призовые места сверху вниз (1-е, 2-е, 3-е …)
PRIZES = _parse_prizes(os.getenv("LOTTERY_PRIZES", "5000,2500,1000")) or [5000, 2500, 1000]
PRIZES_STR = ",".join(map(str, PRIZES))


def tickets_for(confirmed: int) -> int:
    """Сколько чисел даёт указанное число подтверждённых рефералов."""
    return max(0, int(confirmed) // PER_TICKET)


def pick_winners(pool: list, k: int, rng) -> list:
    """Из списка билетов вытянуть до k выигрышных (числа не повторяются).
    rng — random.Random (детерминируется в тестах)."""
    nums = list(pool)
    rng.shuffle(nums)
    return nums[:min(int(k), len(nums))]
