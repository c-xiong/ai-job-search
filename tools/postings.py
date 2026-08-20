#!/usr/bin/env python3
"""Sidecar storage for posting bodies.

`seen_jobs.json` is rewritten in full on every board interaction (a status
click, a note edit) and `/api/jobs` ships every row to the browser on each
reload. A posting body is several kilobytes; inlining one per row would put
megabytes on both of those hot paths. So the body lives in its own file and the
entry keeps only pointers to it.

    job_scraper/postings/<sha1-of-canonical-url>.txt

The canonical URL is the `seen_jobs.json` key, which never moves (ats_fetch
§11.5), so the sidecar name is stable for the life of the row.

Writes are split from descriptions on purpose. `merge()` runs in full during a
`--dry-run` - only `save_seen()` is skipped - so a store call inside it would
litter sidecars on a run that is supposed to touch nothing. `describe()` is
pure and returns the fields; `commit()` is the only function that writes, and
the caller runs it in the same branch that saves the state.

Stdlib only.
"""

import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jobs_md  # noqa: E402

ROOT = jobs_md.ROOT
REL_DIR = "postings"


def store_dir():
    """Where sidecars live: beside whichever state file is active.

    Resolved per call, from `jobs_md.SEEN`, rather than frozen into a module
    constant. Every test that works on a throwaway board redirects `SEEN` to a
    temp directory; a constant here would not follow, so a test exercising the
    real write path would drop posting bodies into the developer's own
    `job_scraper/`. Deriving the path keeps "the bodies sit next to the state
    that references them" true by construction instead of by convention.
    """
    return jobs_md.SEEN.parent / REL_DIR


EXCERPT_CHARS = 300

# The fields describe() puts on an entry. Named here so ats_fetch can add them
# to its append-allowlist without restating them and drifting.
ENTRY_FIELDS = ("posting_path", "posting_chars", "posting_excerpt",
                "posting_source", "posting_extractor", "posting_final_url",
                "posting_fingerprint")

# Kept identical to ats_fetch's dedup window and token rule so a fingerprint
# built here means the same thing the collapse test has always meant.
FINGERPRINT_CHARS = 1500
FINGERPRINT_THRESHOLD = 0.6

# 128 permutations, 16 bits kept from each minimum: 512 hex chars per row.
#
# Why an estimate rather than the exact token set: the exact set is ~1 KB per
# row, and seen_jobs.json is the file we just moved bodies *out* of. The error
# is affordable here because of what this feeds - `find_duplicate` only
# consults a fingerprint after company, exact title, location overlap and a
# 14-day date window have all already agreed, and it can then only *veto* the
# collapse. That decision is bimodal in practice (the same posting via two
# portals lands near 0.9, two different requisitions near 0.3), so a threshold
# at 0.6 is nowhere near the estimator's ~9% standard error. A wrong call costs
# one duplicate row on the board, never a lost one.
MINHASH_N = 128
_MINHASH_BITS = 16
_MINHASH_MASK = (1 << _MINHASH_BITS) - 1
_MINHASH_WIDTH = _MINHASH_BITS // 4  # hex chars per permutation
_MERSENNE_61 = (1 << 61) - 1


def _coefficients(count):
    """Deterministic (a, b) pairs for the permutation family.

    Derived from a fixed digest rather than `random`: a fingerprint written
    today has to compare against one written next month in another process, so
    nothing here may depend on process state or PYTHONHASHSEED.
    """
    out = []
    for i in range(count):
        digest = hashlib.blake2b(b"jobflow-minhash-%d" % i, digest_size=16).digest()
        a = int.from_bytes(digest[:8], "big") % (_MERSENNE_61 - 1) + 1
        b = int.from_bytes(digest[8:], "big") % _MERSENNE_61
        out.append((a, b))
    return tuple(out)


_COEFFS = _coefficients(MINHASH_N)


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _tokens(text):
    """Same rule as ats_fetch._tokens: normalized words longer than two chars."""
    return {t for t in _norm(text).split() if len(t) > 2}


def _stable_hash(token):
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")


def fingerprint(text):
    """A 512-hex-char MinHash signature of the first ~1500 characters.

    Empty string when there is nothing to fingerprint, which reads downstream
    as "unknown" - never as "different".
    """
    tokens = _tokens((text or "")[:FINGERPRINT_CHARS])
    if not tokens:
        return ""
    hashes = [_stable_hash(token) for token in tokens]
    parts = []
    for a, b in _COEFFS:
        smallest = min(((a * h + b) % _MERSENNE_61) for h in hashes)
        parts.append("%0*x" % (_MINHASH_WIDTH, smallest & _MINHASH_MASK))
    return "".join(parts)


