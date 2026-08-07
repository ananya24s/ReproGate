"""Selects the subset of indexed files that is relevant to a given issue.

Retrieval is entirely deterministic: keywords are extracted by tokenisation,
files are matched by name and path, and the import graph is walked by parsing
literal module specifiers. There is no embedding, ranking model, or similarity
measure anywhere in this module — the same issue and repository always produce
the same result, in the same order.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter, deque
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.repository_analysis.file_indexer import FileIndexer
from app.repository_analysis.models import (
    ExtractedKeyword,
    FileIndex,
    KeywordSource,
    RepositoryAnalysis,
    RetrievalCandidate,
    RetrievalResult,
    RetrievalSignal,
    SignalMatch,
)

logger = get_logger(__name__)

# --------------------------------------------------------------------------
# Weights. A candidate's score is the sum of `base weight x source multiplier`
# across every signal it matched, so the score is always reconstructible from
# the signals stored alongside it.
# --------------------------------------------------------------------------

_SIGNAL_WEIGHTS: Final[dict[RetrievalSignal, float]] = {
    RetrievalSignal.EXPLICIT_PATH: 10.0,
    RetrievalSignal.FILENAME: 4.0,
    RetrievalSignal.PATH_SEGMENT: 2.0,
    RetrievalSignal.DIRECTORY: 1.0,
    RetrievalSignal.DEPENDENCY_IMPORT: 1.5,
    RetrievalSignal.IMPORT_NEIGHBOUR: 3.0,
}

#: A term in the title is a stronger hint than the same term buried in a body.
_SOURCE_MULTIPLIERS: Final[dict[KeywordSource, float]] = {
    KeywordSource.TITLE: 1.0,
    KeywordSource.BODY: 0.6,
    KeywordSource.LABEL: 0.4,
}

_SCORE_PRECISION: Final = 4
_EXPLANATION_SIGNAL_LIMIT: Final = 4
_MIN_KEYWORD_LENGTH: Final = 3

#: Extensions whose imports are parsed. Resolution rules are JS/TS specific.
_MODULE_EXTENSIONS: Final = (
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
)

#: TypeScript ESM imports name the emitted `.js` file; map back to the source.
_EMITTED_TO_SOURCE: Final[dict[str, tuple[str, ...]]] = {
    ".js": (".ts", ".tsx"),
    ".mjs": (".mts",),
    ".cjs": (".cts",),
}

#: English function words plus issue-template boilerplate. None of these ever
#: distinguish one file from another.
_STOPWORDS: Final = frozenset(
    # A readable block beats a 200-line list literal here.
    """
    a an and any are as at be been before being both but by can could did do does
    doing done down during each few for from further get gets getting got had has
    have having here how i if in into is it its just like make made me more most
    my no nor not now of off on once only or other others our out over own please
    same see seen should so some such than that the their them then there these
    they this those through to too under until up us use used uses using very was
    way we well were what when where which while who why will with would you your
    actual actually additional behavior behaviour bug case chore column context
    correct crash current description detail details environment example expected
    feature file files fix follow following happens hello hey hi issue line lines
    log logs message minimal node npm observed occurs os output package possible
    problem project question reason repo repro reproduce reproduction result
    results run running screenshot screenshots snippet solution stack steps sure
    system thanks think trace try unexpected update version versions wrong yarn
    """.split()  # noqa: SIM905
)

# Tokens that look like a path: at least one separator and a file extension.
_PATH_RE: Final = re.compile(
    r"(?<![\w/.-])((?:\.{0,2}/)?[\w.-]+(?:/[\w.-]+)+\.\w{1,8})"
)
# A bare filename with an extension, e.g. `parser.ts`.
_FILENAME_RE: Final = re.compile(r"(?<![\w/.-])([\w-]+\.[A-Za-z][\w]{0,7})(?![\w/.-])")
# Fenced code blocks and inline code, stripped before keyword extraction but
# mined for paths first.
_CODE_FENCE_RE: Final = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE: Final = re.compile(r"`[^`\n]+`")

_WORD_RE: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_CAMEL_SPLIT_RE: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

#: Literal module specifiers. Regexes rather than a parser: deterministic, and
#: it never executes or interprets repository code.
_SPECIFIER_RES: Final = (
    re.compile(r"""\bimport\s+[^;'"]*?\bfrom\s*['"]([^'"\n]+)['"]"""),
    re.compile(r"""\bimport\s*['"]([^'"\n]+)['"]"""),
    re.compile(r"""\bexport\s+[^;'"]*?\bfrom\s*['"]([^'"\n]+)['"]"""),
    re.compile(r"""\brequire\s*\(\s*['"]([^'"\n]+)['"]\s*\)"""),
    re.compile(r"""\bimport\s*\(\s*['"]([^'"\n]+)['"]\s*\)"""),
)


