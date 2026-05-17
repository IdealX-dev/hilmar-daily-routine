"""Hilmar Rate Desk Tracker — OL-USA daily reporting pipeline.

Modules:
    core          — pure functions (status machine, biz hours, request_id, parse_teu)
    body_parser   — regex parsers for email subjects + bodies
    graph_client  — MSAL confidential-client wrapper around Microsoft Graph
    ingest        — pull emails, classify, merge into tracking-data-v2.json
    qc            — 7-phase self-heal QC engine
    render        — dashboard, PDF, scorecards, email body
    send          — Graph sendMail + OneDrive upload
    orchestrator  — entrypoint; replaces orchestrator.md as code
"""

__version__ = "0.1.0"
