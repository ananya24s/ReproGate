"""Detects the repository ecosystem, indexes files, discovers relevant code
context, and identifies the existing testing framework."""

from app.repository_analysis.detector import RepositoryDetector
from app.repository_analysis.file_indexer import FileIndexer
from app.repository_analysis.models import (
    FileIndex,
    IndexedFile,
    Language,
    LanguageUsage,
    Lockfile,
    MonorepoTool,
    NodePackage,
    PackageManager,
    PackageManagerSource,
    RepositoryAnalysis,
    TestFramework,
    TestFrameworkSource,
)

__all__ = [
    "FileIndex",
    "FileIndexer",
    "IndexedFile",
    "Language",
    "LanguageUsage",
    "Lockfile",
    "MonorepoTool",
    "NodePackage",
    "PackageManager",
    "PackageManagerSource",
    "RepositoryAnalysis",
    "RepositoryDetector",
    "TestFramework",
    "TestFrameworkSource",
]
