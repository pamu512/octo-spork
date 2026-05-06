"""Diff Manager — oversized PR prompts are reviewed in per-module chunks, then merged.

Implementation is in ``overlays/agenticseek/sources/grounded_review.py`` (``_run_chunked_grounded_synthesis``),
triggered from ``_run_grounded_review_from_snapshot_impl`` when the **full synthesis prompt** exceeds a token
budget (see ``estimate_token_units(build_grounded_review_prompt(...))``).

Environment variables (also documented in ``deploy/local-ai/.env.example``):

- ``GROUNDED_DIFF_CHUNKING_ENABLED`` — default ``true``; set ``false`` to force single-pass only.
- ``GROUNDED_DIFF_CHUNK_PROMPT_TOKEN_THRESHOLD`` — default ``8000``; chunk when estimated prompt tokens exceed this **and** the snapshot contains more than one file.
- ``GROUNDED_DIFF_MODULE_DEPTH`` — default ``1``; group files by this many leading path segments (``src/…`` vs ``tests/…``).
- ``GROUNDED_DIFF_MERGE_SYNTHESIS`` — default ``true``; run a short consolidating LLM pass after all chunks.

Related: ``GROUNDED_REVIEW_CHARS_PER_TOKEN`` (default ``4``) scales token estimates; ``GROUNDED_REVIEW_NUM_CTX``
sets Ollama ``num_ctx`` for each synthesis call.
"""
