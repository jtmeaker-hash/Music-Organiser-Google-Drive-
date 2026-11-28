"""Enforce request-level cloud mutation boundaries for Google Drive and Google Sheets."""
import urllib.parse
from googleapiclient.http import HttpRequest


def read_only_request(http, postproc, uri, method="GET", **kwargs):
    if method.upper() not in {"GET", "HEAD"}:
        raise RuntimeError("Cloud mutations are disabled in MusicVault.")
    return HttpRequest(http, postproc, uri, method=method, **kwargs)


def controlled_sheets_request(http, postproc, uri, method="GET", **kwargs):
    """
    Request builder for controlled Google Sheets operations ONLY:
    - Permits GET/HEAD on Google Sheets API.
    - Permits POST/PUT strictly on `https://sheets.googleapis.com/v4/spreadsheets/`
      (blocking DELETE and blocking any Google Drive or non-Sheets URI).
    """
    m_up = (method or "GET").upper()
    parsed = urllib.parse.urlsplit(str(uri or ""))
    is_sheets_v4 = (
        parsed.scheme == "https"
        and parsed.netloc == "sheets.googleapis.com"
        and parsed.path.startswith("/v4/spreadsheets/")
    )
    if not is_sheets_v4:
        raise RuntimeError(f"Controlled Sheets client blocked non-Sheets URI: {uri}")
    if m_up not in {"GET", "HEAD", "POST", "PUT"}:
        raise RuntimeError(f"Controlled Sheets client blocked HTTP method {m_up} on {uri}")
    return HttpRequest(http, postproc, uri, method=method, **kwargs)
