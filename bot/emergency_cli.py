"""python -m bot.emergency_cli [--yes]   -> close everything (reduce-only), halt trading. Used by ./stop.sh --emergency-close"""
import argparse
import sys

from .config import ConfigError, load_config
from .database.database import Database
from .execution.emergency import emergency_close
from .execution.state import HaltState
from .factory import build_exchanges
from .logging_config import setup_logging


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    try:
        cfg = load_config()
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    lg = cfg.raw["logging"]
    setup_logging(cfg.path(lg["directory"]), lg["level"], int(lg["max_bytes"]), int(lg["backup_count"]), console=False)
    if not a.yes:
        if input(f"EMERGENCY CLOSE on {cfg.display_mode} ({cfg.symbol}): cancel opening orders and close the position? Type YES: ") != "YES":
            print("Cancelled.")
            return 1
    db = Database(cfg.path(cfg.raw["database"]["path"]))
    _, ex = build_exchanges(cfg)
    if ex.simulated:
        saved = db.kv_get("paper_state")
        if saved:
            ex.load_state(saved)
    rep = emergency_close(ex, cfg.symbol, db, HaltState(db))
    if ex.simulated:
        db.kv_set("paper_state", ex.dump_state())
    for s in rep["steps"]:
        print(" -", s)
    print("RESULT:", "position closed / nothing open" if rep["ok"] else "COULD NOT CONFIRM CLOSURE - open the Bybit app and close it manually NOW")
    print("Autonomous trading is now halted. Resume from the dashboard, or: ./start.sh then press Resume.")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
