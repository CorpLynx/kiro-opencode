"""A Markdown report of the plan: what to fix, what goes in, what was changed.

It is meant for the change review that precedes an import, so blocking
problems come first, and every range and every cleaned or dropped row says
which row of the export it came from.
"""

from __future__ import annotations

from typing import List, Sequence

from .infoblox_csv import counts
from .plan import Entry, Options, Plan, comment_for
from .reader import Table
from .records import Issue, Record, counts_by_type


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _what(entry: Entry) -> str:
    record: Record = entry.primary
    parts = [record.resource_type or "range", record.resource_id or record.name]
    if record.account:
        parts.append(f"account {record.account}")
    return " ".join(p for p in parts if p)


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> List[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + " --- |" * len(headers)]
    out += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return out


def render_report(plan: Plan, table: Table, records: Sequence[Record], issues: Sequence[Issue],
                  options: Options, written: Sequence[str] = ()) -> str:
    c = counts(plan)
    source = table.path + (f", sheet {table.sheet!r}" if table.sheet else "")
    lines = ["# AWS IPAM to Infoblox import plan", "",
             f"- **Export:** {source}, header on row {table.header_row}, {len(table.rows)} data rows",
             "- **Columns used:** " + ", ".join(f"{k} = {v!r}" for k, v in table.columns.items()),
             "- **Ranges by type:** " + (", ".join(f"{k} {v}" for k, v in counts_by_type(records).items())
                                          or "none"),
             f"- **Network view:** `{options.view}`",
             "- **Checked against Infoblox:** " + ("yes" if plan.checked else
                                                  "no (pass --wapi to find ranges already there "
                                                  "and conflicts with them)")]
    if written:
        lines.append("- **Import file(s):** " + ", ".join(written))
    lines += ["", "| To import | Containers | Networks | Already in Infoblox | Conflicts | "
              "Rows changed | Rows left out |", "| --- | --- | --- | --- | --- | --- | --- |",
              f"| {c['containers'] + c['networks']} | {c['containers']} | {c['networks']} | "
              f"{c['exists']} | {c['conflicts']} | {len({i.row for i in issues if i.action == 'changed'})} | "
              f"{len({i.row for i in issues if i.action == 'dropped'})} |", ""]

    conflicts = plan.by_status("conflict")
    if plan.problems or conflicts:
        lines += ["## Fix before importing", ""]
        lines += [f"- {p}" for p in plan.problems]
        if plan.problems:
            lines.append("")
        if conflicts:
            lines += ["These ranges are left out of the import file:", ""]
            lines += _table(["CIDR", "View", "Rows", "From", "Why"],
                            [(e.cidr, e.view, e.rows, _what(e), " ".join(e.reasons))
                             for e in conflicts])
            lines.append("")

    new = plan.by_status("new")
    lines += ["## To import", ""]
    if new:
        lines += _table(["CIDR", "As", "View", "Goes inside", "Comment", "Rows", "Notes"],
                        [(e.cidr, e.kind, e.view, e.parent or "(top level)",
                          comment_for(e, options), e.rows, "; ".join(e.notes)) for e in new])
    else:
        lines.append("Nothing new to import.")
    lines.append("")

    exists = plan.by_status("exists")
    if exists:
        lines += ["## Already in Infoblox (left out)", ""]
        lines += _table(["CIDR", "View", "Rows", "From", "Notes"],
                        [(e.cidr, e.view, e.rows, _what(e), "; ".join(e.notes)) for e in exists])
        lines.append("")

    changed = [i for i in issues if i.action == "changed"]
    if changed:
        lines += ["## Changed from the export", ""]
        lines += _table(["Row", "Change"], [(i.row, i.message) for i in changed])
        lines.append("")
    dropped = [i for i in list(issues) + list(plan.issues) if i.action == "dropped"]
    if dropped:
        lines += ["## Rows left out", ""]
        lines += _table(["Row", "Why"], [(i.row, i.message) for i in sorted(dropped, key=lambda i: i.row)])
        lines.append("")
    lines += ["## Importing", "",
              "Use Grid Manager's CSV Import with the **Add** action, and run its test first: "
              "it validates every row without changing anything. Containers come first in the "
              "file, so the networks inside them land in place. Import a copy with a handful of "
              "rows first if this is the first import on your NIOS version.", ""]
    return "\n".join(lines)
