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
import json
import platformdirs

APP_NAME = "p2p-transfer"

DEFAULT_SAVE_DIR = os.path.join(platformdirs.user_downloads_dir(), "P2P Transfer")

# Where we persist a save-dir override, if the person has set one via
# a Settings screen. Separate tiny file rather than folding into
# contacts.json/history.json, since this is a distinct concern (app
# configuration, not saved relationships or activity records).
_SAVE_DIR_CONFIG_PATH = os.path.join(
    platformdirs.user_config_dir(APP_NAME, appauthor=False), "storage_settings.json"
)


def _load_save_dir_override():
    """Return the overridden save dir, or None if using the default."""
    if not os.path.exists(_SAVE_DIR_CONFIG_PATH):
        return None
    with open(_SAVE_DIR_CONFIG_PATH, "r") as f:
        data = json.load(f)
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

    parent_dir = os.path.dirname(_SAVE_DIR_CONFIG_PATH)
    os.makedirs(parent_dir, exist_ok=True)
    with open(_SAVE_DIR_CONFIG_PATH, "w") as f:
        json.dump({"save_dir_override": path}, f, indent=2)


def reset_save_dir() -> None:
    """Clear any override, reverting to DEFAULT_SAVE_DIR."""
    if os.path.exists(_SAVE_DIR_CONFIG_PATH):
        os.remove(_SAVE_DIR_CONFIG_PATH)


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


def sanitize_filename(filename: str) -> str:
    """
    Turn a filename received from a peer into something safe to
    actually create on disk, regardless of platform.

    Beyond the path-traversal stripping we already did (basename), a
    filename could still contain characters Windows forbids entirely
    in file names (: * ? " < > | and friends), or forbidden trailing
    characters (Windows disallows a filename ending in a space or a
    dot). None of this is exploitable in a dangerous way, but left
    unhandled it would surface as a confusing OSError when we tried to
    create the file - bad experience for something that's about to be
    GUI-facing and receiving files from a less controlled source.
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
    connection were to drop mid-transfer.
    """
    manifest = {
        "filename": filename,
        "filesize": filesize,
        "chunk_size": chunk_size,
        "verified_chunks": sorted(verified_chunks),
    }
    with open(manifest_path, "w") as f:
        json.dump(manifest, f)


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
