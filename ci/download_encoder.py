"""Download an externally trained checkpoint, verify its actual checksum, extract safely."""

import argparse
import hashlib
import re
import stat
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlparse

ALLOWED = {
    "model.safetensors",
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "vocab.txt",
    "added_tokens.json",
    "encoder_metadata.json",
}
MAX_BYTES = 512 * 1024 * 1024


def download(url, checksum, destination: Path, open_url=urllib.request.urlopen):
    if urlparse(url).scheme != "https" or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("an HTTPS URL and actual lowercase SHA-256 are required")
    destination = destination.resolve()
    if destination.exists():
        raise ValueError("destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as temporary:
        digest, size = hashlib.sha256(), 0
        with open_url(url, timeout=60) as response:
            if urlparse(response.geturl()).scheme != "https":
                raise ValueError("insecure download redirect")
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("checkpoint download exceeds size limit")
                digest.update(chunk)
                temporary.write(chunk)
        if digest.hexdigest() != checksum:
            raise ValueError("checkpoint checksum mismatch")
        temporary.seek(0)
        with zipfile.ZipFile(temporary) as archive:
            infos = archive.infolist()
            names = [i.filename for i in infos]
            if len(names) != len(set(names)) or not set(names) <= ALLOWED:
                raise ValueError("unexpected or unsafe checkpoint files")
            required = {"model.safetensors", "config.json", "tokenizer_config.json"}
            if not required <= set(names) or not {"tokenizer.json", "vocab.txt"} & set(names):
                raise ValueError("incomplete checkpoint")
            if sum(i.file_size for i in infos) > MAX_BYTES or any(
                stat.S_ISLNK(i.external_attr >> 16) for i in infos
            ):
                raise ValueError("unsafe checkpoint archive")
            destination.mkdir()
            for info in infos:
                with (
                    archive.open(info) as source,
                    (destination / info.filename).open("wb") as target,
                ):
                    while chunk := source.read(1024 * 1024):
                        target.write(chunk)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--destination", type=Path, default=Path("state/encoder"))
    args = parser.parse_args()
    print(download(args.url, args.sha256, args.destination))


if __name__ == "__main__":
    main()
