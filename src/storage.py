"""
storage.py

Handles how received files are stored on disk, separate from the wire
protocol logic in protocol.py. Specifically:

  - Incoming files are written to a STAGING area under a name derived
    from their transfer_id, not their final human-readable filename.
    This keeps in-progress/incomplete transfers clearly separate from
    finished ones, and sets up future resume support (matching by
    transfer_id, not by filename, which can collide or be renamed).

  - A small JSON "manifest" file sits alongside each staged file,
    recording enough info to describe its state: original filename,
    total size, chunk size, and which chunk indices are verified good.

  - Once a transfer is fully complete and verified, the file is moved
    out of staging into its final location, with a collision-safe
    name (appending " (1)", " (2)", etc. if the name is already taken)
    - similar to how browsers handle duplicate downloads.

STORAGE LOCATIONS: these used to be plain relative paths ("received/",
"received/.partial/"), which meant where files actually landed
depended entirely on whatever folder the script happened to be
launched from - fine for development, but fragile for a real
installed app (a double-clicked .exe might run from Program Files,
which a normal user often can't even write to, or some unpredictable
temp extraction folder). We now use `platformdirs` to find a proper,
OS-appropriate Downloads location instead:

  - DEFAULT_SAVE_DIR: the built-in default - under the user's
    Downloads folder, in a "P2P Transfer" subfolder, since that's
    where people already expect to find things a program downloaded
    for them, same idea as a web browser's downloads.
  - get_save_dir() / set_save_dir() / reset_save_dir(): the actual
    EFFECTIVE save location can be overridden (e.g. a Settings screen
    letting someone pick a different folder) - the override persists
    across restarts, in its own small config file. get_staging_dir()
    is always computed FROM the current effective save dir (as a
    "Partial Downloads" subfolder within it), so an override moves
    staging along with it automatically - staying co-located with
    finished downloads is a deliberate design choice (see below), not
    just an accident of the default path.
  - In-progress transfers and their manifests live in "Partial
    Downloads" rather than a hidden, internal app-data location -
    in-progress downloads are genuinely interesting to see (their
    existence is what a future "resume" feature will build on), so
    this is browsable, not hidden away.
"""

import os
import platformdirs
import json_store

APP_NAME = "p2p-transfer"

DEFAULT_SAVE_DIR = os.path.join(platformdirs.user_downloads_dir(), "P2P Transfer")

_SAVE_DIR_CONFIG_PATH = os.path.join(
    platformdirs.user_config_dir(APP_NAME, appauthor=False), "storage_settings.json"
)


def _load_save_dir_override():
    """Return the overridden save dir, or None if using the default."""
    data = json_store.safe_load_json(_SAVE_DIR_CONFIG_PATH, default={})
    return data.get("save_dir_override")


def get_save_dir() -> str:
    """
    The CURRENTLY EFFECTIVE save directory - the override if one has
    been set, otherwise DEFAULT_SAVE_DIR. This is what all storage
    functions should call at the point of use, NOT a cached constant -
    an override set while the app is running should take effect
    immediately, without needing a restart.
    """
    override = _load_save_dir_override()
    return override if override else DEFAULT_SAVE_DIR


def set_save_dir(path: str) -> None:
    """
    Set a save directory override, persisted across restarts. Attempts
    to create the directory immediately (rather than waiting until the
    next file is saved) specifically so a Settings screen can show a
    clear error right away if the chosen folder isn't usable (e.g. no
    write permission), instead of the person only discovering that
    later, mid-transfer. Raises OSError on failure - callers should
    catch this and show it, not let it propagate as a crash.
    """
    os.makedirs(path, exist_ok=True)  # raises OSError if not usable
    json_store.atomic_write_json(_SAVE_DIR_CONFIG_PATH, {"save_dir_override": path})


def reset_save_dir() -> None:
    """
    Clear any override, reverting to DEFAULT_SAVE_DIR. Uses try/except
    rather than checking existence first - a check-then-remove pattern
    has its own small race (the file could be removed by a concurrent
    call between the check and the removal) - either way "the override
    is gone" is the desired end state, so a FileNotFoundError here is
    harmless and simply ignored rather than treated as an error.
    """
    try:
        os.remove(_SAVE_DIR_CONFIG_PATH)
    except FileNotFoundError:
        pass


def get_staging_dir() -> str:
    """
    Where in-progress transfers and their manifests live - always
    computed FROM the current effective save dir (see get_save_dir()),
    so a save-dir override moves staging along with it.
    """
    return os.path.join(get_save_dir(), "Partial Downloads")

# Characters forbidden in filenames on Windows (some are also awkward
# elsewhere). We replace rather than reject outright, so a peer
# sending an unusual filename doesn't crash the transfer - it just
# lands with a slightly modified name.
_ILLEGAL_FILENAME_CHARS = '<>:"/\\|?*'

# Most filesystems cap an individual filename component at 255
# characters/bytes (NTFS, ext4, etc.). We stay comfortably under that,
# leaving headroom for the ".<32-char transfer_id>.part" suffix
# staging_paths() adds on top of this (~38 extra characters) - without
# a cap, a peer sending an unusually long filename could push the
# STAGED file's name over the real OS limit, surfacing as a confusing
# OSError deep inside preallocate_file rather than a clear failure
# here, at the point where we actually know what's going wrong.
MAX_FILENAME_LENGTH = 200


