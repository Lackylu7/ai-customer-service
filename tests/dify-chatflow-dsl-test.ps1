$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$dslPath = Join-Path $projectRoot "infra\dify\star-river-customer-service.yml"
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $dslPath)) {
    throw "Dify Chatflow DSL file does not exist."
}

$validator = @'
import sys
from pathlib import Path

import yaml

data = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert data["version"] == "0.3.1"
assert data["kind"] == "app"
assert data["app"]["mode"] == "advanced-chat"

nodes = data["workflow"]["graph"]["nodes"]
by_type = {}
for node in nodes:
    by_type.setdefault(node["data"]["type"], []).append(node)
for node_type in ("start", "knowledge-retrieval", "llm", "answer"):
    assert len(by_type.get(node_type, [])) == 1, node_type

knowledge = by_type["knowledge-retrieval"][0]
llm = by_type["llm"][0]
answer = by_type["answer"][0]
assert knowledge["data"]["dataset_ids"] == ["__DIFY_DATASET_ID__"]
assert knowledge["data"]["query_variable_selector"] == ["start", "sys.query"]
assert llm["data"]["model"]["name"] == "deepseek-chat"
assert "deepseek" in llm["data"]["model"]["provider"].lower()
assert llm["data"]["context"]["variable_selector"] == ["knowledge", "result"]
prompt = "\n".join(item["text"] for item in llm["data"]["prompt_template"])
assert "[HANDOFF]" in prompt
assert "\u4e0d\u5f97\u7f16\u9020" in prompt
assert "{{#context#}}" in prompt
assert answer["data"]["answer"] == "{{#llm.text#}}"

edges = data["workflow"]["graph"]["edges"]
pairs = {(edge["source"], edge["target"]) for edge in edges}
assert pairs == {("start", "knowledge"), ("knowledge", "llm"), ("llm", "answer")}
'@

$validator | & $pythonPath - $dslPath
if ($LASTEXITCODE -ne 0) {
    throw "Dify Chatflow DSL validation failed."
}

Write-Output "PASS: Dify Chatflow DSL structure is valid."
