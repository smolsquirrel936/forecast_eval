# -*- coding: utf-8 -*-
"""C4 / W2: re-derive ROLL_AT per session from the raw TAIFEX daily files.

The RPT minute files and TXF_RPT_ticks.csv are both front-month-continuous --
one contract per minute (measured 2026-08-23, real_data.load_minute_bars). The
incoming month is simply absent from them, so the two-sided liquidity question
C4 asks cannot be answered there. The raw TAIFEX `Daily_*.rpt` files carry every
listed month and are the only source that can.
"""
import sys, csv, io
sys.path.insert(0, "D:/Users/squirrel/Documents/paper/myPaper/forecast_eval_folder/night")
from collections import defaultdict
from datetime import timedelta, date
from forecast_eval.live.roll import last_trading_day, ROLL_LEAD_DAYS

RAW = "D:/Users/squirrel/Documents/paper/myPaper/dataset/RPT/extracted_data"
EXPIRIES = ["202606", "202607", "202608"]

def nxt(m):
    y, mm = int(m[:4]), int(m[4:])
    return "%04d%02d" % ((y + 1, 1) if mm == 12 else (y, mm + 1))

def read_day(d, extra_days=(0,)):
    """TX rows for trading_date d: (month, hhmmss, qty).

    A trading date's NIGHT session (15:00 -> 05:00) is filed in the *next*
    calendar day's Daily_*.rpt, so the night measurement must read both files
    and filter on trading_date rather than on the filename."""
    out = []
    for k in extra_days:
        f = d + timedelta(days=k)
        path = "%s/Daily_%04d_%02d_%02d.rpt" % (RAW, f.year, f.month, f.day)
        try:
            out.extend(_scan(path, d))
        except FileNotFoundError:
            pass
    return out


def _scan(path, d):
    out = []
    with io.open(path, encoding="latin-1") as fh:
        next(fh)
        for line in fh:
            p = line.split(",")
            if len(p) < 6:
                continue
            if p[0].strip() != d.strftime("%Y%m%d"):
                continue
            if p[1].strip() != "TX":
                continue
            out.append((p[2].strip(), p[3].strip(), int(p[5])))
    return out

def bucket_of(hhmmss, open_min):
    t = int(hhmmss)
    m = (t // 10000) * 60 + (t // 100) % 100
    off = m - open_min
    if off < 0:
        off += 24 * 60
    return off // 30

def measure(open_hm, span_min, label, extra_days=(0,)):
    open_min = open_hm[0] * 60 + open_hm[1]
    agg = defaultdict(lambda: [0, 0])   # bucket -> [expiring, incoming]
    per_date = {}
    for m in EXPIRIES:
        d = last_trading_day(m) - timedelta(days=ROLL_LEAD_DAYS)
        exp, inc = m, nxt(m)
        rows = read_day(d, extra_days)
        seen = defaultdict(lambda: [0, 0])
        for mon, hhmmss, q in rows:
            b = bucket_of(hhmmss, open_min)
            if b * 30 >= span_min:
                continue
            if mon == exp:
                agg[b][0] += q; seen[b][0] += q
            elif mon == inc:
                agg[b][1] += q; seen[b][1] += q
        per_date[d] = (exp, inc, dict(seen))
    print("\n=== %s ===" % label)
    print("  roll dates: %s (last trading day - %d)" %
          (", ".join(str(d) for d in sorted(per_date)), ROLL_LEAD_DAYS))
    print("  %-8s %11s %11s %11s %11s %7s" %
          ("clock", "expiring", "incoming", "both", "thinner", "share"))
    tot = sum(v[0] + v[1] for v in agg.values())
    best_thin, best_thin_b = -1, None
    for b in sorted(agg):
        e, i = agg[b]
        thin = min(e, i)
        mm = open_min + b * 30
        lbl = "%02d:%02d" % ((mm // 60) % 24, mm % 60)
        if thin > best_thin:
            best_thin, best_thin_b = thin, b
        print("  %-8s %11d %11d %11d %11d %6.1f%%" % (lbl, e, i, e + i, thin,
                                                      100.0 * (e + i) / tot))
    # Tech: best_thin_b stays None when `agg` is empty -- every Daily_*.rpt
    #       read failed (read_day swallows FileNotFoundError per file), so no
    #       bucket saw any volume on either delivery month.
    # Why:  the suite's static name check imports this module, and until
    #       2026-10-11 the import crashed here with a TypeError on machines
    #       where RAW does not exist. An empty survey is a legible answer; a
    #       crash on import is not.
    if best_thin_b is None:
        print("  -> no rows matched any bucket (no raw TAIFEX files under %s?)"
              % RAW)
        return agg
    mm = open_min + best_thin_b * 30
    print("  -> busiest by THINNER leg (the binding side of a two-order roll): "
          "%02d:%02d" % ((mm // 60) % 24, mm % 60))
    return agg


if __name__ == "__main__":
    # Tech: the two survey calls run only when the file is executed directly.
    # Why:  module-level execution ran the full measurement on *import* -- the
    #       undefined-globals test imports every module, and on a machine
    #       without the D: raw files that import crashed (empty agg -> None
    #       best_thin_b). The docs page's run-by-hand command is unchanged.
    measure((15, 0), 14 * 60, "NIGHT roll day 15:00-04:59  [validation]", extra_days=(0, 1))
    measure((8, 45), 5 * 60, "DAY roll day 08:45-13:45  [the C4 answer]")
