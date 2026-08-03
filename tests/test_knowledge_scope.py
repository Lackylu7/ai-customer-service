import unittest
from datetime import date

from app.knowledge_scope import KnowledgeContext, KnowledgeItem, KnowledgeScope


class KnowledgeScopeTests(unittest.TestCase):
    def test_activity_is_available_only_inside_its_active_window(self) -> None:
        item = KnowledgeItem(
            knowledge_id="ACT-2026-068",
            domain="activity",
            status="active",
            active_from=date(2026, 6, 13),
            active_until=date(2026, 8, 21),
        )
        scope = KnowledgeScope()

        current = scope.evaluate(item, KnowledgeContext(as_of=date(2026, 7, 22)))
        expired = scope.evaluate(item, KnowledgeContext(as_of=date(2026, 8, 22)))

        self.assertTrue(current.eligible)
        self.assertEqual(current.reason, "eligible")
        self.assertFalse(expired.eligible)
        self.assertEqual(expired.reason, "expired")

    def test_internal_knowledge_is_hidden_from_customer_context(self) -> None:
        item = KnowledgeItem(
            knowledge_id="FIN-INTERNAL-001",
            domain="finance",
            status="active",
            visible_to=frozenset({"employee", "headquarters"}),
        )
        scope = KnowledgeScope()

        employee = scope.evaluate(
            item,
            KnowledgeContext(as_of=date(2026, 7, 22), audience="employee"),
        )
        customer = scope.evaluate(
            item,
            KnowledgeContext(as_of=date(2026, 7, 22), audience="customer"),
        )

        self.assertTrue(employee.eligible)
        self.assertFalse(customer.eligible)
        self.assertEqual(customer.reason, "restricted_audience")

    def test_location_scoped_knowledge_requires_matching_context(self) -> None:
        item = KnowledgeItem(
            knowledge_id="ACT-SZ-001",
            domain="activity",
            status="active",
            applicable_cities=frozenset({"深圳"}),
            applicable_stores=frozenset({"体育中心店"}),
        )
        scope = KnowledgeScope()

        matching = scope.evaluate(
            item,
            KnowledgeContext(
                as_of=date(2026, 7, 22),
                city="深圳",
                store="体育中心店",
            ),
        )
        wrong_city = scope.evaluate(
            item,
            KnowledgeContext(
                as_of=date(2026, 7, 22),
                city="上海",
                store="体育中心店",
            ),
        )
        missing_store = scope.evaluate(
            item,
            KnowledgeContext(as_of=date(2026, 7, 22), city="深圳"),
        )

        self.assertTrue(matching.eligible)
        self.assertEqual(wrong_city.reason, "out_of_scope")
        self.assertEqual(missing_store.reason, "missing_context")


if __name__ == "__main__":
    unittest.main()