class IssueQuery:
    """The text of an issue, decoupled from the GitHub schema.

    Retrieval only needs a title, a body, and labels, so the module does not
    depend on the shape of ``GitHubIssue`` beyond those three fields.
    """

    __slots__ = ("body", "labels", "number", "title")

    def __init__(
        self,
        *,
        number: int = 0,
        title: str = "",
        body: str | None = None,
        labels: Iterable[str] = (),
    ) -> None:
        self.number = number
        self.title = title
        self.body = body or ""
        self.labels = tuple(labels)

    @classmethod
    def from_issue(cls, issue: object) -> IssueQuery:
        """Build a query from anything exposing an issue's public fields."""
        return cls(
            number=getattr(issue, "number", 0),
            title=getattr(issue, "title", "") or "",
            body=getattr(issue, "body", None),
            labels=getattr(issue, "labels", ()) or (),
        )


class ContextRetriever:
    """Finds files that are plausibly involved in a reported issue."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        indexer: FileIndexer | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._indexer = indexer or FileIndexer(self._settings)
        self._max_candidates = max(1, self._settings.retrieval_max_candidates)
        self._max_keywords = max(1, self._settings.retrieval_max_keywords)
        self._max_seeds = max(1, self._settings.retrieval_max_seed_files)
        self._max_graph_files = max(0, self._settings.retrieval_max_graph_files)
        self._max_source_bytes = max(0, self._settings.retrieval_max_source_bytes)

    async def retrieve(
        self,
        issue: object,
        index: FileIndex,
        analysis: RepositoryAnalysis | None = None,
        *,
        depth: int | None = None,
    ) -> RetrievalResult:
        """Retrieve candidate files for ``issue`` from an existing index.

        Args:
            issue: A ``GitHubIssue``, or anything with ``number``, ``title``,
                ``body``, and ``labels``.
            index: The repository index produced by :class:`FileIndexer`.
            analysis: Repository analysis, used for the dependency signal.
            depth: Import-graph traversal depth. Defaults to
                ``RETRIEVAL_TRAVERSAL_DEPTH``.

        Returns:
            Ranked candidates, each carrying every reason it was selected.
        """
        query = IssueQuery.from_issue(issue)
        return await asyncio.to_thread(
            self._retrieve_sync, query, index, analysis, depth
        )

    async def retrieve_from_repository(
        self,
        issue: object,
        root: Path,
        analysis: RepositoryAnalysis | None = None,
        *,
        depth: int | None = None,
    ) -> RetrievalResult:
        """Index ``root``, then retrieve. Convenience for callers without an index."""
        index = await self._indexer.index(root)
        return await self.retrieve(issue, index, analysis, depth=depth)

    # -- Implementation ----------------------------------------------------

    def _retrieve_sync(
        self,
        query: IssueQuery,
        index: FileIndex,
        analysis: RepositoryAnalysis | None,
        depth: int | None,
    ) -> RetrievalResult:
        started = time.monotonic()
        warnings: list[str] = []
        traversal_depth = max(
            0,
            self._settings.retrieval_traversal_depth if depth is None else depth,
        )

        keywords = _extract_keywords(query, self._max_keywords)
        matches: dict[str, list[SignalMatch]] = {}

        referenced = self._match_referenced_paths(query, index)
        for path in referenced:
            _add(
                matches,
                path,
                SignalMatch(
                    signal=RetrievalSignal.EXPLICIT_PATH,
                    weight=_SIGNAL_WEIGHTS[RetrievalSignal.EXPLICIT_PATH],
                    detail="named directly in the issue",
                ),
            )

        self._match_names_and_paths(index, keywords, matches)

        graph, graph_files, graph_truncated = self._build_import_graph(index)
        if graph_truncated:
            warnings.append(
                "The import graph was capped at "
                f"{self._max_graph_files} files; traversal may be incomplete."
            )

        self._match_dependency_imports(query, analysis, graph, matches)

        seeds = _select_seeds(matches, self._max_seeds)
        distances = _traverse(graph, seeds, traversal_depth)
        for path, distance in sorted(distances.items()):
            _add(
                matches,
                path,
                SignalMatch(
                    signal=RetrievalSignal.IMPORT_NEIGHBOUR,
                    weight=round(
                        _SIGNAL_WEIGHTS[RetrievalSignal.IMPORT_NEIGHBOUR] / distance,
                        _SCORE_PRECISION,
                    ),
                    detail=f"{distance} import hop(s) from a seed file",
                ),
            )

        candidates = _rank(matches, distances)
        truncated = (
            index.truncated
            or graph_truncated
            or (len(candidates) > self._max_candidates)
        )

        result = RetrievalResult(
            root=index.root,
            issue_number=query.number,
            candidates=tuple(candidates[: self._max_candidates]),
            keywords=keywords,
            referenced_paths=tuple(referenced),
            seed_paths=tuple(seeds),
            traversal_depth=traversal_depth,
            considered_file_count=len(index.files),
            graph_file_count=graph_files,
            truncated=truncated,
            warnings=tuple(warnings),
            retrieved_at=datetime.now(tz=UTC),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

        logger.info(
            "Retrieved relevant files",
            extra={
                "root": str(index.root),
                "issue_number": query.number,
                "keyword_count": len(keywords),
                "referenced_path_count": len(referenced),
                "seed_count": len(seeds),
                "graph_file_count": graph_files,
                "candidate_count": len(result.candidates),
                "top_candidate": (
                    result.candidates[0].path if result.candidates else None
                ),
                "truncated": truncated,
                "duration_ms": result.duration_ms,
            },
        )
        return result

    def _match_referenced_paths(self, query: IssueQuery, index: FileIndex) -> list[str]:
        """Resolve paths and filenames the issue names against the index."""
        text = f"{query.title}\n{query.body}"
        resolved: set[str] = set()

        for raw in _PATH_RE.findall(text):
            match = _resolve_reference(raw, index)
            if match is not None:
                resolved.add(match)

        for raw in _FILENAME_RE.findall(text):
            # A bare filename only counts when it is unambiguous; `index.ts`
            # would otherwise pull in every barrel file in the repository.
            match = _resolve_reference(raw, index, unique_only=True)
            if match is not None:
                resolved.add(match)

        return sorted(resolved)

    def _match_names_and_paths(
        self,
        index: FileIndex,
        keywords: tuple[ExtractedKeyword, ...],
        matches: dict[str, list[SignalMatch]],
    ) -> None:
        """Score files whose name, parent directory, or ancestors match a keyword."""
        if not keywords:
            return

        terms = {keyword.term: keyword for keyword in keywords}

        for file in index.files:
            stem = file.name[: -len(file.suffix)] if file.suffix else file.name
            name_tokens = _tokenize(stem)

            components = file.path.split("/")[:-1]
            parent = components[-1] if components else None
            ancestors = components[:-1]

            parent_tokens = _tokenize(parent) if parent else set()
            ancestor_tokens = {
                token for component in ancestors for token in _tokenize(component)
            }

            for term in sorted(terms):
                keyword = terms[term]
                if term in name_tokens:
                    _add(
                        matches,
                        file.path,
                        _keyword_match(
                            RetrievalSignal.FILENAME,
                            keyword,
                            f"filename contains {term!r}",
                        ),
                    )
                if term in parent_tokens:
                    _add(
                        matches,
                        file.path,
                        _keyword_match(
                            RetrievalSignal.DIRECTORY,
                            keyword,
                            f"parent directory contains {term!r}",
                        ),
                    )
                if term in ancestor_tokens:
                    _add(
                        matches,
                        file.path,
                        _keyword_match(
                            RetrievalSignal.PATH_SEGMENT,
                            keyword,
                            f"path contains {term!r}",
                        ),
                    )

    def _match_dependency_imports(
        self,
        query: IssueQuery,
        analysis: RepositoryAnalysis | None,
        graph: _ImportGraph,
        matches: dict[str, list[SignalMatch]],
    ) -> None:
        """Score files importing a declared package the issue mentions."""
        if analysis is None or analysis.root_package is None:
            return

        text = f"{query.title}\n{query.body}".lower()
        mentioned = sorted(
            name for name in analysis.root_package.dependencies if name.lower() in text
        )
        if not mentioned:
            return

        for package in mentioned:
            for path in sorted(graph.importers_of_package(package)):
                _add(
                    matches,
                    path,
                    SignalMatch(
                        signal=RetrievalSignal.DEPENDENCY_IMPORT,
                        weight=_SIGNAL_WEIGHTS[RetrievalSignal.DEPENDENCY_IMPORT],
                        detail=f"imports {package!r}, which the issue mentions",
                        keyword=package,
                    ),
                )

    def _build_import_graph(self, index: FileIndex) -> tuple[_ImportGraph, int, bool]:
        """Parse literal import specifiers from every indexed source file."""
        graph = _ImportGraph()
        parsed = 0
        truncated = False

        sources = [file for file in index.files if file.suffix in _MODULE_EXTENSIONS]
        for file in sources:
            if parsed >= self._max_graph_files:
                truncated = True
                break
            if file.size_bytes > self._max_source_bytes:
                continue

            text = _read_source(index.root, file.path, self._max_source_bytes)
            if text is None:
                continue
            parsed += 1

            for specifier in _specifiers(text):
                if specifier.startswith("."):
                    target = _resolve_relative(file.path, specifier, index.paths)
                    if target is not None:
                        graph.add_edge(file.path, target)
                elif not specifier.startswith("/"):
                    graph.add_package(file.path, _package_name(specifier))

        return graph, parsed, truncated


class _ImportGraph:
    """Local import edges plus the bare packages each file imports."""

    __slots__ = ("_imported_by", "_imports", "_packages")

    def __init__(self) -> None:
        self._imports: dict[str, set[str]] = {}
        self._imported_by: dict[str, set[str]] = {}
        self._packages: dict[str, set[str]] = {}

    def add_edge(self, importer: str, target: str) -> None:
        if importer == target:
            return
        self._imports.setdefault(importer, set()).add(target)
        self._imported_by.setdefault(target, set()).add(importer)

    def add_package(self, importer: str, package: str) -> None:
        self._packages.setdefault(package, set()).add(importer)

    def neighbours(self, path: str) -> set[str]:
        """Files this file imports, and files that import it."""
        return self._imports.get(path, set()) | self._imported_by.get(path, set())

    def importers_of_package(self, package: str) -> set[str]:
        return self._packages.get(package, set())


# --------------------------------------------------------------------------
# Keyword extraction
# --------------------------------------------------------------------------


def _extract_keywords(query: IssueQuery, limit: int) -> tuple[ExtractedKeyword, ...]:
    """Tokenise the issue into ranked, de-duplicated search terms."""
    counts: Counter[str] = Counter()
    sources: dict[str, set[KeywordSource]] = {}

    def absorb(text: str, source: KeywordSource) -> None:
        for token in _tokenize(text):
            counts[token] += 1
            sources.setdefault(token, set()).add(source)

    absorb(query.title, KeywordSource.TITLE)
    # Code blocks are mined for paths separately; as prose they are noise.
    body = _INLINE_CODE_RE.sub(" ", _CODE_FENCE_RE.sub(" ", query.body))
    absorb(body, KeywordSource.BODY)
    for label in query.labels:
        absorb(label, KeywordSource.LABEL)

    def rank(item: tuple[str, int]) -> tuple[float, int, str]:
        term, count = item
        # Where a term came from first, then how often it appears, then the
        # term itself. Without the source rank, a long prose body spends the
        # whole keyword budget on alphabetically-early one-off words before
        # the title's terms are reached.
        strongest = max(_SOURCE_MULTIPLIERS[source] for source in sources[term])
        return (-strongest, -count, term)

    ordered = sorted(counts.items(), key=rank)
    return tuple(
        ExtractedKeyword(
            term=term,
            sources=tuple(sorted(sources[term], key=lambda item: item.value)),
            occurrences=count,
        )
        for term, count in ordered[:limit]
    )


def _tokenize(text: str | None) -> set[str]:
    """Split text into lowercase terms, expanding camelCase and snake_case."""
    if not text:
        return set()

    tokens: set[str] = set()
    for word in _WORD_RE.findall(text):
        parts = [word, *word.split("_"), *_CAMEL_SPLIT_RE.split(word)]
        for part in parts:
            term = part.lower()
            if len(term) >= _MIN_KEYWORD_LENGTH and term not in _STOPWORDS:
                tokens.add(term)
    return tokens


# --------------------------------------------------------------------------
# Path resolution
# --------------------------------------------------------------------------


def _resolve_reference(
    raw: str,
    index: FileIndex,
    *,
    unique_only: bool = False,
) -> str | None:
    """Resolve a path mentioned in issue text to a file in the index."""
    candidate = raw.strip().lstrip("/")
    while candidate.startswith("./"):
        candidate = candidate[2:]
    if not candidate or ".." in candidate.split("/"):
        return None

    if index.contains(candidate):
        return candidate

    # Issues often quote a path from the reporter's machine. Drop leading
    # components, longest tail first, until one names a file we actually have:
    # `/home/dev/project/src/parser.ts` resolves to `src/parser.ts`.
    parts = candidate.split("/")
    for start in range(1, len(parts)):
        tail = "/".join(parts[start:])
        if index.contains(tail):
            return tail

    # Or the issue quotes only the tail of a path. Anchor on a separator so
    # `parser.ts` cannot match `superparser.ts`.
    suffix = f"/{candidate}"
    hits = sorted(path for path in index.paths if path.endswith(suffix))
    if not hits or (unique_only and len(hits) > 1):
        return None
    return hits[0]


def _specifiers(text: str) -> list[str]:
    """Every literal module specifier in a source file, in first-seen order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for pattern in _SPECIFIER_RES:
        for specifier in pattern.findall(text):
            if specifier not in seen:
                seen.add(specifier)
                ordered.append(specifier)
    return ordered


