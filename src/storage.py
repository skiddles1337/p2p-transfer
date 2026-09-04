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

  - SAVE_DIR: where FINISHED files actually land - under the user's
    Downloads folder, in a "P2P Transfer" subfolder, since that's
    where people already expect to find things a program downloaded
    for them, same idea as a web browser's downloads.
  - STAGING_DIR: in-progress transfers and their manifests. Previously
    this was a hidden, internal app-data location - but in-progress
    downloads are genuinely interesting to see (their existence is
    what a future "resume" feature will build on), so this now lives
    right alongside finished downloads, in a visible "Partial
    Downloads" subfolder - browsable, not hidden away.
"""

import os
import json
import platformdirs

APP_NAME = "p2p-transfer"

SAVE_DIR = os.path.join(platformdirs.user_downloads_dir(), "P2P Transfer")
STAGING_DIR = os.path.join(SAVE_DIR, "Partial Downloads")

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
    data_path = os.path.join(STAGING_DIR, f"{filename}.{hex_id}.part")
    manifest_path = os.path.join(STAGING_DIR, f"{filename}.{hex_id}.json")
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
