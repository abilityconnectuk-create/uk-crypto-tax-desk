# -*- coding: utf-8 -*-
# Cryptocurrency tax calculator for UK tax rules
# (c) Nano Nano Ltd 2019

import argparse
import builtins
import io
import os
import platform
import sys
from typing import Dict, List, Tuple

import colorama
from colorama import Fore

from .audit import AuditRecords
from .audit_excel import AuditLogExcel
from .bt_types import TAX_RULES_UK_COMPANY, AssetSymbol, DisposalType, TaxRules, Year
from .config import config
from .constants import ERROR, TERMINAL_POWERSHELL_GUI, WARNING
from .exceptions import ImportFailureError
from .export_records import ExportRecords
from .holdings import Holdings
from .import_records import ImportRecords
from .price.exceptions import DataSourceApiError, DataSourceError
from .price.valueasset import ValueAsset
from .reconcile_koinly import main as reconcile_main
from .report import ReportLog, ReportPdf
from .review_queue import (
    QueueItem,
    ReviewQueueError,
    load_review_exceptions,
    review_queue_failures,
)
from .t_record import TransactionRecord
from .t_row import TransactionRow
from .tax import CalculateCapitalGains as CCG
from .tax import TaxCalculator
from .transactions import TransactionHistory
from .utils import bt_print, is_compiled
from .version import __version__
from .year_end import write_snapshot

if sys.stdout.encoding != "UTF-8":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]


def _build_version_str() -> str:
    compiled_suffix = " - compiled" if is_compiled() else ""
    return f"v{__version__}{compiled_suffix}"


def _create_arg_parser(version_str: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(conflict_handler="resolve")

    parser.add_argument(
        "filename",
        type=str,
        nargs="?",
        help="filename of transaction records, or can read CSV data from standard input",
    )

    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"{parser.prog} {version_str}",
    )
    parser.add_argument("-d", "--debug", action="store_true", help="enable debug logging")
    parser.add_argument(
        "-ty",
        "--taxyear",
        type=_validate_year,
        dest="tax_year",
        help=(
            f"tax year must be in the range "
            f"({min(CCG.CG_DATA_INDIVIDUAL)}-{max(CCG.CG_DATA_INDIVIDUAL)})"
        ),
    )
    parser.add_argument(
        "--taxrules",
        choices=[tax_rules.name for tax_rules in TaxRules],
        metavar="{UK_INDIVIDUAL, UK_COMPANY_XXX} "
        "where XXX is the month which starts the financial year, i.e. JAN, FEB, etc.",
        default=TaxRules.UK_INDIVIDUAL.name,
        type=str.upper,
        dest="tax_rules",
        help="specify tax rules to use, default: UK_INDIVIDUAL",
    )
    parser.add_argument(
        "--audit",
        dest="audit_only",
        action="store_true",
        help="audit only",
    )
    parser.add_argument(
        "--skipint",
        dest="skip_integrity",
        action="store_true",
        help="skip integrity check",
    )
    parser.add_argument(
        "--summary",
        dest="summary_only",
        action="store_true",
        help="only output the capital gains summary in the tax report",
    )
    parser.add_argument(
        "-o",
        dest="output_filename",
        type=str,
        help="specify the output filename for the PDF report",
    )
    parser.add_argument(
        "--nopdf",
        action="store_true",
        help="don't output PDF report, output report to terminal only",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="populate transaction records with price data in CSV format",
    )
    parser.add_argument(
        "--review-rows",
        choices=["conservative", "reject", "market", "disposal", "transfer"],
        default=None,
        help="how to treat unmatched and unmapped rows. conservative (default) "
        "acquires deposits at market value and leaves withdrawals on the review "
        "queue. The integrity check fails until that queue is empty or excepted. "
        "reject skips them. market treats withdrawals as transfers. "
        "disposal treats both as gifts. transfer treats both as transfers.",
    )
    parser.add_argument(
        "--review-exceptions",
        type=str,
        help="YAML list of review-queue rows that are allowed to remain. "
        "Each entry needs a reason and a tx_hash, or a date, asset and amount.",
    )
    parser.add_argument(
        "--year-end-snapshot",
        type=str,
        help="write the Section 104 pool at each 5 April (Europe/London) as JSON",
    )
    parser.add_argument(
        "--skip-holdings",
        action="store_true",
        help="do not value current holdings. The tax report is written without that lookup.",
    )
    return parser


