"""Unit tests for deterministic relevant file retrieval."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.config import Settings
from app.repository_analysis import (
    ContextRetriever,
    FileIndexer,
    KeywordSource,
    RepositoryDetector,
    RetrievalResult,
    RetrievalSignal,
)
from app.schemas.github import GitHubIssue, GitHubIssueState


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def retriever(settings: Settings) -> ContextRetriever:
    return ContextRetriever(settings)


def write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def make_issue(
    title: str, body: str | None = None, labels: tuple[str, ...] = ()
) -> GitHubIssue:
    return GitHubIssue(
        number=42,
        title=title,
        body=body,
        state=GitHubIssueState.OPEN,
        author="reporter",
        labels=labels,
        html_url="https://github.com/octocat/hello-world/issues/42",
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small TypeScript project with a realistic import graph."""
    root = tmp_path / "repo"
    write(
        root / "package.json",
        json.dumps(
            {
                "name": "widget",
                "scripts": {"test": "vitest run"},
                "dependencies": {"zod": "^3.0.0"},
                "devDependencies": {"vitest": "^2.0.0"},
            }
        ),
    )
    write(root / "src" / "index.ts", "import { parseConfig } from './config/parser';\n")
    write(
        root / "src" / "config" / "parser.ts",
        "import { z } from 'zod';\nimport { readSettings } from './settings';\n"
        "export function parseConfig() {}\n",
    )
    write(
        root / "src" / "config" / "settings.ts",
        "export const readSettings = () => {};\n",
    )
    write(root / "src" / "unrelated" / "logger.ts", "export const log = () => {};\n")
    write(root / "tests" / "parser.test.ts", "import '../src/config/parser';\n")
    return root


async def index_of(root: Path, settings: Settings):
    return await FileIndexer(settings).index(root)


def paths_of(result: RetrievalResult) -> list[str]:
    return [candidate.path for candidate in result.candidates]


def candidate_for(result: RetrievalResult, path: str):
    for candidate in result.candidates:
        if candidate.path == path:
            return candidate
    raise AssertionError(f"{path} is not among {paths_of(result)}")


def signals_of(result: RetrievalResult, path: str) -> set[RetrievalSignal]:
    return {match.signal for match in candidate_for(result, path).signals}


# --------------------------------------------------------------------------
# Keyword extraction
# --------------------------------------------------------------------------


async def test_extracts_keywords_from_title_body_and_labels(
    retriever: ContextRetriever, settings: Settings, repo: Path
) -> None:
    issue = make_issue(
        "parseConfig crashes",
        body="The settings loader misbehaves.",
        labels=("regression",),
    )

    result = await retriever.retrieve(issue, await index_of(repo, settings))

    terms = {keyword.term for keyword in result.keywords}
    assert {"parseconfig", "parse", "config", "crashes"} <= terms
    assert "settings" in terms
    assert "regression" in terms

    by_term = {keyword.term: keyword for keyword in result.keywords}
    assert KeywordSource.TITLE in by_term["parseconfig"].sources
    assert KeywordSource.BODY in by_term["settings"].sources
    assert KeywordSource.LABEL in by_term["regression"].sources


async def test_stopwords_and_short_tokens_are_dropped(
    retriever: ContextRetriever, settings: Settings, repo: Path
) -> None:
    issue = make_issue(
        "The parser is broken",
        body="Steps to reproduce: it does not work as expected.",
    )

    result = await retriever.retrieve(issue, await index_of(repo, settings))

    terms = {keyword.term for keyword in result.keywords}
    assert "parser" in terms
    assert "broken" in terms
    for noise in ("the", "is", "it", "steps", "reproduce", "expected", "not"):
        assert noise not in terms


async def test_code_fences_do_not_pollute_keywords(
    retriever: ContextRetriever, settings: Settings, repo: Path
) -> None:
    issue = make_issue(
        "parser crash",
        body="```\nconsole.log(someUnrelatedIdentifier)\n```",
    )

    result = await retriever.retrieve(issue, await index_of(repo, settings))

    terms = {keyword.term for keyword in result.keywords}
    assert "parser" in terms
    assert "someunrelatedidentifier" not in terms


# --------------------------------------------------------------------------
# Filename, path, and directory matching
# --------------------------------------------------------------------------


