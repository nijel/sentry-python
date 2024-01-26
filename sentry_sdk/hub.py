from __future__ import annotations

import copy
from contextlib import contextmanager

from sentry_sdk._compat import with_metaclass
from sentry_sdk.consts import INSTRUMENTER
from sentry_sdk.scope import Scope
from sentry_sdk.client import Client
from sentry_sdk.tracing import (
    NoOpSpan,
    Span,
    Transaction,
)

from sentry_sdk.utils import (
    logger,
    ContextVar,
)

from sentry_sdk._types import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any
    from typing import Callable
    from typing import ContextManager
    from typing import Dict
    from typing import Generator
    from typing import List
    from typing import Optional
    from typing import overload
    from typing import Tuple
    from typing import Type
    from typing import TypeVar
    from typing import Union

    from sentry_sdk.integrations import Integration
    from sentry_sdk._types import (
        Event,
        Hint,
        Breadcrumb,
        BreadcrumbHint,
        ExcInfo,
    )
    from sentry_sdk.consts import ClientConstructor

    T = TypeVar("T")

else:

    def overload(x: T) -> T:
        return x


_local = ContextVar("sentry_current_hub")


def _should_send_default_pii() -> bool:
    client = Hub.current.client
    if not client:
        return False
    return client.options["send_default_pii"]


class _InitGuard:
    def __init__(self, client: Client) -> None:
        self._client = client

    def __enter__(self) -> _InitGuard:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, tb: Any) -> None:
        c = self._client
        if c is not None:
            c.close()


def _check_python_deprecations() -> None:
    # Since we're likely to deprecate Python versions in the future, I'm keeping
    # this handy function around. Use this to detect the Python version used and
    # to output logger.warning()s if it's deprecated.
    pass


def _init(*args: Optional[str], **kwargs: Any) -> ContextManager[Any]:
    """Initializes the SDK and optionally integrations.

    This takes the same arguments as the client constructor.
    """
    client = Client(*args, **kwargs)  # type: ignore
    Hub.current.bind_client(client)
    _check_python_deprecations()
    rv = _InitGuard(client)
    return rv


from sentry_sdk._types import TYPE_CHECKING

if TYPE_CHECKING:
    # Make mypy, PyCharm and other static analyzers think `init` is a type to
    # have nicer autocompletion for params.
    #
    # Use `ClientConstructor` to define the argument types of `init` and
    # `ContextManager[Any]` to tell static analyzers about the return type.

    class init(ClientConstructor, _InitGuard):  # noqa: N801
        pass

else:
    # Alias `init` for actual usage. Go through the lambda indirection to throw
    # PyCharm off of the weakly typed signature (it would otherwise discover
    # both the weakly typed signature of `_init` and our faked `init` type).

    init = (lambda: _init)()


class HubMeta(type):
    @property
    def current(cls) -> Hub:
        """Returns the current instance of the hub."""
        rv = _local.get(None)
        if rv is None:
            rv = Hub(GLOBAL_HUB)
            _local.set(rv)
        return rv

    @property
    def main(cls) -> Hub:
        """Returns the main instance of the hub."""
        return GLOBAL_HUB


class _ScopeManager:
    def __init__(self, hub: Hub) -> None:
        self._hub = hub
        self._original_len = len(hub._stack)
        self._layer = hub._stack[-1]

    def __enter__(self) -> Scope:
        scope = self._layer[1]
        assert scope is not None
        return scope

    def __exit__(self, exc_type: Any, exc_value: Any, tb: Any) -> None:
        current_len = len(self._hub._stack)
        if current_len < self._original_len:
            logger.error(
                "Scope popped too soon. Popped %s scopes too many.",
                self._original_len - current_len,
            )
            return
        elif current_len > self._original_len:
            logger.warning(
                "Leaked %s scopes: %s",
                current_len - self._original_len,
                self._hub._stack[self._original_len :],
            )

        layer = self._hub._stack[self._original_len - 1]
        del self._hub._stack[self._original_len - 1 :]

        if layer[1] != self._layer[1]:
            logger.error(
                "Wrong scope found. Meant to pop %s, but popped %s.",
                layer[1],
                self._layer[1],
            )
        elif layer[0] != self._layer[0]:
            warning = (
                "init() called inside of pushed scope. This might be entirely "
                "legitimate but usually occurs when initializing the SDK inside "
                "a request handler or task/job function. Try to initialize the "
                "SDK as early as possible instead."
            )
            logger.warning(warning)


