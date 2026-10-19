"""Read an AWS IPAM export (CSV or Excel) into rows keyed by column header.

Exports differ: the console, the CLI flattened to CSV, and hand-kept
spreadsheets all name their columns a little differently, and spreadsheets
often have a title or a blank line above the header. So the header row is
found rather than assumed (the first of the top rows with a CIDR column), and
each field is matched against a list of known names. `--column FIELD=HEADER`
settles anything the matching gets wrong; `inspect` shows what it chose.
"""

from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

HEADER_SEARCH_ROWS = 10

# Field -> the column names it is known by, compared after lower-casing and
# dropping everything but letters and digits ("Resource CIDR" -> "resourcecidr").
ALIASES: Dict[str, Tuple[str, ...]] = {
    "cidr": ("resourcecidr", "cidr", "cidrblock", "ipv4cidr", "ipv6cidr", "ipcidr", "prefix",
             "network", "subnetcidr", "vpccidr", "poolcidr", "allocationcidr"),
    "resource_type": ("resourcetype", "type", "objecttype"),
    "resource_id": ("resourceid", "id"),
    "name": ("name", "resourcename", "nametag", "resourcetagname", "tagname", "displayname"),
    "region": ("resourceregion", "region", "locale", "awsregion", "operatingregion"),
    "account": ("resourceownerid", "resourceowner", "ownerid", "owner", "accountid", "account",
                "awsaccount", "awsaccountid"),
    "vpc_id": ("vpcid", "vpc"),
    "pool_id": ("ipampoolid", "poolid", "pool", "sourcepoolid"),
    "description": ("description", "comment"),
    "management_state": ("managementstate",),
    "overlap_status": ("overlapstatus",),
    "compliance_status": ("compliancestatus",),
}
FIELDS = tuple(ALIASES)


def normalize_header(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


@dataclass
class Table:
    """Rows of one sheet, keyed by header, with where they came from."""
    path: str
    sheet: str
    header_row: int                       # 1-based row number of the header
    headers: List[str]
    rows: List[Tuple[int, Dict[str, str]]] = field(default_factory=list)   # (row number, values)
    columns: Dict[str, str] = field(default_factory=dict)                   # field -> header
    encoding: str = ""                                                      # CSV only


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)       # Excel stores account IDs as numbers
    return str(value).strip()


def _is_header(cells: Sequence[str], cidr_names: Iterable[str] = ()) -> bool:
    names = {normalize_header(c) for c in cells}
    return bool(names & (set(ALIASES["cidr"]) | {normalize_header(n) for n in cidr_names}))


def _from_grid(path: str, sheet: str, grid: Iterable[Sequence[object]],
               cidr_names: Iterable[str] = ()) -> Optional[Table]:
    """The table in a sheet's cells, or None if no header row is found."""
    lines = [[_cell(c) for c in row] for row in grid]
    cidr_names = list(cidr_names)
    for index, line in enumerate(lines[:HEADER_SEARCH_ROWS]):
        if _is_header(line, cidr_names):
            break
    else:
        return None
    headers = _unique_headers(line)
    table = Table(path, sheet, index + 1, headers)
    for number, cells in enumerate(lines[index + 1:], start=index + 2):
        if not any(cells):
            continue
        cells = list(cells) + [""] * (len(headers) - len(cells))
        table.rows.append((number, dict(zip(headers, cells))))
    return table


def _unique_headers(cells: Sequence[str]) -> List[str]:
    """Headers as written, with blanks named by position and repeats
    numbered, so no column is lost to a dict key clash."""
    out: List[str] = []
    for i, cell in enumerate(cells):
        name = cell or f"column {i + 1}"
        base, n = name, 2
        while name in out:
            name, n = f"{base} ({n})", n + 1
        out.append(name)
    return out


def read_table(path: str, sheet: Optional[str] = None, cidr_column: Optional[str] = None) -> Table:
    """The IPAM rows in `path` (.csv, .tsv, .txt, .xlsx or .xlsm).
    `cidr_column` (from --column cidr=HEADER) also marks the header row."""
    names = [cidr_column] if cidr_column else []
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{path}: no such file")
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        return _read_excel(path, sheet, names)
    if ext == ".xls":
        raise ValueError(f"{path}: old .xls files aren't supported; save it as .xlsx or CSV")
    if sheet:
        raise ValueError("--sheet only applies to Excel files")
    with open(path, "rb") as handle:
        data = handle.read()
    try:
        text, encoding = data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:  # Excel on Windows saves "CSV" in the ANSI code page
        text, encoding = data.decode("cp1252", errors="replace"), "windows-1252"
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    table = _from_grid(path, "", csv.reader(io.StringIO(text, newline=""), dialect), names)
    if table is None:
        raise ValueError(f"{path}: no header row with a CIDR column in the first "
                         f"{HEADER_SEARCH_ROWS} rows (expected a column such as 'Resource CIDR'; "
                         "name it with --column cidr=HEADER)")
    table.encoding = encoding
    return table


def _read_excel(path: str, sheet: Optional[str], names: Sequence[str] = ()) -> Table:
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("reading Excel files needs openpyxl: pip install 'ipam-infoblox[xlsx]' "
                           "(or save the sheet as CSV)") from None
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        names = book.sheetnames
        if sheet is not None:
            if sheet not in names:
                raise ValueError(f"{path}: no sheet named {sheet!r} (sheets: {', '.join(names)})")
            names = [sheet]
        for name in names:
            table = _from_grid(path, name, book[name].iter_rows(values_only=True), names)
            if table is not None:
                return table
    finally:
        book.close()
    raise ValueError(f"{path}: no sheet has a header row with a CIDR column (sheets: "
                     f"{', '.join(book.sheetnames)}); pick one with --sheet and name the column "
                     "with --column cidr=HEADER")


def map_columns(table: Table, overrides: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """field -> header. Overrides name a header exactly (or by its
    normalized form); otherwise the first header matching a known name wins,
    in the order of ALIASES, so "Resource CIDR" beats a bare "CIDR"."""
    overrides = dict(overrides or {})
    by_norm = {normalize_header(h): h for h in reversed(table.headers)}
    columns: Dict[str, str] = {}
    used = set()
    for name, header in overrides.items():
        if name not in ALIASES:
            raise ValueError(f"--column: unknown field {name!r} (fields: {', '.join(FIELDS)})")
        found = header if header in table.headers else by_norm.get(normalize_header(header))
        if found is None:
            raise ValueError(f"--column {name}={header}: no such column (columns: "
                             f"{', '.join(table.headers)})")
        columns[name] = found
        used.add(found)
    for name, aliases in ALIASES.items():
        if name in columns:
            continue
        for alias in aliases:
            header = by_norm.get(alias)
            if header is not None and header not in used:
                columns[name] = header
                used.add(header)
                break
    if "cidr" not in columns:
        raise ValueError("no CIDR column found; name it with --column cidr=HEADER")
    table.columns = columns
    return columns
