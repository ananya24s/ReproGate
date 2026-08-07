"""HTTP interface layer.

Exposes endpoints, validates incoming requests, and converts domain results
into API responses. It must not contain repository analysis, LLM, Docker, or
classification logic.
"""
