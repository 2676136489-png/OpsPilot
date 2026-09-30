"""Evaluation datasets for OpsPilot.

Nothing is imported eagerly here: the dataset module needs the simulator's
source on ``sys.path``, and making ``import opspilot_evals.datasets`` fail until
someone has run the bootstrap would be a trap for the next reader.
"""

__all__: list[str] = []
