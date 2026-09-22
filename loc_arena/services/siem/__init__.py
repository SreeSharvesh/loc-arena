"""SIEM: in-scenario mirror event log (tamperable).

Enforces the sealed-vs-tamperable isolation.

Owns the in-scenario event log the monitors and any investigator read (the mirror side). Its divergence from
the sealed recorder is a first-class signal for log-blinding.
"""
