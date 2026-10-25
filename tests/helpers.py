import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.strategy.types import Candle

STEP_MS = 300_000


def c(i, o, h, l, cl, v=1.0, step=STEP_MS, start=0):
    return Candle(start + i * step, float(o), float(h), float(l), float(cl), float(v))


def flat(n, start_index=0, mid=100.0, half=5.0):
    """Flat candles: open == close == mid, range 2*half (so ATR == 2*half)."""
    return [c(start_index + k, mid, mid + half, mid - half, mid) for k in range(n)]
