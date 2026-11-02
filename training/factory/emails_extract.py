"""Pull from the emails archive only the frozen mailboxes' messages that carry a spreadsheet attachment. No file is written under the archive's own paths, which run long and hold odd characters: each kept message goes to <out>/<mailbox>/<hash>.eml. Bytes only; nothing here parses a sentence or prints one.

The deposit's first volume has an empty start header, as if the archive was uploaded before 7-Zip closed it. Its end header is whole: the start header is rebuilt on a joined copy by finding that header (see phase3_factory.txt).

usage: python -I emails_extract.py <py7zr's folder> <joined eml.7z> <frozen_list.json> <out>
"""
import hashlib
import json
import os
import re
import sys
import threading
import time

ATTACHED = re.compile(rb"(?i)(?:file)?name\*?=[^\r\n]{0,200}\.xl[sm]x?")
KEPT = [0, 0, 0, 0]     # read, kept, bytes, no mailbox


def main():
    sys.path.insert(0, sys.argv[1])
    import py7zr
    from py7zr.io import Py7zIO, WriterFactory
    with open(sys.argv[3], encoding="utf-8") as source:
        listing = json.load(source)
    folders, wanted = listing["map"], [name for name in listing["wanted"] if name.lower().endswith(".eml")]
    out = sys.argv[4]
    for box in set(folders.values()):
        os.makedirs(os.path.join(out, box), exist_ok=True)
    lock = threading.Lock()

    class Keeper(Py7zIO):
        def __init__(self, name):
            self.name = name
            self.parts = []
            self.length = 0

        def write(self, chunk):
            if self.length < 40 * 1024 ** 2:
                self.parts.append(bytes(chunk))
            self.length += len(chunk)
            return len(chunk)

        def read(self, size=None):
            return b""

        def seek(self, offset, whence=0):
            return 0

        def flush(self):
            return None

        def close(self):
            body = b"".join(self.parts)
            self.parts = []
            if not body:
                return
            with lock:
                KEPT[0] += 1
                if not ATTACHED.search(body):
                    return
                KEPT[1] += 1
                KEPT[2] += len(body)
                if KEPT[0] % 5000 == 0:
                    print(KEPT, round(time.time() - STARTED), "s", flush=True)
            box = next((folders[part] for part in self.name.split("/") if part in folders), None)
            if box is None:
                with lock:
                    KEPT[3] += 1
                return
            digest = hashlib.blake2b(self.name.encode("utf-8", "ignore"), digest_size=10).hexdigest()
            with open(os.path.join(out, box, digest + ".eml"), "wb") as sink:
                sink.write(body)

        def size(self):
            return self.length

    class Factory(WriterFactory):
        def create(self, filename):
            return Keeper(filename)

    STARTED = time.time()
    with py7zr.SevenZipFile(sys.argv[2], "r") as archive:
        archive.extract(targets=wanted, factory=Factory(), recursive=False)
    print("done: read, kept, bytes", KEPT, round(time.time() - STARTED), "s", flush=True)


if __name__ == "__main__":
    main()
