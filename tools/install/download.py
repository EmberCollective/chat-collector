"""Bounded official-runtime download after private Python becomes available."""
from pathlib import Path
import sys
import time
import urllib.request


def download(url, destination):
    started = time.monotonic()
    with urllib.request.urlopen(url, timeout=30) as response, Path(destination).open('wb') as output:
        while chunk := response.read(1024 * 1024):
            if time.monotonic() - started > 180:
                raise TimeoutError('Download exceeded three minutes')
            output.write(chunk)


if __name__ == '__main__':
    download(sys.argv[1], sys.argv[2])
