import unittest

from app.operations_privacy import (
    normalize_question,
    question_fingerprint,
    redact_question,
)


class OperationsPrivacyTests(unittest.TestCase):
    def test_redacts_supported_personal_identifiers_and_truncates(self) -> None:
        text = (
            "联系 13812345678、buyer@example.com，"
            "身份证11010519491231002X，卡号 6222021234567890123。"
            + "问" * 600
        )

        redacted = redact_question(text)

        self.assertNotIn("13812345678", redacted)
        self.assertNotIn("buyer@example.com", redacted)
        self.assertNotIn("11010519491231002X", redacted)
        self.assertNotIn("6222021234567890123", redacted)
        self.assertIn("[手机号]", redacted)
        self.assertIn("[邮箱]", redacted)
        self.assertIn("[身份证]", redacted)
        self.assertIn("[卡号]", redacted)
        self.assertLessEqual(len(redacted), 500)

    def test_normalization_merges_nfkc_case_whitespace_and_edge_punctuation(self) -> None:
        variants = ["  ＡＢＣ  保修？ ", "abc 保修", "…ABC\t保修!!!"]

        normalized = {normalize_question(item) for item in variants}

        self.assertEqual(normalized, {"abc 保修"})

    def test_fingerprint_is_stable_for_normalized_variants(self) -> None:
        variants = ["  ＡＢＣ  保修？ ", "abc 保修", "…ABC\t保修!!!"]

        fingerprints = {question_fingerprint(item) for item in variants}

        self.assertEqual(len(fingerprints), 1)
        self.assertEqual(len(fingerprints.pop()), 64)

    def test_fingerprint_is_based_on_redacted_text(self) -> None:
        self.assertEqual(
            question_fingerprint("手机号 13812345678 怎么改？"),
            question_fingerprint("手机号 13900001111 怎么改？"),
        )


if __name__ == "__main__":
    unittest.main()
