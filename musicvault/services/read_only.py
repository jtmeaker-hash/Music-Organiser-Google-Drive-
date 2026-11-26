"""Enforce the read-only release policy before building Google requests."""
from googleapiclient.http import HttpRequest


def read_only_request(http, postproc, uri, method="GET", **kwargs):
    if method.upper() not in {"GET", "HEAD"}:
        raise RuntimeError("Cloud mutations are disabled in MusicVault.")
    return HttpRequest(http, postproc, uri, method=method, **kwargs)
