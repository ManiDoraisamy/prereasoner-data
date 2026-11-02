"""The labels of FIBO's classes and properties, from the project's own archive (MIT, EDM Council), to check which pack names carry a FIBO label. Standard library only; run with python -I on a fetched archive.

usage: python -I fibo_labels.py <fibo.zip> <out.json>
"""
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
RDFS = "{http://www.w3.org/2000/01/rdf-schema#}"
KEEP = ("/FND/", "/FBC/", "/DER/", "/SEC/", "/BE/", "/IND/", "/LOAN/", "/MD/")


def main():
    labels = {}
    with zipfile.ZipFile(sys.argv[1]) as archive:
        for name in archive.namelist():
            if not name.endswith(".rdf") or not any(part in name for part in KEEP):
                continue
            try:
                root = ET.fromstring(archive.read(name))
            except ET.ParseError:
                continue
            for node in root:
                about = node.get(f"{RDF}about")
                label = node.find(f"{RDFS}label")
                if about and label is not None and label.text:
                    text = re.sub(r"\s+", " ", label.text.strip().lower())
                    labels.setdefault(text, about)
    with open(sys.argv[2], "w", encoding="utf-8", newline="\n") as sink:
        json.dump(dict(sorted(labels.items())), sink, indent=0)
    print("labels", len(labels))


if __name__ == "__main__":
    main()