class Hub(with_metaclass(HubMeta)):  # type: ignore
    """The hub wraps the concurrency management of the SDK.  Each thread has
    its own hub but the hub might transfer with the flow of execution if
    context vars are available.

    If the hub is used with a with statement it's temporarily activated.
    """

    _stack: List[Tuple[Optional[Client], Scope]] = None

    # Mypy doesn't pick up on the metaclass.

    if TYPE_CHECKING:
        current: Hub = None
        main: Hub = None

    def __init__(
        self,
        client_or_hub: Optional[Union[Hub, Client]] = None,
        scope: Optional[Any] = None,
    ) -> None:
        if isinstance(client_or_hub, Hub):
            hub = client_or_hub
            client, other_scope = hub._stack[-1]
            if scope is None:
                scope = copy.copy(other_scope)
        else:
            client = client_or_hub
        if scope is None:
            scope = Scope()

        self._stack = [(client, scope)]
        self._last_event_id: Optional[str] = None
        self._old_hubs: List[Hub] = []

    def __enter__(self) -> Hub:
        self._old_hubs.append(Hub.current)
        _local.set(self)
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_value: Optional[BaseException],
        tb: Optional[Any],
    ) -> None:
        old = self._old_hubs.pop()
        _local.set(old)

    def run(self, callback: Callable[[], T]) -> T:
        """Runs a callback in the context of the hub.  Alternatively the
        with statement can be used on the hub directly.
        """
        with self:
            return callback()

    def get_integration(self, name_or_class: Union[str, Type[Integration]]) -> Any:
        """Returns the integration for this hub by name or class.  If there
        is no client bound or the client does not have that integration
        then `None` is returned.

        If the return value is not `None` the hub is guaranteed to have a
        client attached.
        """
        client = self.client
        if client is not None:
            return client.get_integration(name_or_class)

    @property
    def client(self) -> Optional[Client]:
        """Returns the current client on the hub."""
        return self._stack[-1][0]

    @property
    def scope(self) -> Scope:
        """Returns the current scope on the hub."""
        return self._stack[-1][1]

    def last_event_id(self) -> Optional[str]:
        """Returns the last event ID."""
        return self._last_event_id

    def bind_client(self, new: Optional[Client]) -> None:
        """Binds a new client to the hub."""
        top = self._stack[-1]
        self._stack[-1] = (new, top[1])

    def capture_event(
        self,
        event: Event,
        hint: Optional[Hint] = None,
        scope: Optional[Scope] = None,
        **scope_kwargs: Any,
    ) -> Optional[str]:
        """
        Captures an event.

        Alias of :py:meth:`sentry_sdk.Scope.capture_event`.

        :param event: A ready-made event that can be directly sent to Sentry.

        :param hint: Contains metadata about the event that can be read from `before_send`, such as the original exception object or a HTTP request object.

        :param scope: An optional :py:class:`sentry_sdk.Scope` to apply to events.
            The `scope` and `scope_kwargs` parameters are mutually exclusive.

        :param scope_kwargs: Optional data to apply to event.
            For supported `**scope_kwargs` see :py:meth:`sentry_sdk.Scope.update_from_kwargs`.
            The `scope` and `scope_kwargs` parameters are mutually exclusive.
        """
        client, top_scope = self._stack[-1]
        if client is None:
            return None

        last_event_id = top_scope.capture_event(
            event, hint, client=client, scope=scope, **scope_kwargs
        )

        is_transaction = event.get("type") == "transaction"
        if last_event_id is not None and not is_transaction:
            self._last_event_id = last_event_id

        return last_event_id

    def capture_message(
        self,
        message: str,
        level: Optional[str] = None,
        scope: Optional[Scope] = None,
        **scope_kwargs: Any,
    ) -> Optional[str]:
        """
        Captures a message.

        Alias of :py:meth:`sentry_sdk.Scope.capture_message`.

        :param message: The string to send as the message to Sentry.

        :param level: If no level is provided, the default level is `info`.

        :param scope: An optional :py:class:`sentry_sdk.Scope` to apply to events.
            The `scope` and `scope_kwargs` parameters are mutually exclusive.

        :param scope_kwargs: Optional data to apply to event.
            For supported `**scope_kwargs` see :py:meth:`sentry_sdk.Scope.update_from_kwargs`.
            The `scope` and `scope_kwargs` parameters are mutually exclusive.

        :returns: An `event_id` if the SDK decided to send the event (see :py:meth:`sentry_sdk.Client.capture_event`).
        """
        client, top_scope = self._stack[-1]
        if client is None:
            return None

        last_event_id = top_scope.capture_message(
            message, level=level, client=client, scope=scope, **scope_kwargs
        )

        if last_event_id is not None:
            self._last_event_id = last_event_id

        return last_event_id

    def capture_exception(
        self,
        error: Optional[Union[BaseException, ExcInfo]] = None,
        scope: Optional[Scope] = None,
        **scope_kwargs: Any,
    ) -> Optional[str]:
        """Captures an exception.

        Alias of :py:meth:`sentry_sdk.Scope.capture_exception`.

        :param error: An exception to capture. If `None`, `sys.exc_info()` will be used.

        :param scope: An optional :py:class:`sentry_sdk.Scope` to apply to events.
            The `scope` and `scope_kwargs` parameters are mutually exclusive.

        :param scope_kwargs: Optional data to apply to event.
            For supported `**scope_kwargs` see :py:meth:`sentry_sdk.Scope.update_from_kwargs`.
            The `scope` and `scope_kwargs` parameters are mutually exclusive.

        :returns: An `event_id` if the SDK decided to send the event (see :py:meth:`sentry_sdk.Client.capture_event`).
        """
        client, top_scope = self._stack[-1]
        if client is None:
            return None

        last_event_id = top_scope.capture_exception(
            error, client=client, scope=scope, **scope_kwargs
        )

        if last_event_id is not None:
            self._last_event_id = last_event_id

        return last_event_id

    def _capture_internal_exception(self, exc_info: Any) -> Any:
        """
        Capture an exception that is likely caused by a bug in the SDK
        itself.

        Duplicated in :py:meth:`sentry_sdk.Client._capture_internal_exception`.

        These exceptions do not end up in Sentry and are just logged instead.
        """
        logger.error("Internal error in sentry_sdk", exc_info=exc_info)

    def add_breadcrumb(
        self,
        crumb: Optional[Breadcrumb] = None,
        hint: Optional[BreadcrumbHint] = None,
        **kwargs: Any,
    ) -> None:
        """
        Adds a breadcrumb.

        :param crumb: Dictionary with the data as the sentry v7/v8 protocol expects.

        :param hint: An optional value that can be used by `before_breadcrumb`
            to customize the breadcrumbs that are emitted.
        """
        client, scope = self._stack[-1]
        if client is None:
            logger.info("Dropped breadcrumb because no client bound")
            return

        kwargs["client"] = client

        scope.add_breadcrumb(crumb, hint, **kwargs)

    def start_span(
        self,
        span: Optional[Span] = None,
        instrumenter: str = INSTRUMENTER.SENTRY,
        **kwargs: Any,
    ) -> Span:
        """
        Start a span whose parent is the currently active span or transaction, if any.

        The return value is a :py:class:`sentry_sdk.tracing.Span` instance,
        typically used as a context manager to start and stop timing in a `with`
        block.

        Only spans contained in a transaction are sent to Sentry. Most
        integrations start a transaction at the appropriate time, for example
        for every incoming HTTP request. Use
        :py:meth:`sentry_sdk.start_transaction` to start a new transaction when
        one is not already in progress.

        For supported `**kwargs` see :py:class:`sentry_sdk.tracing.Span`.
        """
        client, scope = self._stack[-1]

        kwargs["hub"] = self
        kwargs["client"] = client

        return scope.start_span(span=span, instrumenter=instrumenter, **kwargs)

    def start_transaction(
        self,
        transaction: Optional[Transaction] = None,
        instrumenter: str = INSTRUMENTER.SENTRY,
        **kwargs: Any,
    ) -> Union[Transaction, NoOpSpan]:
        """
        Start and return a transaction.

        Start an existing transaction if given, otherwise create and start a new
        transaction with kwargs.

        This is the entry point to manual tracing instrumentation.

        A tree structure can be built by adding child spans to the transaction,
        and child spans to other spans. To start a new child span within the
        transaction or any span, call the respective `.start_child()` method.

        Every child span must be finished before the transaction is finished,
        otherwise the unfinished spans are discarded.

        When used as context managers, spans and transactions are automatically
        finished at the end of the `with` block. If not using context managers,
        call the `.finish()` method.

        When the transaction is finished, it will be sent to Sentry with all its
        finished child spans.

        For supported `**kwargs` see :py:class:`sentry_sdk.tracing.Transaction`.
        """
        client, scope = self._stack[-1]

        kwargs["hub"] = self
        kwargs["client"] = client

        return scope.start_transaction(
            transaction=transaction, instrumenter=instrumenter, **kwargs
        )

    def continue_trace(
        self,
        environ_or_headers: Dict[str, Any],
        op: Optional[str] = None,
        name: Optional[str] = None,
        source: Optional[str] = None,
    ) -> Transaction:
        """
        Sets the propagation context from environment or headers and returns a transaction.
        """
        scope = self._stack[-1][1]

        return scope.continue_trace(
            environ_or_headers=environ_or_headers, op=op, name=name, source=source
        )

    @overload
    def push_scope(self, callback: Optional[None] = None) -> ContextManager[Scope]:
        pass

    @overload
    def push_scope(self, callback: Callable[[Scope], None]) -> None:  # noqa: F811
        pass

    def push_scope(  # noqa
        self,
        callback: Optional[Callable[[Scope], None]] = None,
        continue_trace: bool = True,
    ) -> Optional[ContextManager[Scope]]:
        """
        Pushes a new layer on the scope stack.

        :param callback: If provided, this method pushes a scope, calls
            `callback`, and pops the scope again.

        :returns: If no `callback` is provided, a context manager that should
            be used to pop the scope again.
        """
        if callback is not None:
            with self.push_scope() as scope:
                callback(scope)
            return None

        client, scope = self._stack[-1]

        new_scope = copy.copy(scope)

        if continue_trace:
            new_scope.generate_propagation_context()

        new_layer = (client, new_scope)
        self._stack.append(new_layer)

        return _ScopeManager(self)

    def pop_scope_unsafe(self) -> Tuple[Optional[Client], Scope]:
        """
        Pops a scope layer from the stack.

        Try to use the context manager :py:meth:`push_scope` instead.
        """
        rv = self._stack.pop()
        assert self._stack, "stack must have at least one layer"
        return rv

    @overload
    def configure_scope(self, callback: Optional[None] = None) -> ContextManager[Scope]:
        pass

    @overload
    def configure_scope(self, callback: Callable[[Scope], None]) -> None:  # noqa: F811
        pass

    def configure_scope(  # noqa
        self,
        callback: Optional[Callable[[Scope], None]] = None,
        continue_trace: bool = True,
    ) -> Optional[ContextManager[Scope]]:
        """
        Reconfigures the scope.

        :param callback: If provided, call the callback with the current scope.

        :returns: If no callback is provided, returns a context manager that returns the scope.
        """

        client, scope = self._stack[-1]

        if continue_trace:
            scope.generate_propagation_context()

        if callback is not None:
            if client is not None:
                callback(scope)

            return None

        @contextmanager
        def inner() -> Generator[Scope, None, None]:
            if client is not None:
                yield scope
            else:
                yield Scope()

        return inner()

    def start_session(self, session_mode: str = "application") -> None:
        """Starts a new session."""
        client, scope = self._stack[-1]
        scope.start_session(
            client=client,
            session_mode=session_mode,
        )

    def end_session(self) -> None:
        """Ends the current session if there is one."""
        client, scope = self._stack[-1]
        scope.end_session(client=client)

    def stop_auto_session_tracking(self) -> None:
        """Stops automatic session tracking.

        This temporarily session tracking for the current scope when called.
        To resume session tracking call `resume_auto_session_tracking`.
        """
        client, scope = self._stack[-1]
        scope.stop_auto_session_tracking(client=client)

    def resume_auto_session_tracking(self) -> None:
        """Resumes automatic session tracking for the current scope if
        disabled earlier.  This requires that generally automatic session
        tracking is enabled.
        """
        scope = self._stack[-1][1]
        scope.resume_auto_session_tracking()

    def flush(
        self,
        timeout: Optional[float] = None,
        callback: Optional[Callable[[int, float], None]] = None,
    ) -> None:
        """
        Alias for :py:meth:`sentry_sdk.Client.flush`
        """
        client, scope = self._stack[-1]
        if client is not None:
            return client.flush(timeout=timeout, callback=callback)

    def get_traceparent(self) -> Optional[str]:
        """
        Returns the traceparent either from the active span or from the scope.
        """
        client, scope = self._stack[-1]
        return scope.get_traceparent(client=client)

    def get_baggage(self) -> Optional[str]:
        """
        Returns Baggage either from the active span or from the scope.
        """
        client, scope = self._stack[-1]
        baggage = scope.get_baggage(client=client)

        if baggage is not None:
            return baggage.serialize()

        return None

    def iter_trace_propagation_headers(
        self, span: Optional[Span] = None
    ) -> Generator[Tuple[str, str], None, None]:
        """
        Return HTTP headers which allow propagation of trace data. Data taken
        from the span representing the request, if available, or the current
        span on the scope if not.
        """
        client, scope = self._stack[-1]

        return scope.iter_trace_propagation_headers(span=span, client=client)

    def trace_propagation_meta(self, span: Optional[Span] = None) -> str:
        """
        Return meta tags which should be injected into HTML templates
        to allow propagation of trace information.
        """
        if span is not None:
            logger.warning(
                "The parameter `span` in trace_propagation_meta() is deprecated and will be removed in the future."
            )

        client, scope = self._stack[-1]
        return scope.trace_propagation_meta(span=span, client=client)


GLOBAL_HUB = Hub()
_local.set(GLOBAL_HUB)
