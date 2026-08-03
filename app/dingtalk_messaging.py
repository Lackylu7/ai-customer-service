from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class Response(Protocol):
    def __enter__(self) -> "Response": ...

    def __exit__(self, *args: object) -> None: ...

    def read(self) -> bytes: ...


OpenUrl = Callable[..., Response]


class DingTalkMessagingError(RuntimeError):
    def __init__(self, status_code: int | None, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class _AccessToken:
    value: str
    expires_at: float


class DingTalkProactiveMessenger:
    """Send internal robot messages without relying on a temporary session webhook."""

    _COLLABORATION_CARD_TEMPLATE_ID = (
        "382e4302-551d-4880-bf29-a30acfab2e71.schema"
    )

    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        robot_code: str,
        timeout: float = 15,
        opener: OpenUrl = urlopen,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._app_key = _require_text(app_key, "app_key")
        self._app_secret = _require_text(app_secret, "app_secret")
        self._robot_code = _require_text(robot_code, "robot_code")
        if timeout <= 0:
            raise ValueError("timeout must be positive.")
        self._timeout = timeout
        self._opener = opener
        self._clock = clock
        self._token: _AccessToken | None = None
        self._token_lock = threading.Lock()

    def send_text(self, user_ids: Sequence[str], text: str) -> None:
        recipients = tuple(
            dict.fromkeys(_require_text(user_id, "user_id") for user_id in user_ids)
        )
        content = _require_text(text, "text")
        if not recipients:
            raise ValueError("At least one DingTalk recipient is required.")
        self._send_with_token(recipients, content, retry_auth=True)

    def send_group_card(
        self,
        *,
        group_id: str,
        card_biz_id: str,
        card_data: dict[str, object],
    ) -> None:
        self._send_group_card_with_token(
            group_id=_require_text(group_id, "group_id"),
            card_biz_id=_require_text(card_biz_id, "card_biz_id"),
            card_data=_require_card_data(card_data),
            retry_auth=True,
        )

    def update_group_card(
        self,
        *,
        card_biz_id: str,
        card_data: dict[str, object],
    ) -> None:
        self._update_group_card_with_token(
            card_biz_id=_require_text(card_biz_id, "card_biz_id"),
            card_data=_require_card_data(card_data),
            retry_auth=True,
        )

    def _send_with_token(
        self,
        recipients: tuple[str, ...],
        content: str,
        *,
        retry_auth: bool,
    ) -> None:
        token = self._get_access_token()
        try:
            self._request_json(
                "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
                {
                    "robotCode": self._robot_code,
                    "userIds": list(recipients),
                    "msgKey": "sampleText",
                    "msgParam": json.dumps(
                        {"content": content},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
                headers={"x-acs-dingtalk-access-token": token},
            )
        except DingTalkMessagingError as exc:
            if retry_auth and exc.status_code in {401, 403}:
                with self._token_lock:
                    self._token = None
                self._send_with_token(recipients, content, retry_auth=False)
                return
            raise

    def _send_group_card_with_token(
        self,
        *,
        group_id: str,
        card_biz_id: str,
        card_data: dict[str, object],
        retry_auth: bool,
    ) -> None:
        token = self._get_access_token()
        try:
            self._request_json(
                "https://api.dingtalk.com/v1.0/card/instances",
                {
                    "cardTemplateId": self._COLLABORATION_CARD_TEMPLATE_ID,
                    "outTrackId": card_biz_id,
                    "cardData": {"cardParamMap": {"flowStatus": "1"}},
                    "callbackType": "STREAM",
                    "imGroupOpenSpaceModel": {"supportForward": False},
                },
                headers={"x-acs-dingtalk-access-token": token},
            )
            self._request_json(
                "https://api.dingtalk.com/v1.0/card/instances/deliver",
                {
                    "outTrackId": card_biz_id,
                    "userIdType": 1,
                    "openSpaceId": f"dtv1.card//IM_GROUP.{group_id}",
                    "imGroupOpenDeliverModel": {
                        "robotCode": self._robot_code,
                    },
                },
                headers={"x-acs-dingtalk-access-token": token},
            )
            self._request_json(
                "https://api.dingtalk.com/v1.0/card/instances",
                {
                    "outTrackId": card_biz_id,
                    "cardData": {
                        "cardParamMap": {**card_data, "flowStatus": "3"}
                    },
                },
                headers={"x-acs-dingtalk-access-token": token},
                method="PUT",
            )
        except DingTalkMessagingError as exc:
            if retry_auth and exc.status_code in {401, 403}:
                self._clear_token()
                self._send_group_card_with_token(
                    group_id=group_id,
                    card_biz_id=card_biz_id,
                    card_data=card_data,
                    retry_auth=False,
                )
                return
            raise

    def _update_group_card_with_token(
        self,
        *,
        card_biz_id: str,
        card_data: dict[str, object],
        retry_auth: bool,
    ) -> None:
        token = self._get_access_token()
        try:
            self._request_json(
                "https://api.dingtalk.com/v1.0/card/instances",
                {
                    "outTrackId": card_biz_id,
                    "cardData": {
                        "cardParamMap": {**card_data, "flowStatus": "3"}
                    },
                },
                headers={"x-acs-dingtalk-access-token": token},
                method="PUT",
            )
        except DingTalkMessagingError as exc:
            if retry_auth and exc.status_code in {401, 403}:
                self._clear_token()
                self._update_group_card_with_token(
                    card_biz_id=card_biz_id,
                    card_data=card_data,
                    retry_auth=False,
                )
                return
            raise

    def _clear_token(self) -> None:
        with self._token_lock:
            self._token = None

    def _get_access_token(self) -> str:
        with self._token_lock:
            now = self._clock()
            if self._token is not None and self._token.expires_at > now + 60:
                return self._token.value
            result = self._request_json(
                "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                {
                    "appKey": self._app_key,
                    "appSecret": self._app_secret,
                },
            )
            token = result.get("accessToken")
            expires_in = result.get("expireIn")
            if (
                not isinstance(token, str)
                or not token.strip()
                or isinstance(expires_in, bool)
                or not isinstance(expires_in, int)
                or expires_in <= 0
            ):
                raise DingTalkMessagingError(
                    None,
                    "DingTalk returned an invalid access token response.",
                )
            self._token = _AccessToken(
                value=token.strip(),
                expires_at=now + expires_in,
            )
            return self._token.value

    def _request_json(
        self,
        url: str,
        payload: dict[str, object],
        *,
        headers: dict[str, str] | None = None,
        method: str = "POST",
    ) -> dict[str, object]:
        request_headers = {
            "Content-Type": "application/json; charset=utf-8",
            **(headers or {}),
        }
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=request_headers,
            method=method,
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            raise DingTalkMessagingError(
                exc.code,
                _read_error_message(exc),
            ) from exc
        except URLError as exc:
            raise DingTalkMessagingError(
                None,
                f"DingTalk messaging API is unreachable: {exc.reason}",
            ) from exc
        if not raw:
            return {}
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DingTalkMessagingError(
                None,
                "DingTalk messaging API returned invalid JSON.",
            ) from exc
        if not isinstance(result, dict):
            raise DingTalkMessagingError(
                None,
                "DingTalk messaging API returned an invalid response.",
            )
        return result


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required.")
    return value.strip()


def _require_card_data(value: dict[str, object]) -> dict[str, object]:
    if not isinstance(value, dict) or not value:
        raise ValueError("card_data is required.")
    return value


def _read_error_message(error: HTTPError) -> str:
    try:
        payload = json.loads(error.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return f"DingTalk messaging API returned HTTP {error.code}."
    if not isinstance(payload, dict):
        return f"DingTalk messaging API returned HTTP {error.code}."
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        return f"DingTalk messaging API returned HTTP {error.code}."
    return message.strip()[:500]
