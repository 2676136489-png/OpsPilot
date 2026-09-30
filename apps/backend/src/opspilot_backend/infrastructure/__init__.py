"""External-system adapters.

Business code never imports httpx / a GitHub SDK / a Kubernetes client — it
only ever talks to a provider defined here. Swapping the simulator for real
Grafana, GitHub or Kubernetes means writing a new adapter and nothing else.
"""
