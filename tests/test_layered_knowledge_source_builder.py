import unittest
from datetime import date
from unittest.mock import patch

from app.knowledge_sources import build_layered_knowledge_packages
from scripts.build_layered_knowledge_sources import _load_document


class LayeredKnowledgeSourceBuilderTests(unittest.TestCase):
    def test_registry_section_rule_changes_generated_answer_layer(self) -> None:
        raw = {
            "source_id": "SH-SRC-001",
            "title": "拉伸大师员工合伙制3.0发布",
            "node_id": "fixture-node",
            "authority": "official_policy",
            "review_status": "approved",
            "lifecycle": "current",
            "sensitivity": "internal",
            "section_rules": [
                {
                    "heading_contains": "股东开分店标准",
                    "use": "reference_only",
                    "reason": "个人资格计算口径待确认",
                }
            ],
        }
        payload = {
            "success": True,
            "title": "拉伸大师员工合伙制3.0发布",
            "nodeId": "fixture-node",
            "docUrl": "https://alidocs.example/i/nodes/fixture-node",
            "logId": "fixture-log",
            "markdown": (
                "## 三、风险金制度\n\n风险金用于经营安全。\n\n"
                "## 五、股东开分店标准\n\n原门店分红已回本。\n"
            ),
        }

        with patch(
            "scripts.build_layered_knowledge_sources._read_dingtalk_document",
            return_value=payload,
        ):
            document = _load_document(raw)
        packages = build_layered_knowledge_packages(
            (document,),
            as_of=date(2026, 7, 28),
        )
        by_layer = {package.layer: package for package in packages}

        self.assertIn("风险金用于经营安全", by_layer["direct_answer"].markdown)
        self.assertNotIn("原门店分红已回本", by_layer["direct_answer"].markdown)
        self.assertIn("原门店分红已回本", by_layer["reference_only"].markdown)


if __name__ == "__main__":
    unittest.main()
