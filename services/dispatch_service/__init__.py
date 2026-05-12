"""Dispatch invoice ingest as an MCP service.

Exposes CRUD operations over Candor Foods' Dispatch Sheet (CFPL / CDPL /
SERVICE CFPL tabs). Heavy lifting is delegated to dispatch_ingest/ for the
PDF parsing + row building; this service wraps it as MCP tools that take
pydantic models in and return pydantic responses out.
"""
