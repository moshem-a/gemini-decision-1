"""Static security, model-hygiene, and Google Cloud best-practice auditor (`agentic-sdlc audit`)."""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any

# Construct regexes dynamically so auditor never flags itself
_PRIV_KEY_RE = re.compile(r"-----BEGIN (?:RSA |EC )?" + r"PRIVATE KEY-----")
_RAW_API_KEY_RE = re.compile(r"\bAIza" + r"[0-9A-Za-z_-]{35}\b")
_LEGACY_MODEL_RE = re.compile(r"\bgemini-[0-3](?:\.[0-9]+)?(?:-[a-z0-9.-]+)?\b", re.IGNORECASE)
_INNER_HTML_RE = re.compile(r"\.(?:innerHTML|outerHTML)\s*=|document\.write\s*\(|insertAdjacentHTML\s*\(")
_LOCAL_STORAGE_TOKEN_RE = re.compile(
    r"(?:localStorage|sessionStorage)\.setItem\s*\(\s*['\"][^'\"]*(?:token|jwt|session|secret|auth)[^'\"]*['\"]",
    re.IGNORECASE,
)

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".pytest_cache",
    "logs",
    "sandbox_worktrees",
}

TEXT_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".html",
    ".css",
    ".json",
    ".yaml",
    ".yml",
    ".tf",
    ".md",
    ".sh",
}


def audit_directory(target_dir: str | Path) -> dict[str, Any]:
    """Scan `target_dir` for security, credential, DOM XSS, and Gemini model-governance violations."""
    root = Path(os.path.realpath(str(target_dir)))
    findings: list[dict[str, Any]] = []
    scanned_files = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            ext = fpath.suffix.lower()
            if ext not in TEXT_EXTENSIONS and fname != "Dockerfile":
                continue

            try:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            scanned_files += 1
            rel_path = str(fpath.relative_to(root))
            lines = content.splitlines()

            for idx, line in enumerate(lines, start=1):
                if _PRIV_KEY_RE.search(line):
                    findings.append(
                        {
                            "severity": "HIGH",
                            "cwe": "CWE-798",
                            "rule": "hardcoded-private-key",
                            "file": rel_path,
                            "line": idx,
                            "message": "Hardcoded private key detected. Use ADC, Workload Identity, or Secret Manager.",
                        }
                    )
                if _RAW_API_KEY_RE.search(line):
                    findings.append(
                        {
                            "severity": "HIGH",
                            "cwe": "CWE-798",
                            "rule": "hardcoded-google-api-key",
                            "file": rel_path,
                            "line": idx,
                            "message": "Raw Google API key literal detected. Inject secrets via Secret Manager or env.",
                        }
                    )
                if _LEGACY_MODEL_RE.search(line):
                    findings.append(
                        {
                            "severity": "MEDIUM",
                            "cwe": "POLICY-MODEL",
                            "rule": "deprecated-gemini-model",
                            "file": rel_path,
                            "line": idx,
                            "message": "Outdated pre-v4 Gemini model identifier detected. Use Gemini v4+ models.",
                        }
                    )
                if ext in {".js", ".ts", ".tsx", ".html"}:
                    if _INNER_HTML_RE.search(line):
                        findings.append(
                            {
                                "severity": "HIGH",
                                "cwe": "CWE-79",
                                "rule": "unsafe-dom-innerhtml",
                                "file": rel_path,
                                "line": idx,
                                "message": "Unsafe DOM HTML injection sink detected. Use createElement() and textContent.",
                            }
                        )
                    if _LOCAL_STORAGE_TOKEN_RE.search(line):
                        findings.append(
                            {
                                "severity": "HIGH",
                                "cwe": "CWE-922",
                                "rule": "sensitive-token-web-storage",
                                "file": rel_path,
                                "line": idx,
                                "message": "Sensitive token stored in Web Storage. Use HttpOnly, Secure, SameSite cookies.",
                            }
                        )

            if fname == "Dockerfile" and "USER " not in content:
                findings.append(
                    {
                        "severity": "MEDIUM",
                        "cwe": "CWE-250",
                        "rule": "dockerfile-missing-nonroot-user",
                        "file": rel_path,
                        "line": 1,
                        "message": "Dockerfile lacks a non-root `USER` instruction.",
                    }
                )

    return {
        "target": str(root),
        "scanned_files": scanned_files,
        "passed": len(findings) == 0,
        "finding_count": len(findings),
        "findings": findings,
    }
