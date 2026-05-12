"""Media download/upload helpers for WeChat addon."""
from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import os
import secrets
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import api
from .types import (
    CDNMedia, UploadMediaType, MessageItemType,
    ImageItem, VoiceItem, FileItem, VideoItem, MessageItem,
)

log = logging.getLogger(__name__)

# Extension → UploadMediaType mapping
_UPLOAD_TYPE_MAP = {
    ".jpg": UploadMediaType.IMAGE,
    ".jpeg": UploadMediaType.IMAGE,
    ".png": UploadMediaType.IMAGE,
    ".gif": UploadMediaType.IMAGE,
    ".webp": UploadMediaType.IMAGE,
    ".bmp": UploadMediaType.IMAGE,
    ".mp4": UploadMediaType.VIDEO,
    ".avi": UploadMediaType.VIDEO,
    ".mov": UploadMediaType.VIDEO,
    ".mkv": UploadMediaType.VIDEO,
    ".wav": UploadMediaType.VOICE,
    ".mp3": UploadMediaType.VOICE,
    ".ogg": UploadMediaType.VOICE,
    ".silk": UploadMediaType.VOICE,
    ".amr": UploadMediaType.VOICE,
}

# UploadMediaType → MessageItemType mapping
_ITEM_TYPE_MAP = {
    UploadMediaType.IMAGE: MessageItemType.IMAGE,
    UploadMediaType.VIDEO: MessageItemType.VIDEO,
    UploadMediaType.VOICE: MessageItemType.VOICE,
    UploadMediaType.FILE: MessageItemType.FILE,
}


async def download_media(
    cdn_media: CDNMedia,
    dest_dir: str | Path,
    filename: str = "media",
) -> str:
    """Download media from CDN. Returns local file path."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    url = cdn_media.full_url
    if not url:
        raise ValueError("CDN media has no full_url")

    dest_path = dest_dir / filename
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=60.0)
        resp.raise_for_status()
        dest_path.write_bytes(resp.content)

    return str(dest_path)


def decode_voice(silk_path: str | Path, out_path: str | Path) -> str:
    """Decode Silk audio to WAV. Returns output path.

    Requires the `pilk` package: pip install pilk
    """
    try:
        import pilk
    except ImportError:
        log.warning("pilk not installed — cannot decode Silk voice. pip install pilk")
        return str(silk_path)

    silk_path = str(silk_path)
    out_path = str(out_path)
    pilk.decode(silk_path, out_path)
    return out_path


def detect_upload_type(file_path: str | Path) -> UploadMediaType:
    """Detect UploadMediaType from file extension. Defaults to FILE."""
    ext = Path(file_path).suffix.lower()
    return _UPLOAD_TYPE_MAP.get(ext, UploadMediaType.FILE)


def _pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    pad = block - (len(data) % block)
    return data + bytes([pad]) * pad


def _aes128_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    enc = cipher.encryptor()
    return enc.update(_pkcs7_pad(data, 16)) + enc.finalize()


def _encrypted_size(raw_size: int, block: int = 16) -> int:
    # PKCS#7 always appends at least one block when already aligned.
    return raw_size + (block - (raw_size % block))


async def upload_media(
    file_path: str | Path,
    base_url: str,
    token: str,
    to_user_id: str,
) -> CDNMedia:
    """Upload a file to WeChat CDN. Returns CDNMedia reference for sendMessage.

    Mirrors Hermes/OpenClaw: iLink expects getuploadurl to receive raw size,
    raw MD5, AES key, and padded ciphertext size; the CDN upload body is
    AES-128-ECB encrypted with PKCS#7 padding and posted with HTTP POST
    (not PUT); sendMessage then references encrypt_query_param + aes_key +
    encrypt_type=1. Earlier versions of this addon used plaintext PUT, which
    iLink would accept (HTTP 200) but produce an image the WeChat client
    could not decrypt/open.
    """
    file_path = Path(file_path)
    if not file_path.is_file():
        raise FileNotFoundError(f"File not found: {file_path}")

    data = file_path.read_bytes()
    md5 = hashlib.md5(data).hexdigest()
    media_type = detect_upload_type(file_path)
    aeskey = secrets.token_bytes(16)
    aeskey_hex = aeskey.hex()
    ciphertext = _aes128_ecb_encrypt(data, aeskey)
    filekey = secrets.token_hex(16)

    # Get upload URL. filesize is ciphertext size, rawsize is plaintext size.
    # Hermes/OpenClaw include filekey + no_need_thumb; without filekey iLink
    # may return HTTP 200 but omit upload_full_url.
    upload_resp = await api.get_upload_url(
        base_url, token,
        media_type=int(media_type),
        to_user_id=to_user_id,
        rawsize=len(data),
        rawfilemd5=md5,
        filesize=len(ciphertext),
        aeskey=aeskey_hex,
        filekey=filekey,
        no_need_thumb=True,
    )

    upload_url = upload_resp.upload_full_url
    if not upload_url:
        raise RuntimeError("Server did not return an upload URL")

    # Upload encrypted bytes to CDN. OpenClaw uses POST (not PUT) and reads
    # the final download encrypted_query_param from the x-encrypted-param
    # response header. Some CDN responses also embed it in a JSON body as a
    # fallback.
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            upload_url,
            content=ciphertext,
            headers={"Content-Type": "application/octet-stream"},
            timeout=120.0,
        )
        resp.raise_for_status()
        raw = resp.text

    download_param = (
        resp.headers.get("x-encrypted-param")
        or upload_resp.upload_param
        or filekey
    )
    try:
        if raw and raw.strip().startswith("{"):
            body = resp.json()
            download_param = (
                resp.headers.get("x-encrypted-param")
                or body.get("encrypt_query_param")
                or body.get("download_param")
                or download_param
            )
    except Exception:
        pass

    return CDNMedia(
        encrypt_query_param=download_param,
        # OpenClaw sends media.aes_key as base64(32-char hex string), not
        # base64(raw 16 bytes). The WeChat client decrypts the CDN payload
        # using this exact form; the raw-bytes form looks valid but renders
        # as an un-openable image.
        aes_key=base64.b64encode(aeskey_hex.encode("ascii")).decode("ascii"),
        encrypt_type=1,
    )


def make_media_item(cdn_media: CDNMedia, file_path: Path) -> MessageItem:
    """Create a MessageItem for sending uploaded media."""
    upload_type = detect_upload_type(file_path)
    item_type = _ITEM_TYPE_MAP.get(upload_type, MessageItemType.FILE)

    item = MessageItem(type=int(item_type))
    if item_type == MessageItemType.IMAGE:
        # OpenClaw/Hermes set mid_size = ciphertext byte count. Without it,
        # sendMessage returns ok but the WeChat client fails to display.
        item.image_item = ImageItem(
            media=cdn_media,
            mid_size=_encrypted_size(file_path.stat().st_size),
        )
    elif item_type == MessageItemType.VIDEO:
        item.video_item = VideoItem(media=cdn_media)
    elif item_type == MessageItemType.VOICE:
        item.voice_item = VoiceItem(media=cdn_media)
    else:
        item.file_item = FileItem(media=cdn_media, file_name=file_path.name)

    return item
