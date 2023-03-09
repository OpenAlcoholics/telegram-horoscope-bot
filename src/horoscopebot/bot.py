import functools
import logging
import signal
import time
from dataclasses import dataclass
from typing import Callable, Optional, List, Self

import pendulum
from pendulum.tz.timezone import Timezone
from rate_limit import RateLimiter
from requests import Response, Session, exceptions as requests_exceptions

from horoscopebot.config import TelegramConfig
from horoscopebot.dementia_responder import DementiaResponder
from horoscopebot.event.publisher import EventPublisher, Event, EventPublishingException
from horoscopebot.horoscope.horoscope import Horoscope, HoroscopeResult

_LOG = logging.getLogger(__name__)


def _build_session(default_timeout: float | int = 60) -> Session:
    session = Session()
    request_with_default_timeout = functools.partial(
        session.request,
        timeout=default_timeout,
    )
    session.request = request_with_default_timeout  # type: ignore
    return session


class RateLimitException(Exception):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after

    @classmethod
    def from_response(cls, response: Response) -> Self:
        parameters = response.json()["parameters"]
        if parameters:
            return cls(retry_after=float(parameters["retry_after"]))

        return cls(retry_after=60.0)


@dataclass
class HoroscopeEvent(Event):
    message_id: int
    user_id: int
    horoscope: str


class Bot:
    def __init__(
        self,
        config: TelegramConfig,
        horoscope: Horoscope,
        event_publisher: EventPublisher,
        rate_limiter: RateLimiter,
        timezone: Timezone,
    ):
        self.config = config
        self.horoscope = horoscope
        self._event_publisher = event_publisher
        self._rate_limiter = rate_limiter
        self._timezone = timezone
        self._dementia_responder = DementiaResponder()
        self._session = _build_session()
        self._should_terminate = False

    def run(self):
        signal.signal(signal.SIGTERM, self._on_kill)
        signal.signal(signal.SIGINT, self._on_kill)
        self._handle_updates(self._handle_update)

    def _on_kill(self, kill_signal: int, _):
        _LOG.info(
            "Received %s signal, requesting termination...",
            signal.Signals(kill_signal).name,
        )
        self._should_terminate = True

    def _build_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.config.token}/{method}"

    @staticmethod
    def _get_actual_body(response: Response):
        if response.status_code == 429:
            raise RateLimitException.from_response(response)

        response.raise_for_status()
        body = response.json()
        if body.get("ok"):
            return body["result"]
        raise ValueError(f"Body was not ok! {body}")

    def _send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None,
        use_html_parsing: bool = False,
        image: bytes | None = None,
    ) -> dict:
        parsing_conf = {"parse_mode": "HTML"} if use_html_parsing else {}
        if image is None:
            response = self._session.post(
                self._build_url("sendMessage"),
                json={
                    "text": text,
                    "chat_id": chat_id,
                    "reply_to_message_id": reply_to_message_id,
                    **parsing_conf,
                },
                timeout=10,
            )
        else:
            response = self._session.post(
                self._build_url("sendPhoto"),
                data={
                    "caption": text,
                    "chat_id": chat_id,
                    "reply_to_message_id": reply_to_message_id,
                    **parsing_conf,
                },
                files={
                    "photo": image,
                },
                timeout=20,
            )

        return self._get_actual_body(response)

    def _publish_horoscope_event(self, event: HoroscopeEvent):
        try:
            self._event_publisher.publish(event)
        except EventPublishingException as e:
            _LOG.error("Could not publish event", exc_info=e)

    @staticmethod
    def _is_lemons(dice: int) -> bool:
        return dice == 43

    def _handle_update(self, update: dict):
        message = update.get("message")

        if not message:
            _LOG.debug("Skipping non-message update")
            return

        chat_id = message["chat"]["id"]
        if chat_id not in self.config.enabled_chats:
            _LOG.debug("Not enabled in chat %d", chat_id)
            return

        dice: Optional[dict] = message.get("dice")
        if not dice:
            _LOG.debug("Skipping non-dice message")
            return

        if dice["emoji"] != "🎰":
            _LOG.debug("Skipping non-slot-machine message")
            return

        timestamp = message["date"]
        time = pendulum.from_timestamp(timestamp).in_timezone(self._timezone)
        user_id = message["from"]["id"]
        message_id = message["message_id"]
        dice_value = dice["value"]

        conflicting_usage = self._rate_limiter.get_offending_usage(
            context_id=chat_id,
            user_id=user_id,
            at_time=time,
        )

        if conflicting_usage is not None:
            if self._is_lemons(dice_value):
                # The other bot will send the picture anyway, so we'll be quiet
                return

            response = self._dementia_responder.create_response(
                current_message_id=message_id,
                current_message_time=time,
                usage=conflicting_usage,
            )
            reply_message_id = response.reply_message_id or message_id
            self._send_message(
                chat_id=chat_id,
                reply_to_message_id=reply_message_id,
                text=response.text,
            )
            return

        horoscope_result: HoroscopeResult | None = None
        if not self._is_lemons(dice_value):
            horoscope_result = self.horoscope.provide_horoscope(
                dice=dice_value,
                context_id=chat_id,
                user_id=user_id,
                message_id=message_id,
                message_time=time,
            )

        response_id: str | None = None
        if horoscope_result is None:
            _LOG.debug(
                "Not sending horoscope because horoscope returned None for %d",
                dice["value"],
            )
        else:
            response_message = self._send_message(
                chat_id=chat_id,
                text=horoscope_result.formatted_message,
                image=horoscope_result.image,
                use_html_parsing=horoscope_result.should_use_html_parsing,
                reply_to_message_id=message_id,
            )
            response_message_id = response_message["message_id"]
            response_id = str(response_message_id)
            self._publish_horoscope_event(
                HoroscopeEvent(
                    chat_id=chat_id,
                    user_id=user_id,
                    message_id=response_message_id,
                    horoscope=horoscope_result.message,
                )
            )

        self._rate_limiter.add_usage(
            context_id=chat_id,
            user_id=user_id,
            time=time,
            reference_id=str(message_id),
            response_id=response_id,
        )

    def _request_updates(self, last_update_id: Optional[int]) -> List[dict]:
        body = {
            "timeout": 10,
        }
        if last_update_id:
            body["offset"] = last_update_id + 1

        try:
            return self._get_actual_body(
                self._session.post(
                    self._build_url("getUpdates"),
                    json=body,
                    timeout=15,
                )
            )
        except requests_exceptions.Timeout as e:
            _LOG.warning("Encountered timeout while getting updates", exc_info=e)
            return []
        except RateLimitException as e:
            _LOG.warning(
                "Sent too many requests to Telegram, retrying after %f seconds",
                e.retry_after,
            )
            time.sleep(e.retry_after)
            return []
        except requests_exceptions.HTTPError as e:
            _LOG.error("Got HTTPError when requesting updates", exc_info=e)
            return []

    def _handle_updates(self, handler: Callable[[dict], None]):
        last_update_id: Optional[int] = None
        while not self._should_terminate:
            updates = self._request_updates(last_update_id)
            try:
                for update in updates:
                    _LOG.info(f"Received update: {update}")
                    handler(update)
                    last_update_id = update["update_id"]
            except Exception as e:
                _LOG.error("Could not handle update", exc_info=e)
        _LOG.info("Stopping update handling because of terminate signal")
