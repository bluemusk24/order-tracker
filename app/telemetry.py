import logging
import os
import time

from fastapi import FastAPI
from opentelemetry import _logs as otel_logs
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    ConsoleLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "order-tracker")
SERVICE_VERSION = "0.1.0"
OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
METRIC_EXPORT_INTERVAL_MS = int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL_MS", "5000"))
BATCH_DELAY_MS = int(os.getenv("OTEL_BSP_SCHEDULE_DELAY", "1000"))
TRACES_EXCLUDED_URLS = os.getenv("OTEL_TRACES_EXCLUDED_URLS", "healthz")
METRICS_EXCLUDED_URLS = os.getenv("OTEL_METRICS_EXCLUDED_URLS", "healthz")
LOGGER_NAME = "order_tracker"

lookup_duration = None


def _resource():
    return Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.version": SERVICE_VERSION,
            "deployment.environment": os.getenv("OTEL_DEPLOYMENT_ENVIRONMENT", "local"),
        }
    )


def get_logger():
    return logging.getLogger(LOGGER_NAME)


def get_tracer():
    return trace.get_tracer(SERVICE_NAME)


def get_meter():
    return metrics.get_meter(SERVICE_NAME)


def _span_processor():
    if OTLP_ENDPOINT:
        return BatchSpanProcessor(OTLPSpanExporter())
    return SimpleSpanProcessor(ConsoleSpanExporter())


def _log_record_processor():
    if OTLP_ENDPOINT:
        return BatchLogRecordProcessor(
            OTLPLogExporter(), schedule_delay_millis=BATCH_DELAY_MS
        )
    return SimpleLogRecordProcessor(ConsoleLogRecordExporter())


def _metric_exporter():
    if OTLP_ENDPOINT:
        return OTLPMetricExporter()
    return ConsoleMetricExporter()


def configure(app: FastAPI):
    resource = _resource()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(_span_processor())
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(
        _metric_exporter(),
        export_interval_millis=METRIC_EXPORT_INTERVAL_MS,
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(_log_record_processor())
    otel_logs.set_logger_provider(logger_provider)

    logger = get_logger()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))

    global lookup_duration
    lookup_duration = get_meter().create_histogram(
        "orders.lookup.duration",
        unit="s",
        description="Time spent looking up a single order in the database.",
    )

    FastAPIInstrumentor.instrument_app(app, excluded_urls=TRACES_EXCLUDED_URLS)


def shutdown():
    for provider in (
        trace.get_tracer_provider(),
        metrics.get_meter_provider(),
        otel_logs.get_logger_provider(),
    ):
        shutdown_provider = getattr(provider, "shutdown", None)
        if callable(shutdown_provider):
            shutdown_provider()


def record_lookup(order: dict | None, elapsed: float):
    attributes = {"order.found": order is not None}
    if order is not None:
        attributes["order.status"] = order["status"]
        attributes["order.priority"] = order["priority"]
    if lookup_duration is not None:
        lookup_duration.record(elapsed, attributes)


class RequestMetricsMiddleware:
    def __init__(self, app, meter, logger, excluded_urls: str = METRICS_EXCLUDED_URLS):
        self.app = app
        self.logger = logger
        self.excluded_urls = excluded_urls
        self.request_count = meter.create_counter(
            "http.server.request.count",
            unit="{request}",
            description="HTTP server requests handled, by route and response status code.",
        )
        self.request_duration = meter.create_histogram(
            "http.server.request.duration",
            unit="s",
            description="Duration of HTTP server requests, by route and response status code.",
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or _is_excluded(scope, self.excluded_urls):
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        state = {"status": 500, "span": None}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                state["span"] = trace.get_current_span()
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            self._record(scope, state, time.perf_counter() - started)

    def _record(self, scope, state, elapsed):
        attributes = {
            "http.route": _route_template(scope),
            "http.request.method": scope.get("method", ""),
            "http.response.status_code": state["status"],
        }
        self.request_count.add(1, attributes)
        self.request_duration.record(elapsed, attributes)

        span = state["span"]
        if span is None:
            self.logger.error(
                "http_request_failed_before_response",
                extra={**attributes, "http.server.request.duration": elapsed},
            )
        else:
            with trace.use_span(span, end_on_exit=False, record_exception=False):
                self.logger.info(
                    "http_request",
                    extra={**attributes, "http.server.request.duration": elapsed},
                )


def _route_template(scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path or scope.get("path", "unmatched")


def _is_excluded(scope, excluded_urls: str) -> bool:
    if not excluded_urls:
        return False
    return any(pattern in scope.get("path", "") for pattern in excluded_urls.split(","))
