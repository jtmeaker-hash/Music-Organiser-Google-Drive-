"""
Bandwidth-aware seekable remote HTTP Range reader and byte-budget accounting for Google Drive media.
Verifies HTTP 206 + Content-Range, aborts immediately if server ignores Range (HTTP 200),
detects mid-read remote version changes, and never fabricates sparse audio bytes.
"""
import io
import re
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple
import requests


class HydrationTransportError(RuntimeError):
    """Base error for remote ranged media reads."""


class RangeIgnoredError(HydrationTransportError):
    """Raised when server returns HTTP 200 OK instead of 206 Partial Content."""


class RangeUnsatisfiableError(HydrationTransportError):
    """Raised when HTTP 416 cannot be resolved cleanly."""


class RemoteVersionChangedError(HydrationTransportError):
    """Raised when remote ETag or generation changes mid-read."""


class BudgetExceededError(HydrationTransportError):
    """Raised when global hydration byte budget is exhausted."""


class PerFileRangeCapError(HydrationTransportError):
    """Raised when ranged reads for a single file exceed per-file byte cap."""


class InsufficientDiskSpaceError(HydrationTransportError):
    """Raised when free temporary storage is insufficient for opt-in full download."""


class UnsupportedAudioStreamError(HydrationTransportError):
    """Raised when audio container cannot be parsed from bounded stream."""


@dataclass
class RangeFetchResult:
    """Result from a single HTTP Range GET attempt."""
    status_code: int
    headers: Dict[str, str]
    data: bytes
    bytes_transferred: int


_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(?:(\d+)-(\d+)|\*)/(\d+|\*)$", re.IGNORECASE)


def parse_content_range(header_val: str) -> Optional[Tuple[Optional[int], Optional[int], Optional[int]]]:
    """Parse HTTP Content-Range header into (start, end, total)."""
    if not header_val:
        return None
    m = _CONTENT_RANGE_RE.match(header_val.strip())
    if not m:
        return None
    s_raw, e_raw, t_raw = m.groups()
    start = int(s_raw) if s_raw is not None else None
    end = int(e_raw) if e_raw is not None else None
    total = int(t_raw) if (t_raw and t_raw != "*") else None
    return start, end, total