def _package_name(specifier: str) -> str:
    """The package part of a bare specifier: `@scope/pkg/sub` -> `@scope/pkg`."""
    parts = specifier.split("/")
    if specifier.startswith("@") and len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0]


def _resolve_relative(
    importer: str, specifier: str, paths: frozenset[str]
) -> str | None:
    """Resolve a relative specifier to an indexed file, or ``None``."""
    base = _normalize(f"{PurePosixPath(importer).parent}/{specifier}")
    if base is None:
        return None

    for candidate in _resolution_candidates(base):
        if candidate in paths:
            return candidate
    return None


def _resolution_candidates(base: str) -> Iterator[str]:
    """Module resolution attempts, in Node's order."""
    yield base

    suffix = PurePosixPath(base).suffix
    # `./util.js` in a TypeScript ESM project means `./util.ts`.
    for source_extension in _EMITTED_TO_SOURCE.get(suffix, ()):
        yield f"{base[: -len(suffix)]}{source_extension}"

    for extension in _MODULE_EXTENSIONS:
        yield f"{base}{extension}"
    for extension in _MODULE_EXTENSIONS:
        yield f"{base}/index{extension}"


def _normalize(path: str) -> str | None:
    """Collapse `.` and `..`, refusing anything that escapes the root."""
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts) or None


