"""Tool Layer — the only sanctioned way for the Agent to touch the world.

Rules enforced here (not by convention):
  * one registry, Pydantic-validated input and output
  * every call has a timeout, a retry budget and a typed error
  * every call declares permission level and risk level
  * high/critical risk cannot execute without an APPROVED approval
  * every call is persisted (tool_calls) and audited (audit_logs)
"""
