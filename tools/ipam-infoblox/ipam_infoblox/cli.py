"""Command-line interface.

    ipam-infoblox inspect EXPORT            what the export holds and how it is read
    ipam-infoblox plan EXPORT -o FILE       write the Infoblox import (and --report)

Exit status: 0 when the import is clean, 1 when something must be fixed
first (conflicts, missing network views or attributes), 2 on errors.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from typing import List, Optional, Tuple

from . import __version__
from .infoblox_csv import NETMASK_STYLES, counts, write_import, write_private
from .plan import DEFAULT_COMMENT, Options, build_plan, check_template, source_names, view_for
from .reader import FIELDS, map_columns, read_table
from .records import counts_by_type, load_records
from .report import render_report, rows_left_out
from .wapi import DEFAULT_VERSION, Wapi, load_existing

USER_ENV = "INFOBLOX_USERNAME"
PASSWORD_ENV = "INFOBLOX_PASSWORD"


def _pair(text: str, what: str) -> Tuple[str, str]:
    name, sep, value = text.partition("=")
    if not sep or not name.strip() or not value.strip():
        raise argparse.ArgumentTypeError(f"{what} takes NAME=VALUE, not {text!r}")
    return name.strip(), value.strip()


def _column(text: str) -> Tuple[str, str]:
    return _pair(text, "--column")


def _ea(text: str) -> Tuple[str, str]:
    name, source = _pair(text, "--ea")
    if not (source.startswith(("column:", "text:")) or source in source_names()):
        raise argparse.ArgumentTypeError(
            f"--ea {text}: unknown source {source!r} (use a field: {', '.join(source_names())}; "
            "a column: column:HEADER; or fixed text: text:VALUE)")
    return name, source


def _add_input(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("export", metavar="EXPORT", help="the AWS IPAM export (.csv or .xlsx)")
    parser.add_argument("--sheet", help="the Excel sheet to read (default: the first with a CIDR column)")
    parser.add_argument("--column", action="append", type=_column, default=[],
                        metavar="FIELD=HEADER",
                        help=f"read FIELD from this column; fields: {', '.join(FIELDS)}")
    parser.add_argument("--keep-type", action="append", default=[], metavar="TYPE",
                        help="keep rows of this type, which would be left out (eip, eni)")
    parser.add_argument("--drop-type", action="append", default=[], metavar="TYPE",
                        help="leave out rows of this type (such as pool)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ipam-infoblox",
        description="Turn an AWS IPAM export into an Infoblox CSV import, checked read-only "
                    "against the Infoblox API.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect", help="show how the export is read")
    _add_input(inspect)

    plan = commands.add_parser("plan", help="write the Infoblox import file and a report")
    _add_input(plan)
    plan.add_argument("-o", "--output", required=True, metavar="FILE",
                      help="the Infoblox CSV import file to write")
    plan.add_argument("--report", metavar="FILE",
                      help="also write a Markdown report (what goes in, what is left out and why)")
    plan.add_argument("--network-view", default="default", metavar="VIEW",
                      help="Infoblox network view; may use fields, e.g. 'aws-{account}' "
                           "(default: default)")
    plan.add_argument("--comment", default=DEFAULT_COMMENT, metavar="TEMPLATE",
                      help=f"comment for each range (default: {DEFAULT_COMMENT!r}); fields in "
                           "braces, or {column:HEADER}")
    plan.add_argument("--ea", action="append", type=_ea, default=[], metavar="NAME=SOURCE",
                      help="set extensible attribute NAME from a field (account, region, "
                           "resource_id, ...), column:HEADER or text:VALUE; repeatable")
    plan.add_argument("--netmask-style", choices=NETMASK_STYLES, default="documented",
                      help="IPv4 netmask form: as documented (dotted for networks, prefix length "
                           "for containers), or one form for both")
    plan.add_argument("--split", action="store_true",
                      help="write containers and networks to separate files")
    grid = plan.add_argument_group("checking against Infoblox (read-only)")
    grid.add_argument("--wapi", metavar="URL",
                      help="Grid Manager URL, e.g. https://gm.example.net; compares the export "
                           "with what the Grid holds")
    grid.add_argument("--wapi-version", default=DEFAULT_VERSION, metavar="VERSION",
                      help=f"WAPI version (default: {DEFAULT_VERSION})")
    grid.add_argument("--username", help=f"Infoblox user (default: ${USER_ENV}); the password "
                                         f"comes from ${PASSWORD_ENV} or a prompt")
    grid.add_argument("--ca-bundle", metavar="FILE", help="CA certificate(s) to trust for the Grid")
    grid.add_argument("--insecure", action="store_true",
                      help="don't verify the Grid's certificate (lab use only)")
    grid.add_argument("--allow-http", action="store_true",
                      help="allow an http:// URL (sends the password in clear; lab use only)")
    grid.add_argument("--timeout", type=float, default=60, metavar="SECONDS")
    return parser


def _load(args: argparse.Namespace):
    columns = dict(args.column)
    table = read_table(args.export, args.sheet, columns.get("cidr"))
    map_columns(table, columns)
    records, issues = load_records(table, args.keep_type, args.drop_type)
    return table, records, issues


def _inspect(args: argparse.Namespace) -> int:
    table, records, issues = _load(args)
    print(f"{table.path}" + (f", sheet {table.sheet!r}" if table.sheet else "")
          + f": header on row {table.header_row}, {len(table.rows)} data rows"
          + (f", read as {table.encoding}" if table.encoding not in ("", "utf-8") else ""))
    print("columns:")
    for name in FIELDS:
        print(f"  {name:18} {table.columns.get(name, '(not found)')}")
    unused = [h for h in table.headers if h not in table.columns.values()]
    if unused:
        print("other columns (usable as column:HEADER): " + ", ".join(unused))
    print("ranges by type: " + (", ".join(f"{k} {v}" for k, v in counts_by_type(records).items())
                                or "none"))
    versions = {r.cidr.version for r in records}
    print(f"IPv4 {sum(r.cidr.version == 4 for r in records)}, IPv6 "
          f"{sum(r.cidr.version == 6 for r in records)}" if versions else "no usable CIDRs")
    for action in ("changed", "dropped"):
        rows = [i for i in issues if i.action == action]
        if rows:
            print(f"{action} ({len(rows)}):")
            for issue in rows[:10]:
                print(f"  row {issue.row}: {issue.message}")
            if len(rows) > 10:
                print(f"  ... and {len(rows) - 10} more")
    return 0


def _wapi(args: argparse.Namespace) -> Wapi:
    username = args.username or os.environ.get(USER_ENV)
    if not username:
        raise ValueError(f"--wapi needs a user: --username or ${USER_ENV}")
    password = os.environ.get(PASSWORD_ENV)
    if password is None:
        if not sys.stdin.isatty():
            raise ValueError(f"set ${PASSWORD_ENV}; there is no terminal to ask for the password on")
        password = getpass.getpass(f"Infoblox password for {username}: ")
    if args.insecure:
        print("ipam-infoblox: warning: not verifying the Grid's certificate (--insecure)",
              file=sys.stderr)
    return Wapi(args.wapi, username, password, args.wapi_version, args.ca_bundle, args.insecure,
                args.allow_http, args.timeout)


def _plan(args: argparse.Namespace) -> int:
    check_template(args.network_view, "--network-view")
    check_template(args.comment, "--comment")
    table, records, issues = _load(args)
    options = Options(view=args.network_view, eas=list(args.ea), comment=args.comment)
    existing = None
    if args.wapi:
        wapi = _wapi(args)
        views = {view_for(options, r) for r in records} - {""}
        existing = load_existing(wapi, views)
    plan = build_plan(records, options, existing)
    written = write_import(plan, options, args.output, args.netmask_style, args.split)
    if args.report:
        write_private(args.report, render_report(plan, table, records, issues, options,
                                                 [p for p, _ in written]))
    c = counts(plan)
    print(f"ipam-infoblox: {c['containers']} container(s) and {c['networks']} network(s) to import"
          + (f", {c['exists']} already in Infoblox" if plan.checked else "")
          + (f", {c['conflicts']} conflict(s) left out" if c["conflicts"] else "")
          + f"; {len(rows_left_out(plan, issues))} row(s) left out",
          file=sys.stderr)
    for path, n in written:
        print(f"ipam-infoblox: wrote {path} ({n} object(s))", file=sys.stderr)
    for problem in plan.problems:
        print(f"ipam-infoblox: must fix: {problem}", file=sys.stderr)
    if c["conflicts"]:
        print("ipam-infoblox: conflicts are explained in " + (args.report or "the report "
              "(add --report FILE)"), file=sys.stderr)
    if not plan.checked:
        print("ipam-infoblox: not checked against Infoblox (add --wapi URL to find ranges "
              "already there)", file=sys.stderr)
    return 1 if plan.blocked else 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _inspect(args) if args.command == "inspect" else _plan(args)
    except BrokenPipeError:  # `inspect ... | head`: the reader left, nothing to report
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 1
    except (OSError, ValueError, RuntimeError) as e:
        print(f"ipam-infoblox: error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
