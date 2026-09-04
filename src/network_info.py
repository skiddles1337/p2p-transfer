"""
network_info.py

Fetches the user's PUBLIC (external) IP address - the address their
port-forwarded listening port is actually reachable at from the
internet, as opposed to their LOCAL network IP (e.g. 192.168.x.x),
which only works from inside their own home network and is useless to
hand to a friend elsewhere.

Used by the "copy my info" GUI action: without this, generating a
connection string would require the person to look up and manually
type their own public IP - defeating much of the point of automating
the pairing flow.
"""

import urllib.request
import urllib.error
import webbrowser

# Tried in order - a single service being down, blocked, or rate-
# limiting doesn't leave this feature completely broken. Each of these
# returns the caller's IP as plain text and nothing else.
_IP_LOOKUP_URLS = [
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
]

_TIMEOUT_SECONDS = 5


def get_public_ip():
    """
    Return the caller's current public IPv4 address as a string, or
    None if every lookup attempt failed (no internet connection, all
    services unreachable/blocked, etc.). Callers should treat None as
    "couldn't auto-detect - ask the person to enter it manually,"
    rather than letting an exception surface into GUI code.
    """
    for url in _IP_LOOKUP_URLS:
        try:
            with urllib.request.urlopen(url, timeout=_TIMEOUT_SECONDS) as response:
                ip = response.read().decode("utf-8").strip()
                if _looks_like_an_ipv4_address(ip):
                    return ip
        except (urllib.error.URLError, TimeoutError, OSError):
            continue  # this service failed - try the next one

    return None


def _looks_like_an_ipv4_address(text: str) -> bool:
    """
    A light sanity check, not full validation - just enough to catch a
    service returning something unexpected (an HTML error page, a
    captive-portal redirect page, a rate-limit message) rather than
    blindly trusting arbitrary text as if it were definitely a real IP.
    """
    parts = text.split(".")
    if len(parts) != 4:
        return False
    return all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)


def open_port_check_tool() -> None:
    """
    Opens a reputable third-party port-checking website in the
    person's default browser, so they can manually verify their port
    forwarding actually works before asking a friend to test it
    blindly (or before wondering why a real connection attempt failed
    later).

    Deliberately NOT automated or scraped on our end: most of these
    sites use an internal form submission rather than a stable,
    documented URL interface, so trying to pre-fill the port or parse
    a result programmatically would be fragile and could silently
    break the moment the site changes its page layout. The person
    types their own port in manually - one extra step, but nothing
    here depends on the site's internals, so it can't quietly break.
    """
    webbrowser.open("https://canyouseeme.org/")


if __name__ == "__main__":
    print("Fetching public IP address...")
    ip = get_public_ip()
    if ip:
        print(f"Public IP: {ip}")
    else:
        print("Could not determine public IP - no internet connection, "
              "or all lookup services are unreachable right now.")
