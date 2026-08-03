import json
import unittest

from app.dingtalk_messaging import DingTalkProactiveMessenger


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self) -> bytes:
        return self._body


class RecordingOpener:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        if request.full_url.endswith("/oauth2/accessToken"):
            return FakeResponse({"accessToken": "token-1", "expireIn": 7200})
        return FakeResponse({"processQueryKey": "query-1"})


class DingTalkProactiveMessengerTests(unittest.TestCase):
    def test_gets_one_token_and_sends_internal_robot_text(self) -> None:
        opener = RecordingOpener()
        messenger = DingTalkProactiveMessenger(
            app_key="app-key",
            app_secret="private-secret",
            robot_code="robot-code",
            opener=opener,
            clock=lambda: 1000.0,
        )

        messenger.send_text(("staff-1", "staff-1"), "第一条消息")
        messenger.send_text(("staff-2",), "第二条消息")

        self.assertEqual(len(opener.calls), 3)
        token_request = opener.calls[0][0]
        first_send = opener.calls[1][0]
        second_send = opener.calls[2][0]
        self.assertEqual(
            json.loads(token_request.data.decode("utf-8")),
            {"appKey": "app-key", "appSecret": "private-secret"},
        )
        first_payload = json.loads(first_send.data.decode("utf-8"))
        self.assertEqual(first_payload["robotCode"], "robot-code")
        self.assertEqual(first_payload["userIds"], ["staff-1"])
        self.assertEqual(first_payload["msgKey"], "sampleText")
        self.assertEqual(
            json.loads(first_payload["msgParam"]),
            {"content": "第一条消息"},
        )
        self.assertEqual(
            dict(first_send.header_items())["X-acs-dingtalk-access-token"],
            "token-1",
        )
        self.assertEqual(
            json.loads(second_send.data.decode("utf-8"))["userIds"],
            ["staff-2"],
        )

    def test_creates_delivers_and_updates_stream_group_card(self) -> None:
        opener = RecordingOpener()
        messenger = DingTalkProactiveMessenger(
            app_key="app-key",
            app_secret="private-secret",
            robot_code="robot-code",
            opener=opener,
            clock=lambda: 1000.0,
        )
        card_data = {"msgTitle": "协作处理", "staticMsgContent": "测试"}

        messenger.send_group_card(
            group_id="group-1",
            card_biz_id="card-1",
            card_data=card_data,
        )
        messenger.update_group_card(card_biz_id="card-1", card_data=card_data)

        self.assertEqual(len(opener.calls), 5)
        create_request = opener.calls[1][0]
        deliver_request = opener.calls[2][0]
        finish_request = opener.calls[3][0]
        update_request = opener.calls[4][0]
        create_payload = json.loads(create_request.data.decode("utf-8"))
        deliver_payload = json.loads(deliver_request.data.decode("utf-8"))
        finish_payload = json.loads(finish_request.data.decode("utf-8"))
        update_payload = json.loads(update_request.data.decode("utf-8"))
        self.assertTrue(create_request.full_url.endswith("/card/instances"))
        self.assertEqual(create_request.get_method(), "POST")
        self.assertEqual(create_payload["callbackType"], "STREAM")
        self.assertEqual(create_payload["outTrackId"], "card-1")
        self.assertEqual(
            create_payload["cardData"]["cardParamMap"],
            {"flowStatus": "1"},
        )
        self.assertTrue(deliver_request.full_url.endswith("/instances/deliver"))
        self.assertEqual(
            deliver_payload["openSpaceId"],
            "dtv1.card//IM_GROUP.group-1",
        )
        self.assertEqual(
            deliver_payload["imGroupOpenDeliverModel"]["robotCode"],
            "robot-code",
        )
        self.assertEqual(finish_request.get_method(), "PUT")
        self.assertEqual(
            finish_payload["cardData"]["cardParamMap"],
            {**card_data, "flowStatus": "3"},
        )
        self.assertTrue(update_request.full_url.endswith("/card/instances"))
        self.assertEqual(update_request.get_method(), "PUT")
        self.assertEqual(update_payload["outTrackId"], "card-1")
        self.assertEqual(
            update_payload["cardData"]["cardParamMap"],
            {**card_data, "flowStatus": "3"},
        )


if __name__ == "__main__":
    unittest.main()
