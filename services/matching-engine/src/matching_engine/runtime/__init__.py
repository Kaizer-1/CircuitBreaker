"""Runtime plumbing that is identical in every service.

Settings, JSON logging, Problem Details, request middleware, metrics and health checks. It is
copied into each service rather than shared; tools/check_template_drift.py keeps copies equal.
"""