async def test_filename_match_outranks_directory_match(
    retriever: ContextRetriever, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    write(root / "src" / "parser.ts")
    write(root / "parser" / "other.ts")

    result = await retriever.retrieve(
        make_issue("parser is broken"), await index_of(root, settings)
    )

    assert paths_of(result)[0] == "src/parser.ts"
    assert RetrievalSignal.FILENAME in signals_of(result, "src/parser.ts")
    assert RetrievalSignal.DIRECTORY in signals_of(result, "parser/other.ts")
    assert (
        candidate_for(result, "src/parser.ts").score
        > candidate_for(result, "parser/other.ts").score
    )


async def test_path_segment_and_directory_signals_are_distinct(
    retriever: ContextRetriever, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    write(root / "config" / "nested" / "value.ts")

    result = await retriever.retrieve(
        make_issue("config problem"), await index_of(root, settings)
    )

    # `config` is an ancestor, not the immediate parent, so it is a path match.
    assert signals_of(result, "config/nested/value.ts") == {
        RetrievalSignal.PATH_SEGMENT
    }


async def test_title_keywords_weigh_more_than_body_keywords(
    retriever: ContextRetriever, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    write(root / "alpha.ts")
    write(root / "beta.ts")

    result = await retriever.retrieve(
        make_issue("alpha fails", body="possibly related to beta"),
        await index_of(root, settings),
    )

    assert (
        candidate_for(result, "alpha.ts").score > candidate_for(result, "beta.ts").score
    )
    assert (
        candidate_for(result, "alpha.ts").signals[0].keyword_source
        is KeywordSource.TITLE
    )
    assert (
        candidate_for(result, "beta.ts").signals[0].keyword_source is KeywordSource.BODY
    )


async def test_camel_and_snake_case_filenames_match(
    retriever: ContextRetriever, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    write(root / "parseConfig.ts")
    write(root / "parse_config.py")

    result = await retriever.retrieve(
        make_issue("config parsing fails"), await index_of(root, settings)
    )

    assert set(paths_of(result)) == {"parseConfig.ts", "parse_config.py"}


# --------------------------------------------------------------------------
# Explicit path references
# --------------------------------------------------------------------------


async def test_explicit_path_reference_ranks_first(
    retriever: ContextRetriever, settings: Settings, repo: Path
) -> None:
    issue = make_issue(
        "Crash on startup", body="The failure is in `src/config/settings.ts`."
    )

    result = await retriever.retrieve(issue, await index_of(repo, settings))

    assert result.referenced_paths == ("src/config/settings.ts",)
    assert paths_of(result)[0] == "src/config/settings.ts"
    assert RetrievalSignal.EXPLICIT_PATH in signals_of(result, "src/config/settings.ts")


@pytest.mark.parametrize(
    "reference",
    [
        pytest.param("src/config/settings.ts", id="relative"),
        pytest.param("./src/config/settings.ts", id="dot-relative"),
        pytest.param("/home/dev/project/src/config/settings.ts", id="absolute"),
    ],
)
async def test_path_references_resolve_in_several_forms(
    retriever: ContextRetriever, settings: Settings, repo: Path, reference: str
) -> None:
    result = await retriever.retrieve(
        make_issue("crash", body=f"see {reference} line 12"),
        await index_of(repo, settings),
    )

    assert "src/config/settings.ts" in result.referenced_paths


async def test_ambiguous_bare_filename_is_not_treated_as_a_path(
    retriever: ContextRetriever, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    write(root / "a" / "index.ts")
    write(root / "b" / "index.ts")

    result = await retriever.retrieve(
        make_issue("broken", body="look at index.ts"), await index_of(root, settings)
    )

    assert result.referenced_paths == ()


async def test_unique_bare_filename_resolves(
    retriever: ContextRetriever, settings: Settings, repo: Path
) -> None:
    result = await retriever.retrieve(
        make_issue("broken", body="look at settings.ts"),
        await index_of(repo, settings),
    )

    assert result.referenced_paths == ("src/config/settings.ts",)


async def test_path_references_cannot_escape_the_repository(
    retriever: ContextRetriever, settings: Settings, repo: Path
) -> None:
    result = await retriever.retrieve(
        make_issue("bad", body="see ../../etc/passwd.txt and /etc/hosts.conf"),
        await index_of(repo, settings),
    )

    assert result.referenced_paths == ()


# --------------------------------------------------------------------------
# Import graph traversal
# --------------------------------------------------------------------------


async def test_traversal_reaches_imported_and_importing_files(
    retriever: ContextRetriever, settings: Settings, repo: Path
) -> None:
    # A title with no filename-matching keywords, so the only direct match —
    # and therefore the only seed — is the explicitly referenced file.
    issue = make_issue("Crash during startup", body="Fails in `src/config/parser.ts`.")

    result = await retriever.retrieve(issue, await index_of(repo, settings), depth=1)

    assert result.seed_paths == ("src/config/parser.ts",)
    # Imported by the seed, and importers of the seed, are both one hop away.
    assert candidate_for(result, "src/config/settings.ts").traversal_distance == 1
    assert candidate_for(result, "src/index.ts").traversal_distance == 1
    assert candidate_for(result, "tests/parser.test.ts").traversal_distance == 1


async def test_traversal_depth_is_configurable(
    retriever: ContextRetriever, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    write(root / "seed.ts", "import './one';\n")
    write(root / "one.ts", "import './two';\n")
    write(root / "two.ts", "import './three';\n")
    write(root / "three.ts", "export const x = 1;\n")

    index = await index_of(root, settings)
    issue = make_issue("seed is broken")

    shallow = await retriever.retrieve(issue, index, depth=1)
    deeper = await retriever.retrieve(issue, index, depth=2)
    none = await retriever.retrieve(issue, index, depth=0)

    assert paths_of(shallow) == ["seed.ts", "one.ts"]
    assert set(paths_of(deeper)) == {"seed.ts", "one.ts", "two.ts"}
    assert paths_of(none) == ["seed.ts"]
    assert none.traversal_depth == 0


async def test_traversal_distance_decays_the_weight(
    retriever: ContextRetriever, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    write(root / "seed.ts", "import './one';\n")
    write(root / "one.ts", "import './two';\n")
    write(root / "two.ts", "export const x = 1;\n")

    result = await retriever.retrieve(
        make_issue("seed is broken"), await index_of(root, settings), depth=2
    )

    assert candidate_for(result, "one.ts").score > candidate_for(result, "two.ts").score
    assert candidate_for(result, "one.ts").traversal_distance == 1
    assert candidate_for(result, "two.ts").traversal_distance == 2


@pytest.mark.parametrize(
    ("statement", "target"),
    [
        pytest.param("import { a } from './target';", "target.ts", id="named-import"),
        pytest.param("import './target';", "target.ts", id="side-effect-import"),
        pytest.param("export { a } from './target';", "target.ts", id="re-export"),
        pytest.param("const a = require('./target');", "target.ts", id="require"),
        pytest.param("const a = await import('./target');", "target.ts", id="dynamic"),
        pytest.param(
            "import { a } from './target.js';", "target.ts", id="ts-esm-js-ext"
        ),
        pytest.param(
            "import { a } from './nested';", "nested/index.ts", id="directory-index"
        ),
    ],
)
async def test_import_forms_are_resolved(
    retriever: ContextRetriever,
    settings: Settings,
    tmp_path: Path,
    statement: str,
    target: str,
) -> None:
    root = tmp_path / "repo"
    write(root / "seed.ts", f"{statement}\n")
    write(root / "target.ts", "export const a = 1;\n")
    write(root / "nested" / "index.ts", "export const a = 1;\n")

    result = await retriever.retrieve(
        make_issue("seed is broken"), await index_of(root, settings), depth=1
    )

    assert candidate_for(result, target).traversal_distance == 1


async def test_imports_that_escape_the_repository_are_ignored(
    retriever: ContextRetriever, settings: Settings, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    write(outside / "secret.ts", "export const secret = 1;\n")
    root = tmp_path / "repo"
    write(root / "seed.ts", "import '../outside/secret';\nimport '../../etc/passwd';\n")

    result = await retriever.retrieve(
        make_issue("seed is broken"), await index_of(root, settings), depth=2
    )

    assert paths_of(result) == ["seed.ts"]


async def test_files_are_never_duplicated(
    retriever: ContextRetriever, settings: Settings, repo: Path
) -> None:
    issue = make_issue(
        "parser config settings",
        body="`src/config/parser.ts` and `src/config/settings.ts` both fail",
    )

    result = await retriever.retrieve(issue, await index_of(repo, settings), depth=2)

    assert len(paths_of(result)) == len(set(paths_of(result)))


# --------------------------------------------------------------------------
# Dependency traversal
# --------------------------------------------------------------------------


async def test_dependency_mentioned_in_issue_scores_importers(
    retriever: ContextRetriever, settings: Settings, repo: Path
) -> None:
    index = await index_of(repo, settings)
    analysis = await RepositoryDetector(settings).analyze(repo)

    result = await retriever.retrieve(
        make_issue("zod validation fails"), index, analysis, depth=0
    )

    assert RetrievalSignal.DEPENDENCY_IMPORT in signals_of(
        result, "src/config/parser.ts"
    )
    assert any(
        match.keyword == "zod"
        for match in candidate_for(result, "src/config/parser.ts").signals
    )


async def test_dependency_signal_requires_the_analysis(
    retriever: ContextRetriever, settings: Settings, repo: Path
) -> None:
    result = await retriever.retrieve(
        make_issue("zod validation fails"), await index_of(repo, settings), depth=0
    )

    assert all(
        RetrievalSignal.DEPENDENCY_IMPORT not in signals_of(result, path)
        for path in paths_of(result)
    )


async def test_undeclared_package_produces_no_dependency_signal(
    retriever: ContextRetriever, settings: Settings, repo: Path
) -> None:
    analysis = await RepositoryDetector(settings).analyze(repo)

    result = await retriever.retrieve(
        make_issue("lodash is broken"),
        await index_of(repo, settings),
        analysis,
        depth=0,
    )

    assert all(
        RetrievalSignal.DEPENDENCY_IMPORT not in signals_of(result, path)
        for path in paths_of(result)
    )


# --------------------------------------------------------------------------
# Explainability and shape
# --------------------------------------------------------------------------


async def test_every_candidate_explains_itself(
    retriever: ContextRetriever, settings: Settings, repo: Path
) -> None:
    result = await retriever.retrieve(
        make_issue("parser config fails", body="see `src/config/parser.ts`"),
        await index_of(repo, settings),
        depth=1,
    )

    assert result.candidates
    for candidate in result.candidates:
        assert candidate.signals
        assert candidate.explanation
        assert candidate.score > 0


async def test_score_equals_the_sum_of_signal_weights(
    retriever: ContextRetriever, settings: Settings, repo: Path
) -> None:
    result = await retriever.retrieve(
        make_issue("parser config fails", body="see `src/config/parser.ts`"),
        await index_of(repo, settings),
        depth=2,
    )

    for candidate in result.candidates:
        assert candidate.score == pytest.approx(
            round(sum(match.weight for match in candidate.signals), 4)
        )


async def test_traversal_distance_is_absent_for_direct_matches(
    retriever: ContextRetriever, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    write(root / "parser.ts")

    result = await retriever.retrieve(
        make_issue("parser fails"), await index_of(root, settings)
    )

    assert candidate_for(result, "parser.ts").traversal_distance is None


async def test_candidate_limit_is_respected(settings: Settings, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    for number in range(30):
        write(root / f"parser-{number:02d}.ts")

    retriever = ContextRetriever(
        settings.model_copy(update={"retrieval_max_candidates": 5})
    )
    result = await retriever.retrieve(
        make_issue("parser fails"), await index_of(root, settings)
    )

    assert len(result.candidates) == 5
    assert result.truncated is True


async def test_graph_cap_is_reported_as_a_warning(
    settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    for number in range(10):
        write(root / f"module{number:02d}.ts", "export const x = 1;\n")

    retriever = ContextRetriever(
        settings.model_copy(update={"retrieval_max_graph_files": 3})
    )
    result = await retriever.retrieve(
        make_issue("module fails"), await index_of(root, settings)
    )

    assert result.graph_file_count == 3
    assert result.truncated is True
    assert any("import graph was capped" in warning for warning in result.warnings)


async def test_empty_issue_yields_no_candidates(
    retriever: ContextRetriever, settings: Settings, repo: Path
) -> None:
    result = await retriever.retrieve(
        make_issue("", body=None), await index_of(repo, settings)
    )

    assert result.candidates == ()
    assert result.keywords == ()
    assert result.seed_paths == ()


async def test_result_records_provenance(
    retriever: ContextRetriever, settings: Settings, repo: Path
) -> None:
    index = await index_of(repo, settings)

    result = await retriever.retrieve(make_issue("parser fails"), index, depth=2)

    assert isinstance(result, RetrievalResult)
    assert result.root == repo.resolve()
    assert result.issue_number == 42
    assert result.considered_file_count == len(index.files)
    assert result.traversal_depth == 2
    assert result.retrieved_at.tzinfo is not None
    assert result.duration_ms >= 0


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


async def test_retrieval_is_deterministic(
    retriever: ContextRetriever, settings: Settings, repo: Path
) -> None:
    index = await index_of(repo, settings)
    analysis = await RepositoryDetector(settings).analyze(repo)
    issue = make_issue("parseConfig fails", body="see `src/config/parser.ts` and zod")

    first = await retriever.retrieve(issue, index, analysis, depth=2)
    second = await retriever.retrieve(issue, index, analysis, depth=2)

    ignore = {"retrieved_at", "duration_ms"}
    assert first.model_dump(exclude=ignore) == second.model_dump(exclude=ignore)


async def test_equal_scores_break_ties_by_path(
    retriever: ContextRetriever, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    for name in ("zebra", "alpha", "middle"):
        write(root / f"{name}" / "parser.ts")

    result = await retriever.retrieve(
        make_issue("parser fails"), await index_of(root, settings)
    )

    scores = {candidate.score for candidate in result.candidates}
    assert len(scores) == 1
    assert paths_of(result) == [
        "alpha/parser.ts",
        "middle/parser.ts",
        "zebra/parser.ts",
    ]


async def test_retrieve_from_repository_matches_the_indexed_form(
    retriever: ContextRetriever, settings: Settings, repo: Path
) -> None:
    issue = make_issue("parser fails")

    direct = await retriever.retrieve(issue, await index_of(repo, settings), depth=1)
    convenience = await retriever.retrieve_from_repository(issue, repo, depth=1)

    ignore = {"retrieved_at", "duration_ms"}
    assert direct.model_dump(exclude=ignore) == convenience.model_dump(exclude=ignore)
