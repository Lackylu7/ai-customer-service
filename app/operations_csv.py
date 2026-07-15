import codecs
import csv
import io
from collections.abc import Iterable, Mapping


_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def encode_csv(
    headers: tuple[str, ...],
    rows: Iterable[Mapping[str, object]],
) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=headers,
        extrasaction="ignore",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {header: _safe_cell(row.get(header)) for header in headers}
        )
    return codecs.BOM_UTF8 + output.getvalue().encode("utf-8")


def _safe_cell(value: object) -> str:
    rendered = "" if value is None else str(value)
    if rendered.startswith(_DANGEROUS_PREFIXES):
        return "'" + rendered
    return rendered
