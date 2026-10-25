"""Signal sources for the anomaly agent: your predictive model plus observability platforms.

Each source returns normalised Signal rows for the whole estate over a window. In SERVICENOW_MODE=mock every
source serves a labelled MOCK stream that matches the demo story; otherwise a source is "not connected" until its
environment variables are set (see docs/ANOMALY_DETECTION.md).
"""
from __future__ import annotations

from .appdynamics import AppDynamicsSource
from .base import Signal, SignalSource, SourceError, load_config, mock_mode
from .dynatrace import DynatraceSource
from .model import PredictiveModel
from .splunk import SplunkSource


def observability_sources() -> list[SignalSource]:
    cfg = load_config()
    return [DynatraceSource(cfg), AppDynamicsSource(cfg), SplunkSource(cfg)]


__all__ = ["Signal", "SignalSource", "SourceError", "PredictiveModel", "observability_sources", "load_config", "mock_mode"]
