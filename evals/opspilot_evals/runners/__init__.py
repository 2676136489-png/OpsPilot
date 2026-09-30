"""Evaluation runners.

Import ``opspilot_evals.runners.eval_runner`` directly — it has to install the
simulator on ``sys.path`` and set the eval-mode flag *before* the backend is
imported, so re-exporting it here would only move the ordering trap.
"""

__all__: list[str] = []
