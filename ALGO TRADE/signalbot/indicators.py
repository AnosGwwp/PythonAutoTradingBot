"""Indicators on plain float lists. Output lists align with input (NaN during warm-up)."""
from __future__ import annotations

import math

NAN = float("nan")


def ema(xs: list[float], n: int) -> list[float]:
    out, k, e = [NAN] * len(xs), 2 / (n + 1), None
    for i, x in enumerate(xs):
        if i == n - 1:
            e = sum(xs[:n]) / n
        elif i >= n:
            e = x * k + e * (1 - k)
        if e is not None:
            out[i] = e
    return out


def sma(xs: list[float], n: int) -> list[float]:
    out, s = [NAN] * len(xs), 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def _wilder(xs: list[float], n: int, start: int = 0) -> list[float]:
    out, v = [NAN] * len(xs), None
    for i in range(start, len(xs)):
        if i - start == n - 1:
            v = sum(xs[start:i + 1]) / n
        elif i - start >= n:
            v = (v * (n - 1) + xs[i]) / n
        if v is not None:
            out[i] = v
    return out


def rsi(c: list[float], n: int = 14) -> list[float]:
    gains = [0.0] + [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
    losses = [0.0] + [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
    ag, al = _wilder(gains, n, 1), _wilder(losses, n, 1)
    out = [NAN] * len(c)
    for i in range(len(c)):
        if not math.isnan(ag[i]):
            out[i] = 100.0 if al[i] == 0 else 100 - 100 / (1 + ag[i] / al[i])
    return out


def true_range(h, l, c) -> list[float]:
    return [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
                            for i in range(1, len(c))]


def atr(h, l, c, n: int = 14) -> list[float]:
    return _wilder(true_range(h, l, c), n)


def adx(h, l, c, n: int = 14) -> list[float]:
    pdm = [0.0] + [max(h[i] - h[i - 1], 0) if h[i] - h[i - 1] > l[i - 1] - l[i] else 0.0
                   for i in range(1, len(c))]
    ndm = [0.0] + [max(l[i - 1] - l[i], 0) if l[i - 1] - l[i] > h[i] - h[i - 1] else 0.0
                   for i in range(1, len(c))]
    tr = _wilder(true_range(h, l, c), n, 1)
    sp, sn = _wilder(pdm, n, 1), _wilder(ndm, n, 1)
    dx = [NAN] * len(c)
    for i in range(len(c)):
        if not math.isnan(tr[i]) and tr[i] > 0:
            pdi, ndi = 100 * sp[i] / tr[i], 100 * sn[i] / tr[i]
            dx[i] = 0.0 if pdi + ndi == 0 else 100 * abs(pdi - ndi) / (pdi + ndi)
    first = next((i for i, x in enumerate(dx) if not math.isnan(x)), len(dx))
    return _wilder([0.0 if math.isnan(x) else x for x in dx], n, first)
