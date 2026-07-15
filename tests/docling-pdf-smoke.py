import argparse
import base64
import json
from pathlib import Path
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a real PDF smoke test against the Docling API.")
    parser.add_argument("--base-url", default="http://127.0.0.1:5001")
    parser.add_argument("--pdf", type=Path, default=Path(".runtime/w3c-dummy.pdf"))
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()

    pdf_path = args.pdf.resolve()
    if not pdf_path.is_file():
        raise SystemExit(f"PDF fixture does not exist: {pdf_path}")

    payload = {
        "file_sources": [
            {
                "base64_string": base64.b64encode(pdf_path.read_bytes()).decode("ascii"),
                "filename": pdf_path.name,
            }
        ],
        "options": {
            "from_formats": ["pdf"],
            "to_formats": ["md"],
            "do_ocr": False,
        },
    }
    request = Request(
        f"{args.base_url.rstrip('/')}/v1/convert/source",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    with urlopen(request, timeout=args.timeout) as response:
        result = json.loads(response.read().decode("utf-8"))

    if result.get("status") not in {"success", "partial_success"}:
        raise SystemExit(f"Unexpected conversion status: {result.get('status')!r}")

    markdown = result.get("document", {}).get("md_content", "")
    if "Dummy PDF file" not in markdown:
        raise SystemExit(f"Expected PDF text was not found. Markdown: {markdown!r}")

    print("Docling PDF smoke test passed.")
    print(f"Status: {result['status']}")
    print(f"Markdown: {markdown.strip()}")


if __name__ == "__main__":
    main()
