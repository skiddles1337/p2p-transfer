"""
json_store.py

Small shared helpers for reading/writing JSON files safely - used by
contacts.py, history.py, and storage.py's settings file, so this logic
exists in exactly one place rather than three slightly different
copies that could drift apart.

Two real problems this solves:

1. CRASH-SAFETY: writing directly to the real file means a crash or
   power loss mid-write leaves a TRUNCATED, invalid JSON file behind.
   The next time the app starts and tries to load it, a naive
   json.load() would raise an exception - and depending how that's
   handled, could mean the WHOLE APP fails to even start, not just
   "lost some contacts." Fixed by writing to a temporary file first,
   then atomically renaming it over the real file (the same trick
   already used in storage.py's finalize_transfer for moving a
   completed download into place) - a half-finished write can never
   corrupt the real file, since the rename only happens after the
   write has fully succeeded.

2. GRACEFUL RECOVERY: if a file IS somehow corrupted (predates this
   fix, manual editing gone wrong, a genuinely unexpected disk issue),
   loading it should not crash the app - it should fall back to a
   sensible default (empty list/dict), while preserving the broken
   file under a ".corrupted" suffix in case the data is still worth
   investigating, rather than silently overwriting it on the next save.
"""

import os
import json
import shutil


def atomic_write_json(path: str, data) -> None:
    """
    Write `data` as JSON to `path`, atomically - either the write
    fully succeeds and the real file is updated, or it doesn't happen
    at all. The real file is never left in a half-written state.
    """
    parent_dir = os.path.dirname(path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    temp_path = path + ".tmp"
    with open(temp_path, "w") as f:
        json.dump(data, f, indent=2)

    os.replace(temp_path, path)  # atomic rename on both Windows and POSIX


def safe_load_json(path: str, default):
    """
    Load JSON from `path`, returning `default` if the file doesn't
    exist yet (a normal first-run state) OR if it exists but fails to
    parse (corrupted). In the corrupted case, the broken file is
    preserved under a ".corrupted" suffix rather than silently lost,
    in case it's worth investigating or recovering by hand later.
    """
    if not os.path.exists(path):
        return default

    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        corrupted_path = path + ".corrupted"
        try:
            shutil.copy2(path, corrupted_path)
        except OSError:
            pass  # best-effort - don't let the backup itself block recovery
        return default


if __name__ == "__main__":
    import tempfile

    test_dir = tempfile.mkdtemp()
    test_path = os.path.join(test_dir, "subdir", "test.json")

    print("=== Normal round-trip ===")
    atomic_write_json(test_path, {"hello": "world"})
    loaded = safe_load_json(test_path, default={})
    print(f"Loaded: {loaded}")
    assert loaded == {"hello": "world"}
    print("OK")

    print("\n=== Loading a file that doesn't exist yet ===")
    missing = safe_load_json(os.path.join(test_dir, "nope.json"), default=["fallback"])
    print(f"Loaded: {missing}")
    assert missing == ["fallback"]
    print("OK")

    print("\n=== Corrupted file recovery ===")
    with open(test_path, "w") as f:
        f.write("{not valid json at all")  # simulate a crash mid-write

    recovered = safe_load_json(test_path, default={"empty": True})
    print(f"Loaded (should be the default): {recovered}")
    assert recovered == {"empty": True}

    corrupted_backup = test_path + ".corrupted"
    print(f"Corrupted backup preserved: {os.path.exists(corrupted_backup)}")
    assert os.path.exists(corrupted_backup)
    print("OK")

    shutil.rmtree(test_dir)
    print("\nAll checks passed.")
