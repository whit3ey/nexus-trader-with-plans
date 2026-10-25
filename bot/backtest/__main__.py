"""python -m bot.backtest [--days 60 | --csv FILE | --synthetic 4000] [--step 3] [--equity 10000] [--rr 2.0]"""
import argparse
import json
import sys
import time
from pathlib import Path

from ..config import ConfigError, PROJECT_ROOT, load_config
from .engine import Backtester, download_history, format_result, load_csv, save_csv, synthetic_candles


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bot.backtest", description="Backtest the deterministic strategy + risk layer")
    ap.add_argument("--days", type=int, default=30, help="days of real BTCUSDT 5M history to download (default 30)")
    ap.add_argument("--csv", help="use a CSV file (ts,open,high,low,close,volume; ts in ms) instead of downloading")
    ap.add_argument("--synthetic", type=int, metavar="N", help="use N synthetic candles (demo only, not market data)")
    ap.add_argument("--step", type=int, default=3, help="evaluate every N candles (1 = every candle, slower)")
    ap.add_argument("--equity", type=float, default=10_000.0)
    ap.add_argument("--rr", type=float, default=None)
    ap.add_argument("--config", default=None)
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    synthetic = False
    if a.synthetic:
        candles, synthetic = synthetic_candles(a.synthetic), True
    elif a.csv:
        candles = load_csv(a.csv)
    else:
        cache = PROJECT_ROOT / "data" / "history" / f"{cfg.symbol}_5m_{a.days}d.csv"
        print(f"Downloading {a.days} days of {cfg.symbol} 5M candles from Bybit (public data) ...")
        try:
            candles = download_history(cfg.symbol, a.days)
        except Exception as e:
            print(f"Download failed: {e}\nCheck your internet connection, or use --csv FILE / --synthetic 4000 for a demo.")
            return 1
        save_csv(cache, candles)
        print(f"Saved {len(candles)} candles to {cache}")
    if len(candles) < 1000:
        print(f"Need at least 1000 candles (~3.5 days); got {len(candles)}.")
        return 1
    print(f"Running on {len(candles)} candles (step {a.step}) ... this can take a few minutes.")
    t0 = time.time()
    res = Backtester(cfg, candles, a.equity, a.step, a.rr, synthetic=synthetic).run()
    print(format_result(res))
    out = PROJECT_ROOT / "data" / "backtests"
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"backtest_{int(time.time())}.json"
    f.write_text(json.dumps({"metrics": res.metrics, "synthetic": synthetic, "trades": [t.__dict__ for t in res.trades]}, indent=1, default=str))
    print(f"\nTrades saved to {f}   (took {time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
