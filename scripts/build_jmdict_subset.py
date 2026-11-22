"""Build src/mimivox/lexicon/jmdict_common.tsv.gz from EDRDG's JMdict_e.gz.

    curl -O http://ftp.edrdg.org/pub/Nihongo/JMdict_e.gz
    python -I scripts/build_jmdict_subset.py JMdict_e.gz

Keeps entries carrying a commonness tag (news/ichi/spec/gai/nf), ordered most
common first, with at most MAX_SENSES short senses each. One line per entry:
``kanji forms | readings <TAB> sense; sense``. JMdict is (c) EDRDG, CC-BY-SA 4.0;
see THIRD_PARTY_NOTICES.md. Run on the downloaded file with ``-I`` (untrusted).
"""

from __future__ import annotations

import gzip
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

OUT = (
    Path(__file__).resolve().parent.parent / "src" / "mimivox" / "lexicon" / "jmdict_common.tsv.gz"
)
MAX_SENSES = 2
MAX_GLOSSES = 2
_PRI = re.compile(r"^(news[12]|ichi[12]|spec[12]|gai[12]|nf\d\d)$")


def _rank(tags: list[str]) -> int:
    """Lower is more common (news1/ichi1/spec1/gai1 first, then nfNN buckets)."""
    best = 99
    for t in tags:
        if t.endswith("1") and not t.startswith("nf"):
            best = min(best, 0)
        elif t.startswith("nf"):
            best = min(best, 1 + int(t[2:]) // 12)
        else:
            best = min(best, 6)
    return best


def entries(path: Path):
    with gzip.open(path, "rb") as fh:
        for _, el in ET.iterparse(fh):
            if el.tag != "entry":
                continue
            kanji = [
                (k.findtext("keb"), [p.text for p in k.findall("ke_pri")])
                for k in el.findall("k_ele")
            ]
            readings = [
                (r.findtext("reb"), [p.text for p in r.findall("re_pri")])
                for r in el.findall("r_ele")
            ]
            tags = [t for _, ts in kanji + readings for t in ts if _PRI.match(t or "")]
            if tags:
                senses = []
                for s in el.findall("sense")[:MAX_SENSES]:
                    gl = [g.text for g in s.findall("gloss") if g.text][:MAX_GLOSSES]
                    if gl:
                        senses.append(", ".join(gl))
                if senses:
                    yield (
                        _rank(tags),
                        "·".join(k for k, _ in kanji),
                        "·".join(r for r, _ in readings),
                        "; ".join(senses),
                    )
            el.clear()


def main() -> None:
    rows = sorted(entries(Path(sys.argv[1])), key=lambda r: r[0])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8", compresslevel=9, newline="\n") as fh:
        for _, k, r, g in rows:
            fh.write(f"{k}|{r}\t{g.replace(chr(9), ' ')}\n")
    print(f"{len(rows)} entries -> {OUT} ({OUT.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
