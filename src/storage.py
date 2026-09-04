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
"""

import os
import json

STAGING_DIR = os.path.join("received", ".partial")


def staging_paths(transfer_id: bytes) -> tuple[str, str]:
    """
    Given a transfer_id, return (data_path, manifest_path) - the two
    files used to track this transfer while it's incomplete.
    """
    hex_id = transfer_id.hex()
    data_path = os.path.join(STAGING_DIR, f"{hex_id}.part")
    manifest_path = os.path.join(STAGING_DIR, f"{hex_id}.json")
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
