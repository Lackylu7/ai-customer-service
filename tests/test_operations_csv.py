import codecs
import unittest

from app.operations_csv import encode_csv


class OperationsCsvTests(unittest.TestCase):
    def test_encodes_chinese_with_utf8_bom(self) -> None:
        payload = encode_csv(
            ("问题", "备注"),
            ({"问题": "退款多久", "备注": "已补充"},),
        )

        self.assertTrue(payload.startswith(codecs.BOM_UTF8))
        self.assertIn("退款多久", payload.decode("utf-8-sig"))

    def test_prefixes_formula_and_control_character_cells(self) -> None:
        payload = encode_csv(
            ("问题", "备注"),
            (
                {"问题": "退款", "备注": '=HYPERLINK("bad")'},
                {"问题": "保修", "备注": "+SUM(1,1)"},
                {"问题": "运费", "备注": "-1"},
                {"问题": "账户", "备注": "@cmd"},
                {"问题": "发票", "备注": "\tcmd"},
                {"问题": "物流", "备注": "\rcmd"},
            ),
        ).decode("utf-8-sig")

        for dangerous in (
            "'=HYPERLINK",
            "'+SUM",
            "'-1",
            "'@cmd",
            "'\tcmd",
            "'\rcmd",
        ):
            self.assertIn(dangerous, payload)


if __name__ == "__main__":
    unittest.main()
