import sys

from sentry_sdk.hub import Hub
from sentry_sdk.utils import capture_internal_exceptions, event_from_exception
from sentry_sdk.integrations import Integration

from sentry_sdk._types import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Callable
    from typing import Any
    from typing import Type
    from typing import Optional

    from types import TracebackType

    Excepthook = Callable[
        [Type[BaseException], BaseException, Optional[TracebackType]],
        Any,
    ]


class ExcepthookIntegration(Integration):
    identifier = "excepthook"

    always_run = False

    def __init__(self, always_run: bool = False) -> None:
        if not isinstance(always_run, bool):
            raise ValueError(
                "Invalid value for always_run: %s (must be type boolean)"
                % (always_run,)
            )
        self.always_run = always_run

    @staticmethod
    def setup_once() -> None:
        sys.excepthook = _make_excepthook(sys.excepthook)


def _make_excepthook(old_excepthook: Excepthook) -> Excepthook:
    def sentry_sdk_excepthook(
        type_: Type[BaseException],
        value: BaseException,
        traceback: Optional[TracebackType],
    ) -> None:
        hub = Hub.current
        integration = hub.get_integration(ExcepthookIntegration)

        if integration is not None and _should_send(integration.always_run):
            # If an integration is there, a client has to be there.
            client: Any = hub.client

            with capture_internal_exceptions():
                event, hint = event_from_exception(
                    (type_, value, traceback),
                    client_options=client.options,
                    mechanism={"type": "excepthook", "handled": False},
                )
                hub.capture_event(event, hint=hint)

        return old_excepthook(type_, value, traceback)

    return sentry_sdk_excepthook


def _should_send(always_run: bool = False) -> bool:
    if always_run:
        return True

    if hasattr(sys, "ps1"):
        # Disable the excepthook for interactive Python shells, otherwise
        # every typo gets sent to Sentry.
        return False

    return True