def _read_source(root: Path, relative: str, limit: int) -> str | None:
    """Read an indexed source file, staying inside the repository."""
    try:
        resolved = (root / relative).resolve()
        if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
            return None
        if resolved.stat().st_size > limit:
            return None
        return resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# --------------------------------------------------------------------------
# Traversal, scoring, ranking
# --------------------------------------------------------------------------


def _select_seeds(matches: dict[str, list[SignalMatch]], limit: int) -> list[str]:
    """The highest-scoring direct matches, which seed graph traversal."""
    scored = [
        (_score(signals), path)
        for path, signals in matches.items()
        if any(
            signal.signal is not RetrievalSignal.IMPORT_NEIGHBOUR for signal in signals
        )
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [path for _, path in scored[:limit]]


def _traverse(graph: _ImportGraph, seeds: list[str], depth: int) -> dict[str, int]:
    """Breadth-first walk of the import graph, returning hops from a seed."""
    if depth <= 0 or not seeds:
        return {}

    distances: dict[str, int] = {}
    visited = set(seeds)
    queue: deque[tuple[str, int]] = deque((seed, 0) for seed in sorted(seeds))

    while queue:
        path, distance = queue.popleft()
        if distance >= depth:
            continue
        for neighbour in sorted(graph.neighbours(path)):
            if neighbour in visited:
                continue
            visited.add(neighbour)
            distances[neighbour] = distance + 1
            queue.append((neighbour, distance + 1))

    return distances


def _keyword_match(
    signal: RetrievalSignal, keyword: ExtractedKeyword, detail: str
) -> SignalMatch:
    """Weight a keyword match by where in the issue the keyword appeared."""
    source = max(
        keyword.sources, key=lambda item: (_SOURCE_MULTIPLIERS[item], item.value)
    )
    weight = round(
        _SIGNAL_WEIGHTS[signal] * _SOURCE_MULTIPLIERS[source], _SCORE_PRECISION
    )
    return SignalMatch(
        signal=signal,
        weight=weight,
        detail=f"{detail} (from issue {source.value})",
        keyword=keyword.term,
        keyword_source=source,
    )


def _add(matches: dict[str, list[SignalMatch]], path: str, match: SignalMatch) -> None:
    matches.setdefault(path, []).append(match)


def _score(signals: Iterable[SignalMatch]) -> float:
    return round(sum(signal.weight for signal in signals), _SCORE_PRECISION)


def _rank(
    matches: dict[str, list[SignalMatch]], distances: dict[str, int]
) -> list[RetrievalCandidate]:
    """Build candidates and order them by score, then path."""
    candidates: list[RetrievalCandidate] = []

    for path in sorted(matches):
        signals = sorted(
            matches[path],
            key=lambda item: (-item.weight, item.signal.value, item.detail),
        )
        candidates.append(
            RetrievalCandidate(
                path=path,
                score=_score(signals),
                signals=tuple(signals),
                traversal_distance=distances.get(path),
                explanation=_explain(signals),
            )
        )

    candidates.sort(key=lambda item: (-item.score, item.path))
    return candidates


def _explain(signals: list[SignalMatch]) -> str:
    """A readable summary of why a file was selected."""
    shown = signals[:_EXPLANATION_SIGNAL_LIMIT]
    summary = "; ".join(signal.detail for signal in shown)
    remaining = len(signals) - len(shown)
    return f"{summary}; and {remaining} more" if remaining > 0 else summary
