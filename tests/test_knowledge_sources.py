import unittest
from datetime import date
import tempfile
from pathlib import Path

from app.knowledge_sources import (
    KnowledgeSource,
    KnowledgeSourceDocument,
    KnowledgeSourcePolicy,
    KnowledgeSectionRule,
    build_direct_answer_candidate,
    build_layered_knowledge_packages,
    validate_direct_answer_packages,
    validate_faq_case_coverage,
    write_layered_knowledge_packages,
)


class KnowledgeSourcePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = KnowledgeSourcePolicy()
        self.as_of = date(2026, 7, 28)

    def test_approved_current_official_source_can_answer_directly(self) -> None:
        decision = self.policy.evaluate(
            self._source(authority="official_policy"),
            as_of=self.as_of,
        )

        self.assertEqual(decision.use, "direct_answer")
        self.assertTrue(decision.include_content)

    def test_pending_or_draft_source_is_reference_only(self) -> None:
        pending = self.policy.evaluate(
            self._source(review_status="pending_review"),
            as_of=self.as_of,
        )
        draft = self.policy.evaluate(
            self._source(authority="draft"),
            as_of=self.as_of,
        )

        self.assertEqual(pending.use, "reference_only")
        self.assertEqual(draft.use, "reference_only")

    def test_stopped_or_superseded_source_is_historical_only(self) -> None:
        stopped = self.policy.evaluate(
            self._source(lifecycle="stopped"),
            as_of=self.as_of,
        )
        superseded = self.policy.evaluate(
            self._source(lifecycle="superseded"),
            as_of=self.as_of,
        )

        self.assertEqual(stopped.use, "historical_only")
        self.assertEqual(superseded.use, "historical_only")

    def test_restricted_personal_source_exposes_descriptor_not_raw_content(self) -> None:
        decision = self.policy.evaluate(
            self._source(sensitivity="restricted_personal"),
            as_of=self.as_of,
        )

        self.assertEqual(decision.use, "restricted_lookup")
        self.assertFalse(decision.include_content)

    def test_conflicting_source_is_blocked_even_if_official_and_approved(self) -> None:
        decision = self.policy.evaluate(
            self._source(conflicts_with=("SRC-002",)),
            as_of=self.as_of,
        )

        self.assertEqual(decision.use, "blocked")
        self.assertFalse(decision.include_content)

    def test_source_outside_effective_window_is_historical_only(self) -> None:
        decision = self.policy.evaluate(
            self._source(effective_until=date(2026, 6, 30)),
            as_of=self.as_of,
        )

        self.assertEqual(decision.use, "historical_only")

    @staticmethod
    def _source(**overrides: object) -> KnowledgeSource:
        values: dict[str, object] = {
            "source_id": "SH-SRC-001",
            "title": "拉伸大师员工合伙制3.0发布",
            "domain": "shareholder",
            "authority": "official_policy",
            "review_status": "approved",
            "lifecycle": "current",
            "sensitivity": "internal",
        }
        values.update(overrides)
        return KnowledgeSource(**values)  # type: ignore[arg-type]


