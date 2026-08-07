"""Prompt for writing a candidate reproduction test from verification context.

Versioned: any change to the wording or the requested shape must bump
:data:`PROMPT_VERSION`, because stored tests record the version that produced
them.
"""

from __future__ import annotations

import json
from typing import Any, Final

from app.llm.schemas import LLMMessage, LLMRole

PROMPT_NAME: Final = "test_generation"
PROMPT_VERSION: Final = "v1"
PROMPT_ID: Final = f"{PROMPT_NAME}/{PROMPT_VERSION}"

SYSTEM_PROMPT: Final = """\
You write candidate reproduction tests for ReproGate. Given an analysis of a \
reported issue and a slice of the repository, you produce ONE test that would \
demonstrate the reported behaviour if the report is accurate.

The test you write will be executed automatically inside a sandbox against a \
real repository. Everything you get wrong becomes a wasted run or a misleading \
result, so the constraints below are absolute.

Rules, in order of importance:

1. USE ONLY THE SUPPLIED CONTEXT. The "Repository context" section lists every \
file you know to exist. You may import from those paths and no others. Do not \
import a path because it seems likely to exist, because it follows a \
convention, or because the issue mentioned it — if it is not listed, it does \
not exist for you.
2. NEVER INVENT A FILE. Inventing an import makes the test unloadable. If the \
piece you need was not supplied, that is insufficient context, not a gap to \
fill.
3. IF YOU CANNOT WRITE A SOUND TEST, SAY SO. Return \
`{{"outcome": "insufficient_context", "insufficient_context": {{...}}}}` and \
name what is missing. A refusal is a useful result. A plausible-looking test \
built on guesses is not.
4. NEVER CLAIM THE ISSUE REPRODUCES. You have not run anything. Describe what \
a run *would* show using `expected_outcome`, phrased conditionally. Do not \
write that the bug is confirmed, present, or real.
5. WRITE FOR THE REPOSITORY'S OWN FRAMEWORK. Use the framework named in \
"Repository analysis" and no other. Import test helpers from that framework.
6. MAKE THE TEST SELF-CONTAINED AND DETERMINISTIC. No network access, no \
reliance on wall-clock time, no dependence on files outside the repository. \
Prefer asserting the specific reported symptom over a broad smoke test.
7. NAME THE FILE FOR DISCOVERY. `filename` must be a repository-relative path \
containing `.test.` or `.spec.` with an extension matching the language, for \
example `src/config/parser.repro.test.ts`. No absolute paths, no `..`.
8. IMPORT PATHS ARE RELATIVE TO THE TEST FILE. Work out the correct number of \
`../` segments from where you placed the file to the file you are importing.

Return ONLY a single JSON object. No markdown fence, no commentary.

The JSON object must match this schema:

{schema}

Field notes:

- `assumptions`: everything you had to take for granted. Be generous here; an \
assumption you record is one a reviewer can check.
- `reasoning_summary`: two or three sentences on why this test targets the \
reported behaviour. Not a narration of the code.
- `confidence`: "high" only when the context contains the code path in \
question and the issue states the symptom precisely.
- `required_dependencies`: packages the test needs. List them even if you \
believe the repository already has them; do not claim anything about whether \
they are installed.
- `referenced_files`: the context paths the test actually imports or asserts \
against.
"""

USER_PROMPT: Final = """\
Write a reproduction test for this issue.

## Repository

{repository}

## Repository analysis

{repository_analysis}

## Issue analysis

{issue_analysis}

## Repository context

These are the ONLY files that exist for the purposes of this task.

{context_files}
"""

_NO_CONTEXT: Final = "(No repository files were supplied.)"
_SNIPPET_TRUNCATED: Final = "\n… [snippet truncated by ReproGate]"


def build_messages(
    *,
    schema: dict[str, Any],
    repository: dict[str, Any],
    repository_analysis: dict[str, Any],
    issue_analysis: dict[str, Any],
    context_files: str,
) -> tuple[LLMMessage, ...]:
    """Render the test-generation conversation.

    Args:
        schema: JSON Schema the reply must satisfy, embedded so the requested
            shape travels with the instructions.
        context_files: Pre-rendered file listing; see :func:`render_context_files`.
    """
    system = SYSTEM_PROMPT.format(schema=json.dumps(schema, indent=2, sort_keys=True))

    user = USER_PROMPT.format(
        repository=_as_block(repository),
        repository_analysis=_as_block(repository_analysis),
        issue_analysis=_as_block(issue_analysis),
        context_files=context_files or _NO_CONTEXT,
    )

    return (
        LLMMessage(role=LLMRole.SYSTEM, content=system),
        LLMMessage(role=LLMRole.USER, content=user),
    )


def render_context_files(
    files: list[tuple[str, list[str], str | None]],
    *,
    snippet_char_limit: int,
    total_char_limit: int,
) -> str:
    """Render retrieved files and their snippets into the prompt body.

    Args:
        files: ``(path, reasons, snippet)`` triples in the order they should
            appear. A snippet of ``None`` means the file is known to exist but
            its contents were not retrieved.
        snippet_char_limit: Per-snippet cap.
        total_char_limit: Overall cap; files past it are listed by path only,
            so the model still knows they exist but cannot read them.
    """
    blocks: list[str] = []
    budget = total_char_limit

    for path, reasons, snippet in files:
        header = f"### `{path}`"
        if reasons:
            header += f"\nSelected because: {'; '.join(reasons)}"

        if snippet is None:
            blocks.append(f"{header}\n(Contents not retrieved.)")
            continue

        body = snippet
        if snippet_char_limit > 0 and len(body) > snippet_char_limit:
            body = body[:snippet_char_limit] + _SNIPPET_TRUNCATED

        if len(body) > budget:
            blocks.append(f"{header}\n(Contents omitted: context budget reached.)")
            continue

        budget -= len(body)
        blocks.append(f"{header}\n\n```\n{body}\n```")

    return "\n\n".join(blocks)


def _as_block(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str)
