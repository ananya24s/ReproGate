"""Detects the repository ecosystem, indexes files, discovers relevant code
context, and identifies the existing testing framework."""

from app.repository_analysis.context_retriever import ContextRetriever, IssueQuery
from app.repository_analysis.detector import RepositoryDetector
from app.repository_analysis.file_indexer import FileIndexer
from app.repository_analysis.models import (
    ExtractedKeyword,
    FileIndex,
    IndexedFile,
    KeywordSource,
    Language,
    LanguageUsage,
    Lockfile,
    MonorepoTool,
    NodePackage,
    PackageManager,
    PackageManagerSource,
    RepositoryAnalysis,
    RetrievalCandidate,
    RetrievalResult,
    RetrievalSignal,
    SignalMatch,
    TestFramework,
    TestFrameworkSource,
)

__all__ = [
    "ContextRetriever",
    "ExtractedKeyword",
    "FileIndex",
    "FileIndexer",
    "IndexedFile",
    "IssueQuery",
    "KeywordSource",
    "Language",
    "LanguageUsage",
    "Lockfile",
    "MonorepoTool",
    "NodePackage",
    "PackageManager",
    "PackageManagerSource",
    "RepositoryAnalysis",
    "RepositoryDetector",
    "RetrievalCandidate",
    "RetrievalResult",
    "RetrievalSignal",
    "SignalMatch",
    "TestFramework",
    "TestFrameworkSource",
]
