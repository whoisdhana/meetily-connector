"""Cloud transcription via Google Gemini 3.5 Transcribe.

Flow: resumable upload to the Gemini Files API -> POST /v1beta/interactions
with a transcription_config -> speaker-labelled text. The uploaded audio file
is deleted from Google's storage afterward (best effort) — the connector
treats meeting audio as sensitive.

Diarization limits audio to 30 minutes per request (API constraint); callers
disable it for longer recordings. Plain transcription allows up to 1 hour.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

API_BASE = "https://generativelanguage.googleapis.com"

_MIME_BY_EXT = {
    ".wav": "audio/wav",
    ".mp3": "audio/mp3",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",  # Meetily saves recordings as audio.mp4 (AAC)
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
}

AUDIO_EXTENSIONS = tuple(_MIME_BY_EXT)


class TranscribeError(Exception):
    """Transcription failed; caller falls back to the local transcript."""


@dataclass
class GeminiTranscript:
    text: str
    model: str
    diarized: bool


def find_audio_file(folder_path: str) -> Path | None:
    """Pick the largest audio file in a meeting's recording folder."""
    folder = Path(folder_path).expanduser()
    if not folder.is_dir():
        return None
    candidates = [p for p in folder.iterdir()
                  if p.suffix.lower() in _MIME_BY_EXT and p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def transcribe_file(path: Path, api_key: str, *, model: str,
                    language_codes: list[str], diarization: bool,
                    custom_vocabulary: list[str],
                    timeout: int = 600) -> GeminiTranscript:
    mime = _MIME_BY_EXT.get(path.suffix.lower())
    if mime is None:
        raise TranscribeError(f"unsupported audio extension {path.suffix!r}")

    file_name, file_uri = _upload(path, mime, api_key, timeout)
    try:
        _wait_active(file_name, api_key, timeout)
        response = _interact(file_uri, mime, api_key, model, language_codes,
                             diarization, custom_vocabulary, timeout)
    finally:
        _delete_file(file_name, api_key)

    text = _extract_text(response)
    if not text.strip():
        raise TranscribeError("empty transcript in API response")
    return GeminiTranscript(text=text, model=model, diarized=diarization)


# -- HTTP plumbing (stdlib urllib only) --------------------------------------


def _request(url: str, api_key: str, *, method: str = "POST",
             headers: dict | None = None, data: bytes | None = None,
             timeout: int = 60) -> tuple[dict, dict]:
    """Returns (parsed json body, response headers). Raises TranscribeError."""
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"x-goog-api-key": api_key,
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            parsed = json.loads(body) if body.strip() else {}
            return parsed, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        # Error class + status only; API error bodies can echo request detail.
        raise TranscribeError(f"http_{exc.code}") from exc
    except urllib.error.URLError as exc:
        reason = type(exc.reason).__name__ if exc.reason else "URLError"
        raise TranscribeError(f"transport_{reason}") from exc
    except TimeoutError:
        raise TranscribeError("transport_Timeout")


def _upload(path: Path, mime: str, api_key: str, timeout: int) -> tuple[str, str]:
    size = path.stat().st_size
    start_body = json.dumps({"file": {"display_name": path.stem}}).encode()
    _, headers = _request(
        f"{API_BASE}/upload/v1beta/files", api_key,
        headers={
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": mime,
            "Content-Type": "application/json",
        },
        data=start_body, timeout=60,
    )
    upload_url = headers.get("X-Goog-Upload-URL") or headers.get("x-goog-upload-url")
    if not upload_url:
        raise TranscribeError("no upload URL in Files API response")

    body, _ = _request(
        upload_url, api_key,
        headers={
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
            "Content-Type": mime,
        },
        data=path.read_bytes(), timeout=timeout,
    )
    info = body.get("file") or {}
    name, uri = info.get("name"), info.get("uri")
    if not name or not uri:
        raise TranscribeError("upload finalize returned no file name/uri")
    return name, uri


def _wait_active(file_name: str, api_key: str, timeout: int) -> None:
    deadline = time.monotonic() + min(timeout, 300)
    while True:
        body, _ = _request(f"{API_BASE}/v1beta/{file_name}", api_key,
                           method="GET", timeout=30)
        state = body.get("state", "")
        if state == "ACTIVE":
            return
        if state == "FAILED":
            raise TranscribeError("uploaded file processing failed")
        if time.monotonic() > deadline:
            raise TranscribeError("uploaded file never became ACTIVE")
        time.sleep(2)


def _interact(file_uri: str, mime: str, api_key: str, model: str,
              language_codes: list[str], diarization: bool,
              custom_vocabulary: list[str], timeout: int) -> dict:
    transcription_config: dict = {}
    if language_codes:
        transcription_config["language_codes"] = language_codes
    if custom_vocabulary:
        transcription_config["custom_vocabulary"] = custom_vocabulary
    if diarization:
        transcription_config["mode"] = {
            "type": "verbatim",
            "diarization_mode": "speaker",
        }
    payload = {
        "model": model,
        "input": [{"type": "audio", "uri": file_uri, "mime_type": mime}],
        "generation_config": {"transcription_config": transcription_config},
    }
    body, _ = _request(
        f"{API_BASE}/v1beta/interactions", api_key,
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode(), timeout=timeout,
    )
    return body


def _delete_file(file_name: str, api_key: str) -> None:
    try:
        _request(f"{API_BASE}/v1beta/{file_name}", api_key,
                 method="DELETE", timeout=30)
    except TranscribeError:
        pass  # best effort; files auto-expire server-side after ~48h


# -- response parsing ---------------------------------------------------------


def _extract_text(response: dict) -> str:
    obj = response.get("interaction", response)

    # Prefer speaker-labelled text reconstructed from word annotations.
    labelled = _speaker_labelled(obj)
    if labelled:
        return labelled

    text = obj.get("output_text")
    if isinstance(text, str) and text.strip():
        return text

    parts = []
    for step in obj.get("steps") or []:
        for content in step.get("content") or []:
            if content.get("type") == "text" and content.get("text"):
                parts.append(content["text"])
    return "\n".join(parts)


def _speaker_labelled(obj: dict) -> str:
    """Group word_info annotations into 'spk_N: …' lines, if present."""
    lines: list[str] = []
    current_speaker = None
    current_words: list[str] = []
    seen_any = False

    for step in obj.get("steps") or []:
        for content in step.get("content") or []:
            for note in content.get("annotations") or []:
                if note.get("type") != "word_info" or "speaker" not in note:
                    continue
                seen_any = True
                speaker = note.get("speaker")
                if speaker != current_speaker:
                    if current_words:
                        lines.append(f"{current_speaker}: {' '.join(current_words)}")
                    current_speaker, current_words = speaker, []
                current_words.append(note.get("text", ""))
    if current_words:
        lines.append(f"{current_speaker}: {' '.join(current_words)}")
    return "\n".join(lines) if seen_any else ""