class LayeredKnowledgePackageTests(unittest.TestCase):
    def test_faq_coverage_maps_natural_language_case_to_canonical_section(self) -> None:
        candidate = (
            "### SH-Q009：什么时候开始计算分红？\n\n"
            "财务确认收款后，以OA中的起始分红月份为准。\n"
        )
        cases = (
            {
                "id": "SH-Q009",
                "expected_keywords": ["财务确认收款", "起始分红月份", "OA"],
            },
            {
                "id": "SH-Q009-NL1",
                "expected_keywords": ["财务确认收款", "起始分红月份", "OA"],
            },
        )

        validate_faq_case_coverage(candidate, cases)

        with self.assertRaisesRegex(ValueError, "SH-Q009-NL1"):
            validate_faq_case_coverage(
                candidate,
                (
                    {
                        "id": "SH-Q009-NL1",
                        "expected_keywords": ["不存在的关键词"],
                    },
                ),
            )

    def test_combines_approved_faq_with_direct_source_sections(self) -> None:
        source_packages = (
            KnowledgeSourceDocument(
                source=KnowledgeSource(
                    source_id="SH-SRC-001",
                    title="正式制度",
                    domain="shareholder",
                    authority="official_policy",
                    review_status="approved",
                ),
                markdown=(
                    "## 三、风险金制度\n\n风险金用于经营安全。\n\n"
                    "### 3.1 为什么需要风险金\n\n用于突发情况。"
                ),
            ),
        )
        layered = build_layered_knowledge_packages(
            source_packages,
            as_of=date(2026, 7, 28),
        )

        candidate = build_direct_answer_candidate(
            domain="shareholder",
            approved_faq_markdown="### SH-Q001：股东类型\n\n包含管理股东。",
            layered_packages=layered,
            blocked_markers={"SH-Q016": ("前两个月平均业绩达到70%",)},
        )

        self.assertIn("SH-Q001", candidate.markdown)
        self.assertIn("风险金用于经营安全", candidate.markdown)
        self.assertIn("### 正式制度 / 三、风险金制度", candidate.markdown)
        self.assertNotIn("### 三、风险金制度", candidate.markdown)
        self.assertIn("**3.1 为什么需要风险金**", candidate.markdown)
        self.assertEqual(candidate.source_ids, ("SH-SRC-001",))

    def test_combined_candidate_rejects_frozen_fact_from_either_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "SH-Q016"):
            build_direct_answer_candidate(
                domain="shareholder",
                approved_faq_markdown=(
                    "### SH-Q016：开分店\n\n前两个月平均业绩达到70%。"
                ),
                layered_packages=(),
                blocked_markers={"SH-Q016": ("前两个月平均业绩达到70%",)},
            )

    def test_release_validation_rejects_frozen_fact_in_direct_layer_only(self) -> None:
        documents = (
            KnowledgeSourceDocument(
                source=KnowledgeSource(
                    source_id="SH-SRC-001",
                    title="正式制度",
                    domain="shareholder",
                    authority="official_policy",
                    review_status="approved",
                ),
                markdown="## 五、开店\n\n前两个月平均业绩达到70%。",
            ),
            KnowledgeSourceDocument(
                source=KnowledgeSource(
                    source_id="SH-SRC-002",
                    title="待确认手册",
                    domain="shareholder",
                    authority="operational",
                    review_status="pending_review",
                ),
                markdown="## 五、开店\n\n前两个月平均业绩达到70%。",
            ),
        )
        packages = build_layered_knowledge_packages(
            documents,
            as_of=date(2026, 7, 28),
        )

        with self.assertRaisesRegex(ValueError, "SH-Q016"):
            validate_direct_answer_packages(
                packages,
                {"SH-Q016": ("前两个月平均业绩达到70%",)},
            )

        validate_direct_answer_packages(
            tuple(
                package
                for package in packages
                if package.layer != "direct_answer"
            ),
            {"SH-Q016": ("前两个月平均业绩达到70%",)},
        )

    def test_demotes_matching_section_without_demoting_other_sections(self) -> None:
        documents = (
            KnowledgeSourceDocument(
                source=KnowledgeSource(
                    source_id="SH-SRC-001",
                    title="正式制度",
                    domain="shareholder",
                    authority="official_policy",
                    review_status="approved",
                ),
                markdown=(
                    "# 股东制3.0\n\n"
                    "## 三、风险金制度\n\n风险金用于经营安全。\n\n"
                    "## 五、股东开分店标准\n\n原门店分红已回本。\n"
                ),
                section_rules=(
                    KnowledgeSectionRule(
                        heading_contains="股东开分店标准",
                        use="reference_only",
                        reason="个人资格计算口径待确认",
                    ),
                ),
            ),
        )

        packages = build_layered_knowledge_packages(
            documents,
            as_of=date(2026, 7, 28),
        )
        by_layer = {package.layer: package for package in packages}

        self.assertIn("风险金用于经营安全", by_layer["direct_answer"].markdown)
        self.assertNotIn("原门店分红已回本", by_layer["direct_answer"].markdown)
        self.assertIn("原门店分红已回本", by_layer["reference_only"].markdown)
        self.assertIn("个人资格计算口径待确认", by_layer["reference_only"].markdown)

    def test_section_rule_cannot_promote_pending_source_to_direct_answer(self) -> None:
        documents = (
            KnowledgeSourceDocument(
                source=KnowledgeSource(
                    source_id="SH-SRC-002",
                    title="待确认手册",
                    domain="shareholder",
                    authority="operational",
                    review_status="pending_review",
                ),
                markdown="## 常见问题\n\n待确认答案。\n",
                section_rules=(
                    KnowledgeSectionRule(
                        heading_contains="常见问题",
                        use="direct_answer",
                        reason="错误配置",
                    ),
                ),
            ),
        )

        packages = build_layered_knowledge_packages(
            documents,
            as_of=date(2026, 7, 28),
        )
        by_layer = {package.layer: package for package in packages}

        self.assertNotIn("direct_answer", by_layer)
        self.assertIn("待确认答案", by_layer["reference_only"].markdown)

    def test_builds_separate_packages_and_omits_restricted_raw_content(self) -> None:
        documents = (
            KnowledgeSourceDocument(
                source=KnowledgeSource(
                    source_id="SH-SRC-001",
                    title="正式制度",
                    domain="shareholder",
                    authority="official_policy",
                    review_status="approved",
                ),
                markdown="正式规则正文",
            ),
            KnowledgeSourceDocument(
                source=KnowledgeSource(
                    source_id="SH-SRC-002",
                    title="待确认会议纪要",
                    domain="shareholder",
                    authority="draft",
                    review_status="pending_review",
                ),
                markdown="尚未确认的新口径",
            ),
            KnowledgeSourceDocument(
                source=KnowledgeSource(
                    source_id="SH-DATA-001",
                    title="股东个人信息表",
                    domain="shareholder",
                    authority="operational",
                    review_status="approved",
                    sensitivity="restricted_personal",
                ),
                markdown="张三的个人分红和手机号",
                lookup_hint="经员工身份校验后按本人查询",
            ),
        )

        packages = build_layered_knowledge_packages(
            documents,
            as_of=date(2026, 7, 28),
        )

        by_layer = {package.layer: package for package in packages}
        self.assertIn("正式规则正文", by_layer["direct_answer"].markdown)
        self.assertNotIn("尚未确认的新口径", by_layer["direct_answer"].markdown)
        self.assertIn("尚未确认的新口径", by_layer["reference_only"].markdown)
        self.assertIn("经员工身份校验后按本人查询", by_layer["restricted_lookup"].markdown)
        self.assertNotIn("张三的个人分红和手机号", by_layer["restricted_lookup"].markdown)
        self.assertEqual(by_layer["direct_answer"].source_ids, ("SH-SRC-001",))

    def test_writes_predictable_layered_package_names(self) -> None:
        documents = (
            KnowledgeSourceDocument(
                source=KnowledgeSource(
                    source_id="SH-SRC-001",
                    title="正式制度",
                    domain="shareholder",
                    authority="official_policy",
                    review_status="approved",
                ),
                markdown="正式规则正文",
            ),
        )
        packages = build_layered_knowledge_packages(
            documents,
            as_of=date(2026, 7, 28),
        )

        with tempfile.TemporaryDirectory() as directory:
            written = write_layered_knowledge_packages(packages, directory)

            self.assertEqual(
                written,
                (Path(directory) / "shareholder-direct_answer.md",),
            )
            self.assertEqual(
                written[0].read_text(encoding="utf-8"),
                packages[0].markdown,
            )


if __name__ == "__main__":
    unittest.main()
