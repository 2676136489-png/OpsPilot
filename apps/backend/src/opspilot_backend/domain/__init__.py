"""Pure domain layer — no framework imports allowed here.

Anything in this package must be importable without SQLAlchemy, FastAPI or
LangGraph installed. That constraint is what keeps the layering honest:
API → Application → Agent → Tool → Infrastructure → Repository.
"""
