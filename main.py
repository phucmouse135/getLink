"""
main.py
=======
Entry point for the Check-Mail tool.

Usage
-----
    python main.py input.txt
    python main.py accounts.csv
    python main.py input.txt --workers 10
    python main.py input.txt --subject "Votre mot de passe a été modifié"

Input format (txt)
------------------
    user@gmx.com|password
    user@gmx.com:password
    user@gmx.com password

Input format (csv)
------------------
    email,password
    user@gmx.com,secret

Output
------
    results/found.txt       — accounts where a password-change email was found
    results/not_found.txt   — accounts where no such email exists
    results/errors.txt      — accounts that failed (auth error / network)
    logs/checkmail.log      — full debug log (rotating)
    errors/<account>.txt    — traceback dumps for unexpected errors
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple

import config
from core import check_one_account
from utils import Counter, log, parse_input_file, setup_logger


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="checkmail",
        description="IMAP password-change email checker (GMX / Facebook / Meta)",
    )
    parser.add_argument(
        "input_file",
        help="Path to txt or csv file containing email|password pairs.",
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=config.MAX_WORKERS,
        metavar="N",
        help=f"Parallel workers (default: {config.MAX_WORKERS}).",
    )
    parser.add_argument(
        "--subject", "-s",
        type=str,
        default=config.TARGET_SUBJECT,
        help=(
            f"Target subject to search for (default: {config.TARGET_SUBJECT!r}). "
            "Any language — will be auto-translated to English for comparison."
        ),
    )
    return parser.parse_args()


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main() -> int:
    """
    Orchestrates the full run:
      1. Parse CLI args.
      2. Load input credentials.
      3. Run per-account checks in a thread pool.
      4. Print a summary table.

    Returns exit code: 0 = at least one account found; 1 = none found / errors.
    """
    args = _parse_args()

    # Apply overrides from CLI
    if args.subject != config.TARGET_SUBJECT:
        config.TARGET_SUBJECT = args.subject
        log.info("[INFO]  Target subject overridden → %r", config.TARGET_SUBJECT)

    workers = max(1, min(args.workers, 20))  # hard cap at 20

    # ── Load credentials ───────────────────────────────────────
    log.info("[INFO]  Loading credentials from: %s", args.input_file)
    try:
        accounts: List[Tuple[str, str]] = list(parse_input_file(args.input_file))
    except FileNotFoundError as exc:
        log.error("[ERROR] %s", exc)
        return 1

    if not accounts:
        log.error("[ERROR] No valid credentials found in input file.")
        return 1

    total = len(accounts)
    log.info("[INFO]  %d account(s) loaded. Workers: %d", total, workers)
    log.info("[INFO]  Target subject: %r", config.TARGET_SUBJECT)
    log.info("[INFO]  Results → %s", config.RESULTS_DIR)
    log.info("=" * 60)

    # ── Thread pool ────────────────────────────────────────────
    counters = {"found": Counter(), "not_found": Counter(), "error": Counter()}
    done     = Counter()

    start_ts = time.perf_counter()

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="chk") as pool:
        futures = {
            pool.submit(check_one_account, em, pw): em
            for em, pw in accounts
        }

        for future in as_completed(futures):
            account = futures[future]
            n = done.increment()

            try:
                result = future.result()
                status = result["status"]
            except Exception as exc:  # noqa: BLE001
                # Should never reach here because check_one_account never raises,
                # but guard defensively.
                log.error("[ERROR] Unhandled exception for %s: %s", account, exc)
                status = "error"

            counters[status].increment()
            elapsed = time.perf_counter() - start_ts
            avg     = elapsed / n if n else 0
            eta     = avg * (total - n)

            log.info(
                "[INFO]  [%d/%d] %-40s → %-9s | ETA %.0fs",
                n, total, account, status.upper(), eta,
            )

    # ── Summary ────────────────────────────────────────────────
    elapsed_total = time.perf_counter() - start_ts
    _print_summary(
        total         = total,
        found         = counters["found"].value,
        not_found     = counters["not_found"].value,
        errors        = counters["error"].value,
        elapsed       = elapsed_total,
    )

    return 0 if counters["found"].value > 0 else 1


def _print_summary(
    total: int,
    found: int,
    not_found: int,
    errors: int,
    elapsed: float,
) -> None:
    sep = "═" * 55
    log.info(sep)
    log.info("  CHECK-MAIL SUMMARY")
    log.info(sep)
    log.info("  Total accounts  : %d", total)
    log.info("  ✔  Found        : %d", found)
    log.info("  ✘  Not found    : %d", not_found)
    log.info("  ⚠  Errors       : %d", errors)
    log.info("  ⏱  Elapsed      : %.1fs  (avg %.2fs/acc)",
             elapsed, elapsed / total if total else 0)
    log.info(sep)
    log.info("  Output files:")
    log.info("    Found     → %s", config.RESULT_FOUND_FILE)
    log.info("    Not found → %s", config.RESULT_NOT_FOUND_FILE)
    log.info("    Errors    → %s", config.RESULT_ERROR_FILE)
    log.info("    Full log  → %s", config.LOG_FILE)
    log.info(sep)


# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    setup_logger()          # ensure handlers are registered before any output
    sys.exit(main())
