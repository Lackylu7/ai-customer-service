$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$dslPath = Join-Path $projectRoot "infra\dify\customer-service-template.yml"
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $dslPath)) {
    throw "Dify Chatflow DSL file does not exist."
}

$validator = @'
import sys
from pathlib import Path

import yaml

data = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
text = Path(sys.argv[1]).read_text(encoding="utf-8")
assert data["version"] == "0.3.1"
assert data["kind"] == "app"
assert data["app"]["mode"] == "advanced-chat"

nodes = data["workflow"]["graph"]["nodes"]
by_type = {}
for node in nodes:
    by_type.setdefault(node["data"]["type"], []).append(node)
assert len(by_type.get("start", [])) == 1
assert len(by_type.get("knowledge-retrieval", [])) == 1
assert len(by_type.get("llm", [])) == 2
assert len(by_type.get("answer", [])) == 1

knowledge = by_type["knowledge-retrieval"][0]
llms = {node["id"]: node for node in by_type["llm"]}
understanding = llms["query-understanding"]
llm = llms["llm"]
answer = by_type["answer"][0]
assert knowledge["data"]["dataset_ids"] == ["__DIFY_DATASET_ID__"]
assert knowledge["data"]["query_variable_selector"] == ["query-understanding", "text"]
assert understanding["data"]["model"]["name"] == "deepseek-chat"
assert understanding["data"]["memory"]["window"]["enabled"] is True
assert understanding["data"]["memory"]["window"]["size"] == 10
understanding_prompt = "\n".join(
    item["text"] for item in understanding["data"]["prompt_template"]
)
assert understanding_prompt == "__QUERY_UNDERSTANDING_PROMPT__"
assert llm["data"]["model"]["name"] == "deepseek-chat"
assert "deepseek" in llm["data"]["model"]["provider"].lower()
assert llm["data"]["context"]["variable_selector"] == ["knowledge", "result"]
prompt = "\n".join(item["text"] for item in llm["data"]["prompt_template"])
assert prompt == "__SYSTEM_PROMPT__"
assert data["app"]["name"] == "__DIFY_APP_NAME__"
assert data["app"]["description"] == "__APP_DESCRIPTION__"
assert data["workflow"]["features"]["opening_statement"] == "__OPENING_STATEMENT__"
assert knowledge["data"]["desc"] == "__KNOWLEDGE_DESCRIPTION__"
assert answer["data"]["answer"] == "{{#llm.text#}}"

expected = {
    "__APP_DESCRIPTION__",
    "__DIFY_APP_NAME__",
    "__OPENING_STATEMENT__",
    "__DIFY_DATASET_ID__",
    "__KNOWLEDGE_DESCRIPTION__",
    "__SYSTEM_PROMPT__",
    "__QUERY_UNDERSTANDING_PROMPT__",
}
for placeholder in expected:
    assert placeholder in text, placeholder
assert "\u661f\u6cb3\u4f18\u9009" not in text

edges = data["workflow"]["graph"]["edges"]
pairs = {(edge["source"], edge["target"]) for edge in edges}
assert pairs == {
    ("start", "query-understanding"),
    ("query-understanding", "knowledge"),
    ("knowledge", "llm"),
    ("llm", "answer"),
}
'@

$validator | & $pythonPath - $dslPath
if ($LASTEXITCODE -ne 0) {
    throw "Dify Chatflow DSL validation failed."
}

Write-Output "PASS: Dify Chatflow DSL structure is valid."