def sanitize_filename(filename: str) -> str:
    """
    Turn a filename received from a peer into something safe to
    actually create on disk, regardless of platform.

    Beyond the path-traversal stripping we already did (basename), a
    filename could still contain characters Windows forbids entirely
    in file names (: * ? " < > | and friends), forbidden trailing
    characters (Windows disallows a filename ending in a space or a
    dot), or simply be too long for the filesystem to accept. None of
    this is exploitable in a dangerous way, but left unhandled it
    would surface as a confusing OSError when we tried to create the
    file - bad experience for something that's about to be GUI-facing
    and receiving files from a less controlled source.
    """
    name = os.path.basename(filename)

    cleaned_chars = []
    for ch in name:
        if ch in _ILLEGAL_FILENAME_CHARS or ord(ch) < 32:
            cleaned_chars.append("_")
        else:
            cleaned_chars.append(ch)
    cleaned = "".join(cleaned_chars)

    # Windows disallows filenames ending in a space or a dot.
    cleaned = cleaned.rstrip(" .")

    # If sanitizing left us with nothing usable (e.g. the whole name
    # was illegal characters), fall back to a generic placeholder
    # rather than trying to create a file with an empty name.
    if not cleaned:
        cleaned = "unnamed_file"

    # Truncate overly long names, preserving the extension so the
    # file type isn't lost - only the base name gets shortened.
    if len(cleaned) > MAX_FILENAME_LENGTH:
        base, ext = os.path.splitext(cleaned)
        keep = max(MAX_FILENAME_LENGTH - len(ext), 1)
        cleaned = base[:keep] + ext

    return cleaned


def preallocate_file(path: str, filesize: int) -> None:
    """
    Create a file of exactly `filesize` bytes, filled with zeros.

    Seeking to the last byte and writing a single zero forces the
    filesystem to commit to a file of that final size right away -
    this makes an out-of-disk-space condition fail immediately and
    clearly, rather than partway through a large transfer, and it's
    what makes safe random-offset chunk writes possible at all.
    """
    parent_dir = os.path.dirname(path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    with open(path, "wb") as f:
        if filesize > 0:
            f.seek(filesize - 1)
            f.write(b"\x00")


def staging_paths(transfer_id: bytes, filename: str) -> tuple[str, str]:
    """
    Given a transfer_id and the (already sanitized) filename, return
    (data_path, manifest_path) - the two files used to track this
    transfer while it's incomplete.

    The filename is included in the actual name on disk (not just the
    transfer_id) so someone browsing the "Partial Downloads" folder
    can immediately tell what a given .part file IS, rather than
    seeing only an opaque hex string. The full transfer_id is still
    included too, guaranteeing uniqueness even if the same filename is
    offered more than once (e.g. a retried or duplicate transfer)
    without needing any separate collision-detection logic.
    """
    hex_id = transfer_id.hex()
    staging_dir = get_staging_dir()
    data_path = os.path.join(staging_dir, f"{filename}.{hex_id}.part")
    manifest_path = os.path.join(staging_dir, f"{filename}.{hex_id}.json")
    return data_path, manifest_path


def write_manifest(manifest_path: str, filename: str, filesize: int,
                    chunk_size: int, verified_chunks: list[int]) -> None:
    """
    Write (or overwrite) the manifest describing a staged transfer's
    current state. Called after processing each chunk, so the on-disk
    record stays up to date even if the process were to crash or the
    connection were to drop mid-transfer - written atomically (see
    json_store) for the same reason: a crash mid-write here shouldn't
    leave behind a corrupted manifest that a future resume feature (or
    a curious person opening it, per our earlier design choice to keep
    staging visible) can't parse.
    """
    manifest = {
        "filename": filename,
        "filesize": filesize,
        "chunk_size": chunk_size,
        "verified_chunks": sorted(verified_chunks),
    }
    json_store.atomic_write_json(manifest_path, manifest)


def unique_final_path(save_dir: str, filename: str) -> str:
    """
    Given a desired filename, return a path in save_dir that doesn't
    already exist - appending " (1)", " (2)", etc. before the file
    extension if needed, same idea as how browsers handle duplicate
    downloads.
    """
    candidate = os.path.join(save_dir, filename)
    if not os.path.exists(candidate):
        return candidate

    base, ext = os.path.splitext(filename)
    counter = 1
    while True:
        candidate = os.path.join(save_dir, f"{base} ({counter}){ext}")
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def finalize_transfer(data_path: str, manifest_path: str,
                       save_dir: str, filename: str) -> str:
    """
    Move a completed, verified staged file into its final location,
    picking a collision-safe name. Cleans up the manifest afterward,
    since it's no longer needed once the transfer is done.

    Returns the final path the file was saved to.
    """
    os.makedirs(save_dir, exist_ok=True)
    final_path = unique_final_path(save_dir, filename)

    os.replace(data_path, final_path)  # atomic move-or-rename

    if os.path.exists(manifest_path):
        os.remove(manifest_path)

    return final_path