def _init_terminal() -> None:
    if config.terminal == TERMINAL_POWERSHELL_GUI:
        colorama.init(strip=False)
        builtins.print = bt_print  # type: ignore[assignment]
    else:
        colorama.init()


def _print_debug_info(prog: str, version_str: str, args: argparse.Namespace) -> None:
    config.debug = args.debug
    if config.debug:
        print(f"{Fore.YELLOW}{prog} {version_str}")
        print(f"{Fore.GREEN}python: v{platform.python_version()}")
        print(f"{Fore.GREEN}system: {platform.system()}, release: {platform.release()}")
        for arg in vars(args):
            print(f"{Fore.GREEN}args: {arg}: {getattr(args, arg)}")
        config.output_config(sys.stdout)


def _run(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    try:
        args.tax_rules = TaxRules[args.tax_rules]
    except KeyError as e:
        raise RuntimeError(f"Unrecognised args.tax_rules: {args.tax_rules}") from e

    if args.tax_rules in TAX_RULES_UK_COMPANY:
        config.start_of_year_month = TAX_RULES_UK_COMPANY.index(args.tax_rules) + 1
        config.start_of_year_day = 1

    if args.review_rows:
        config.config["review_rows"] = args.review_rows
    if args.review_exceptions:
        try:
            config.config["review_exception_rows"] = load_review_exceptions(args.review_exceptions)
        except ReviewQueueError as exc:
            parser.exit(message=f"{ERROR} {exc}\n")
    else:
        config.config["review_exception_rows"] = []

    try:
        transaction_records = _do_import(args.filename)
        if not _warn_review_rows():
            parser.exit(1)
    except IOError:
        parser.exit(message=f"{ERROR} File could not be read: {args.filename}\n")
    except ImportFailureError:
        parser.exit()

    if args.export:
        _do_export(transaction_records)
        parser.exit()

    audit = AuditRecords(transaction_records)

    if args.audit_only:
        if audit.audit_log:
            audit_log_excel = AuditLogExcel(parser.prog, audit.audit_log)
            audit_log_excel.write_excel()

        if args.nopdf:
            ReportLog(args, audit)
        else:
            ReportPdf(parser.prog, args, audit)
    else:
        try:
            tax, value_asset = _do_tax(transaction_records, args.tax_rules, args.skip_integrity)
            if not args.summary_only or args.year_end_snapshot:
                tax.process_income()
                tax.process_margin_trades()
            if args.year_end_snapshot:
                _write_year_end_snapshot(args.year_end_snapshot, tax)
            if not args.skip_integrity:
                int_passed = _do_integrity_check(audit, tax.holdings)
                _exit_if_failed(parser, int_passed)

            _do_each_tax_year(tax, args.tax_year, args.summary_only)

        except DataSourceApiError as e:
            parser.exit(message=f"{ERROR} {e} - please wait and try again\n")
        except DataSourceError as e:
            parser.exit(message=f"{ERROR} {e}\n")

        # The tax report is written before any live holdings valuation, so a
        # stalled price lookup cannot block the gains figures.
        _write_report(parser.prog, args, audit, tax, value_asset)
        if not args.summary_only and not args.skip_holdings and not args.tax_year:
            try:
                tax.calculate_holdings(value_asset)
            except (DataSourceApiError, DataSourceError) as exc:
                print(f"{WARNING} Holdings valuation skipped: {exc}")
            else:
                _write_report(parser.prog, args, audit, tax, value_asset)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "reconcile":
        reconcile_main(sys.argv[2:])
        return
    _init_terminal()
    version_str = _build_version_str()
    parser = _create_arg_parser(version_str)
    args = parser.parse_args()
    _print_debug_info(parser.prog, version_str, args)
    _run(parser, args)


def _validate_year(value: str) -> int:
    year = int(value)
    if year not in CCG.CG_DATA_INDIVIDUAL:
        raise argparse.ArgumentTypeError(
            f"tax year {year} is not supported, must be in the range "
            f"({min(CCG.CG_DATA_INDIVIDUAL)}-{max(CCG.CG_DATA_INDIVIDUAL)})",
        )

    return year


def _write_report(
    prog: str,
    args: argparse.Namespace,
    audit: AuditRecords,
    tax: TaxCalculator,
    value_asset: ValueAsset,
) -> None:
    if args.nopdf:
        ReportLog(
            args,
            audit,
            tax.tax_report,
            value_asset.price_report,
            tax.holdings_report,
            tax.missing_cost_basis,
            tax.liquid_staking,
        )
    else:
        ReportPdf(
            prog,
            args,
            audit,
            tax.tax_report,
            value_asset.price_report,
            tax.holdings_report,
            tax.missing_cost_basis,
            tax.liquid_staking,
        )


def _exit_if_failed(parser: argparse.ArgumentParser, passed: bool) -> None:
    """Leave the process with status 1 when a check has failed."""
    if not passed:
        parser.exit(1)


def _warn_review_rows() -> bool:
    count = TransactionRow.review_assumed_count
    skipped = TransactionRow.skipped_unmapped_count
    if count:
        treatment = str(config.review_rows)
        print(
            f"{WARNING} {count} unmatched or unmapped transaction(s) were treated as "
            f"{treatment}. Confirm each one before relying on this report."
        )
    if skipped and str(config.review_rows) == "conservative":
        print(
            f"{WARNING} {skipped} unmatched withdrawal(s) are on the review queue. "
            "They were not treated as transfers. The integrity check fails until "
            "the queue is empty or every leftover is in --review-exceptions."
        )
    elif skipped:
        print(
            f"{WARNING} {skipped} unmapped transaction(s) were skipped. "
            "They are not in this calculation."
        )
    spam = TransactionRow.skipped_spam_count
    if spam:
        print(
            f"{WARNING} {spam} possible-spam transaction(s) were skipped. "
            "They are listed in the possible-spam file, not in this calculation."
        )
    return _warn_review_reconciliation()


def _warn_review_reconciliation() -> bool:
    """Every unknown-type row is imported or listed. The counts must add up."""
    totals = TransactionRow.review_reconciliation()
    seen = totals["seen"]
    if not seen:
        return True
    print(
        f"{WARNING} review queue reconciliation: {seen} unknown-type row(s): "
        f"{totals['queued']} left on the tax review queue, "
        f"{totals['imported_acquisitions']} imported as acquisitions, "
        f"{totals['imported_trades']} imported as trades, "
        f"{totals['dropped']} dropped with no quantity, "
        f"{totals['failed']} failed to import. "
        f"Accounted {totals['accounted']}."
    )
    if totals["accounted"] != seen:
        print(
            f"{ERROR} review queue reconciliation does not add up "
            f"({totals['accounted']} accounted, {seen} seen)."
        )
        return False
    return True


def _do_import(filename: str) -> List[TransactionRecord]:
    TransactionRow.review_assumed_count = 0
    TransactionRow.skipped_unmapped_count = 0
    TransactionRow.skipped_spam_count = 0
    TransactionRow.skipped_ignored_count = 0
    TransactionRow.needs_review_seen = 0
    TransactionRow.review_imported_count = 0
    TransactionRow.review_imported_trade_count = 0
    TransactionRow.review_dropped_count = 0
    TransactionRow.review_failed_count = 0
    TransactionRow.review_queue = []
    import_records = ImportRecords()

    if filename:
        _, file_extension = os.path.splitext(filename)
        if file_extension == ".xlsx":
            import_records.import_excel_xlsx(filename)
        elif file_extension == ".xls":
            import_records.import_excel_xls(filename)
        else:
            with io.open(filename, newline="", encoding="utf-8") as csv_file:
                import_records.import_csv(csv_file, filename)
    else:
        import_records.import_csv(sys.stdin)

    print(
        f"{Fore.WHITE}import {'successful' if import_records.failure_cnt <= 0 else 'failure'} "
        f"(success={import_records.success_cnt}, failure={import_records.failure_cnt})"
    )

    if import_records.failure_cnt > 0:
        raise ImportFailureError

    return import_records.get_records()


def _do_tax(
    transaction_records: List[TransactionRecord], tax_rules: TaxRules, skip_integrity_check: bool
) -> Tuple[TaxCalculator, ValueAsset]:
    value_asset = ValueAsset(leave_bar=True)
    transaction_history = TransactionHistory(transaction_records, value_asset)

    tax = TaxCalculator(transaction_history.transactions, tax_rules)
    tax.pool_same_day()
    tax.match_sell(DisposalType.SAME_DAY)

    if tax_rules is TaxRules.UK_INDIVIDUAL:
        tax.match_buyback(DisposalType.BED_AND_BREAKFAST)
    elif tax_rules in TAX_RULES_UK_COMPANY:
        tax.match_sell(DisposalType.TEN_DAY)

    tax.process_section104(skip_integrity_check)
    return tax, value_asset


def _do_integrity_check(audit: AuditRecords, holdings: Dict[AssetSymbol, Holdings]) -> bool:
    int_passed = True

    if config.transfers_include:
        transfer_mismatch = _transfer_mismatches(holdings)
    else:
        transfer_mismatch = False

    pools_match = audit.compare_pools(holdings)
    queue_left = _review_queue_leftovers()

    if not pools_match or transfer_mismatch or queue_left:
        int_passed = False

    print(f"{Fore.CYAN}integrity check: {Fore.YELLOW}{'passed' if int_passed else 'failed'}")
    if queue_left:
        print(
            f"{ERROR} Integrity check failed: {len(queue_left)} review-queue "
            "withdrawal(s) are unresolved. Classify them, or list each one in "
            "--review-exceptions with a reason."
        )
        for item in queue_left[:20]:
            print(
                f"{Fore.RED}  {item.timestamp} {item.quantity} {item.asset} "
                f"'{item.wallet}' {item.tx_hash}"
            )
        if len(queue_left) > 20:
            print(f"{Fore.RED}  ... and {len(queue_left) - 20} more")

    if transfer_mismatch:
        print(
            f"{WARNING} Integrity check failed: disposal(s) detected during transfer, "
            f"turn on logging [-d] to see transactions"
        )
    elif not pools_match:
        if not config.transfers_include:
            print(
                f"{WARNING} Integrity check failed: audit does not match section 104 pools, "
                f"please check Withdrawals and Deposits for missing fees"
            )
        else:
            print(f"{ERROR} Integrity check failed: audit does not match section 104 pools")
        audit.report_failures()
    return int_passed


def _review_queue_leftovers() -> List[QueueItem]:
    exceptions = config.config.get("review_exception_rows", [])
    return review_queue_failures(TransactionRow.review_queue, exceptions)


def _write_year_end_snapshot(path: str, tax: TaxCalculator) -> None:
    payload = tax.snapshot_payload(str(config.review_rows))
    json_path = write_snapshot(path, payload)
    print(f"{Fore.WHITE}section 104 snapshot: {Fore.YELLOW}{os.path.abspath(json_path)}")
    flags = payload.get("timezone_flags", [])
    if isinstance(flags, list) and flags:
        print(
            f"{WARNING} {len(flags)} transaction(s) fall in a different tax year in "
            "UTC than in Europe/London. See the timezone flags file."
        )


def _transfer_mismatches(holdings: Dict[AssetSymbol, Holdings]) -> bool:
    return bool([asset for asset in holdings if holdings[asset].mismatches])


def _do_each_tax_year(tax: TaxCalculator, tax_year: Year, summary_only: bool) -> None:
    if tax_year:
        print(f"{Fore.CYAN}calculating tax year {config.format_tax_year(tax_year)}")

        calc_cgt = tax.calculate_capital_gains(tax_year)
        if summary_only:
            tax.tax_report[tax_year] = {"CapitalGains": calc_cgt}
        else:
            calc_income = tax.calculate_income(tax_year)
            calc_margin_trading = tax.calculate_margin_trading(tax_year)
            tax.tax_report[tax_year] = {
                "CapitalGains": calc_cgt,
                "Income": calc_income,
                "MarginTrading": calc_margin_trading,
            }
    else:
        # Calculate for all years
        for year in sorted(tax.tax_events):
            print(f"{Fore.CYAN}calculating tax year {config.format_tax_year(year)}")

            if year in CCG.CG_DATA_INDIVIDUAL:
                calc_cgt = tax.calculate_capital_gains(year)
                if summary_only:
                    tax.tax_report[year] = {"CapitalGains": calc_cgt}
                else:
                    calc_income = tax.calculate_income(year)
                    calc_margin_trading = tax.calculate_margin_trading(year)
                    tax.tax_report[year] = {
                        "CapitalGains": calc_cgt,
                        "Income": calc_income,
                        "MarginTrading": calc_margin_trading,
                    }
            else:
                print(f"{WARNING} Tax year {year} is not supported")


def _do_export(transaction_records: List[TransactionRecord]) -> None:
    value_asset = ValueAsset(leave_bar=True)
    TransactionHistory(transaction_records, value_asset)
    ExportRecords(transaction_records).write_csv()


if __name__ == "__main__":
    main()