def fingerprint_similarity(left, right):
    """Estimated Jaccard, or None when either side is missing or malformed."""
    if not left or not right or len(left) != len(right):
        return None
    if len(left) % _MINHASH_WIDTH:
        return None
    count = len(left) // _MINHASH_WIDTH
    if not count:
        return None
    matches = sum(
        1 for i in range(count)
        if left[i * _MINHASH_WIDTH:(i + 1) * _MINHASH_WIDTH]
        == right[i * _MINHASH_WIDTH:(i + 1) * _MINHASH_WIDTH]
    )
    return matches / float(count)


def fingerprints_agree(left, right, threshold=FINGERPRINT_THRESHOLD):
    """True / False / None, matching ats_fetch._fingerprint_agrees' contract.

    None means "no opinion" - the caller must not read that as disagreement.
    """
    similarity = fingerprint_similarity(left, right)
    if similarity is None:
        return None
    return similarity >= threshold


def excerpt(text, limit=EXCERPT_CHARS):
    """The one piece of body text the list payload is allowed to carry."""
    collapsed = re.sub(r"\s+", " ", (text or "")).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + "…"


def normalize_body(text):
    """Line-ending and trailing-whitespace cleanup, paragraph structure kept.

    The Job pane renders this text, so blank lines between paragraphs are
    content, not noise. Only the things that differ per fetch are flattened.
    """
    if not text:
        return ""
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    body = "\n".join(line.rstrip() for line in body.split("\n"))
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def relpath_for(url):
    """`postings/<sha1>.txt` - the value stored as `posting_path`."""
    digest = hashlib.sha1((url or "").encode("utf-8")).hexdigest()
    return "%s/%s.txt" % (REL_DIR, digest)


def describe(url, text, source="collector", extractor="inline", final_url=""):
    """The `posting_*` fields for an entry. Pure - writes nothing.

    Returns {} for a body that is empty after normalization, so a caller can
    treat "nothing to store" and "stored" the same way.
    """
    body = normalize_body(text)
    if not body:
        return {}
    fields = {
        "posting_path": relpath_for(url),
        "posting_chars": len(body),
        "posting_excerpt": excerpt(body),
        "posting_source": source,
        "posting_extractor": extractor,
        "posting_fingerprint": fingerprint(body),
    }
    if final_url:
        fields["posting_final_url"] = final_url
    return fields


def commit(url, text, root=None):
    """Write one body to its sidecar, atomically. The only function that writes.

    Returns the path written, or None when there was nothing to write.
    """
    body = normalize_body(text)
    if not body:
        return None
    base = Path(root) if root else store_dir()
    base.mkdir(parents=True, exist_ok=True)
    path = base / Path(relpath_for(url)).name
    fd, tmp = tempfile.mkstemp(dir=str(base), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def commit_all(pending, root=None):
    """Write a batch of (url, text) pairs. Returns the number written."""
    written = 0
    for url, text in pending or ():
        if commit(url, text, root=root) is not None:
            written += 1
    return written


class UnsafePath(ValueError):
    """A `posting_path` that does not resolve inside the postings directory."""


def resolve(rel, root=None):
    """Validate a stored `posting_path` and return the file it names.

    `posting_path` is data read back from a JSON file on disk. Treat it as
    untrusted: an absolute path, a `..` segment, or a symlink pointing outside
    would turn a board page render into an arbitrary-file read.
    """
    base = (Path(root) if root else store_dir()).resolve()
    if not rel or not isinstance(rel, str):
        raise UnsafePath("empty posting_path")
    candidate = Path(rel)
    if candidate.is_absolute():
        raise UnsafePath("absolute posting_path: %r" % rel)
    if ".." in candidate.parts:
        raise UnsafePath("posting_path escapes the store: %r" % rel)
    # Stored paths are relative to job_scraper/, and the only directory this
    # module owns is postings/. Accept the prefixed form and the bare filename.
    parts = candidate.parts
    if parts and parts[0] == REL_DIR:
        parts = parts[1:]
    if len(parts) != 1:
        raise UnsafePath("posting_path is not a direct child of %s/: %r" % (REL_DIR, rel))
    target = base / parts[0]
    if target.is_symlink():
        raise UnsafePath("posting_path is a symlink: %r" % rel)
    resolved = target.resolve()
    if resolved != target and base not in resolved.parents:
        raise UnsafePath("posting_path resolves outside the store: %r" % rel)
    if base not in resolved.parents:
        raise UnsafePath("posting_path resolves outside the store: %r" % rel)
    return resolved


def load(entry, root=None):
    """The stored body for one entry, or "" when it has none.

    A missing file is not an error: sidecars are a cache of fetched text and a
    row may legitimately predate the store, or have had its file pruned.
    """
    rel = (entry or {}).get("posting_path")
    if not rel:
        return ""
    try:
        path = resolve(rel, root=root)
    except UnsafePath:
        raise
    try:
        return path.read_text(encoding="utf-8").strip("\n")
    except OSError:
        return ""


def has_body(entry):
    return bool((entry or {}).get("posting_path"))