class ByteBudgetTracker:
    """Tracks actual network bytes transferred against a strict run cap."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max(0, int(max_bytes))
        self.bytes_used = 0

    @property
    def remaining_bytes(self) -> int:
        return max(0, self.max_bytes - self.bytes_used)

    @property
    def exhausted(self) -> bool:
        return self.bytes_used >= self.max_bytes

    def can_spend(self, num_bytes: int) -> bool:
        return (self.bytes_used + max(0, num_bytes)) <= self.max_bytes

    def record(self, num_bytes: int) -> None:
        if num_bytes <= 0:
            return
        self.bytes_used += int(num_bytes)
        if self.bytes_used > self.max_bytes:
            raise BudgetExceededError(
                f"Hydration byte budget exceeded ({self.bytes_used} > {self.max_bytes} bytes)"
            )


def make_requests_range_fetcher(
    session: requests.Session,
    url: str,
    budget: ByteBudgetTracker,
    timeout: int = 25,
) -> Callable[[int, int, int], RangeFetchResult]:
    """
    Build a streaming HTTP Range fetcher using an authenticated requests.Session (e.g. AuthorizedSession).
    Immediately aborts and closes the response stream if server returns HTTP 200 instead of 206.
    """
    def _fetch(start: int, end: int, max_allowed_bytes: int) -> RangeFetchResult:
        if budget.exhausted:
            raise BudgetExceededError("Hydration byte budget reached before request")

        req_headers = {"Range": f"bytes={start}-{end}"}
        resp = session.get(url, headers=req_headers, stream=True, timeout=timeout)
        try:
            norm_headers = {k.lower(): v for k, v in (resp.headers or {}).items()}
            if resp.status_code == 200:
                # Server ignored Range header! Close immediately without reading body into RAM.
                resp.close()
                raise RangeIgnoredError(
                    "Server returned HTTP 200 OK instead of 206 Partial Content; aborted stream"
                )
            if resp.status_code == 416:
                resp.close()
                return RangeFetchResult(
                    status_code=416,
                    headers=norm_headers,
                    data=b"",
                    bytes_transferred=0,
                )
            if resp.status_code != 206:
                status = resp.status_code
                resp.close()
                raise HydrationTransportError(f"Unexpected HTTP {status} during ranged media read")

            expected_max = (end - start) + 1
            buf = bytearray()
            transferred = 0
            for chunk in resp.iter_content(chunk_size=32768):
                if not chunk:
                    continue
                chunk_len = len(chunk)
                transferred += chunk_len
                budget.record(chunk_len)
                if transferred > max_allowed_bytes:
                    resp.close()
                    raise PerFileRangeCapError(
                        f"Single-file ranged transfer exceeded cap ({transferred} > {max_allowed_bytes} bytes)"
                    )
                if transferred > expected_max:
                    resp.close()
                    raise RangeIgnoredError(
                        f"Server streamed more bytes ({transferred}) than requested range ({expected_max})"
                    )
                buf.extend(chunk)

            return RangeFetchResult(
                status_code=206,
                headers=norm_headers,
                data=bytes(buf),
                bytes_transferred=transferred,
            )
        finally:
            resp.close()

    return _fetch


class DriveRangeReader(io.RawIOBase):
    """
    Bounded, block-cached seekable remote reader for Mutagen tag/duration inspection.
    Never invents zero-filled audio data; every returned byte is verified from HTTP 206 responses.
    """

    def __init__(
        self,
        fetcher: Callable[[int, int, int], RangeFetchResult],
        known_size: int = 0,
        expected_md5: str = "",
        block_size: int = 65536,
        max_file_bytes: int = 2 * 1024 * 1024,
        max_retries: int = 2,
        retry_delay: float = 0.2,
    ):
        super().__init__()
        self._fetcher = fetcher
        self._size: Optional[int] = int(known_size) if known_size and known_size > 0 else None
        self._expected_md5 = expected_md5
        self._block_size = max(4096, int(block_size))
        self._max_file_bytes = max(self._block_size, int(max_file_bytes))
        self._max_retries = max(0, int(max_retries))
        self._retry_delay = max(0.0, float(retry_delay))
        self._pos = 0
        self._blocks: Dict[int, bytes] = {}
        self._bytes_transferred = 0
        self._observed_etag: Optional[str] = None
        self._observed_generation: Optional[str] = None

    @property
    def bytes_transferred(self) -> int:
        return self._bytes_transferred

    @property
    def size(self) -> Optional[int]:
        return self._size

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed DriveRangeReader")
        return self._pos

    def _ensure_size_known(self) -> int:
        """Probe first block if remote file size was initially unknown (0)."""
        if self._size is not None:
            return self._size
        self._get_block(0)
        if self._size is None:
            # Fallback to sum of contiguous loaded blocks if Content-Range had '*'
            if 0 in self._blocks:
                self._size = len(self._blocks[0])
            else:
                self._size = 0
        return self._size

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed DriveRangeReader")
        if whence == io.SEEK_SET:
            target = int(offset)
        elif whence == io.SEEK_CUR:
            target = self._pos + int(offset)
        elif whence == io.SEEK_END:
            total = self._ensure_size_known()
            target = total + int(offset)
        else:
            raise ValueError(f"Invalid whence: {whence}")

        if target < 0:
            raise OSError("Negative seek position is invalid")
        self._pos = target
        return self._pos

    def _verify_version_headers(self, headers: Dict[str, str]) -> None:
        etag = headers.get("etag", "").strip()
        gen = headers.get("x-goog-generation", "").strip()
        md5_hdr = headers.get("x-goog-hash", "")

        if etag:
            if self._observed_etag is None:
                self._observed_etag = etag
            elif self._observed_etag != etag:
                raise RemoteVersionChangedError(
                    f"Remote ETag changed during ranged read ({self._observed_etag} -> {etag})"
                )
        if gen:
            if self._observed_generation is None:
                self._observed_generation = gen
            elif self._observed_generation != gen:
                raise RemoteVersionChangedError(
                    f"Remote file generation changed during ranged read ({self._observed_generation} -> {gen})"
                )
        if self._expected_md5 and "md5=" in md5_hdr:
            # Optional sanity check when x-goog-hash carries base64 md5
            import base64
            try:
                expected_b64 = base64.b64encode(bytes.fromhex(self._expected_md5)).decode("ascii")
                if f"md5={expected_b64}" not in md5_hdr:
                    raise RemoteVersionChangedError("Remote x-goog-hash MD5 differs from scanned MD5")
            except ValueError:
                pass

    def _get_block(self, block_index: int) -> bytes:
        if block_index in self._blocks:
            return self._blocks[block_index]

        start = block_index * self._block_size
        if self._size is not None and start >= self._size:
            return b""

        end = start + self._block_size - 1
        if self._size is not None:
            end = min(end, self._size - 1)
        expected_len = (end - start) + 1 if self._size is not None else None

        remaining_file_cap = self._max_file_bytes - self._bytes_transferred
        if remaining_file_cap <= 0:
            raise PerFileRangeCapError(
                f"Per-file ranged read cap reached ({self._bytes_transferred} / {self._max_file_bytes} bytes)"
            )

        last_err: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            try:
                res = self._fetcher(start, end, remaining_file_cap)
                self._bytes_transferred += res.bytes_transferred

                if res.status_code == 416:
                    parsed_416 = parse_content_range(res.headers.get("content-range", ""))
                    if parsed_416 and parsed_416[2] is not None:
                        self._size = parsed_416[2]
                        if start >= self._size:
                            return b""
                    raise RangeUnsatisfiableError(f"HTTP 416 Range Not Satisfiable at offset {start}")

                if res.status_code != 206:
                    raise RangeIgnoredError(f"Expected HTTP 206, got {res.status_code}")

                self._verify_version_headers(res.headers)

                cr = parse_content_range(res.headers.get("content-range", ""))
                if cr is None or cr[0] != start or cr[1] is None or cr[1] < start:
                    raise RangeIgnoredError(
                        f"Missing or invalid Content-Range header: {res.headers.get('content-range', '')!r}"
                    )

                cr_start, cr_end, cr_total = cr
                if cr_total is not None:
                    if self._size is not None and self._size != cr_total:
                        raise RemoteVersionChangedError(
                            f"Remote file size changed during read ({self._size} -> {cr_total})"
                        )
                    self._size = cr_total
                    expected_len = min(end, self._size - 1) - start + 1

                expected_chunk_len = (cr_end - cr_start) + 1
                if len(res.data) != expected_chunk_len or (
                    expected_len is not None and len(res.data) < expected_len
                ):
                    raise HydrationTransportError(
                        f"Short ranged read at offset {start}: got {len(res.data)} bytes, expected {expected_len or expected_chunk_len}"
                    )

                self._blocks[block_index] = res.data
                return res.data

            except (
                RangeIgnoredError,
                RemoteVersionChangedError,
                BudgetExceededError,
                PerFileRangeCapError,
            ):
                # Non-retryable safety/budget errors abort immediately
                raise
            except (HydrationTransportError, requests.RequestException, OSError) as exc:
                last_err = exc
                remaining_file_cap = self._max_file_bytes - self._bytes_transferred
                if remaining_file_cap <= 0:
                    raise PerFileRangeCapError(
                        f"Per-file ranged read cap reached after retry ({self._bytes_transferred} bytes)"
                    ) from exc
                if attempt < self._max_retries:
                    if self._retry_delay > 0:
                        time.sleep(self._retry_delay * (2 ** attempt))
                    continue
                break

        raise HydrationTransportError(f"Ranged read failed at offset {start}: {last_err}") from last_err

    def read(self, size: int = -1) -> bytes:
        if self.closed:
            raise ValueError("I/O operation on closed DriveRangeReader")
        if size == 0:
            return b""

        if self._size is not None and self._pos >= self._size:
            return b""

        if size is None or size < 0:
            total = self._ensure_size_known()
            target_len = max(0, total - self._pos)
        else:
            target_len = int(size)
            if self._size is not None:
                target_len = min(target_len, max(0, self._size - self._pos))

        if target_len == 0:
            return b""

        out = bytearray()
        while len(out) < target_len:
            if self._size is not None and self._pos >= self._size:
                break
            block_idx = self._pos // self._block_size
            block_off = self._pos % self._block_size
            block_data = self._get_block(block_idx)
            if not block_data or block_off >= len(block_data):
                break
            take = min(target_len - len(out), len(block_data) - block_off)
            out.extend(block_data[block_off : block_off + take])
            self._pos += take
            if len(block_data) < self._block_size:
                # Reached final partial block at EOF
                break

        return bytes(out)

    def readinto(self, b: bytearray) -> int:
        data = self.read(len(b))
        n = len(data)
        b[:n] = data
        return n
