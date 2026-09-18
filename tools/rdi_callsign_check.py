#!/usr/bin/env python3
"""
rdi_callsign_check.py - controleer welke roepletters nog vrij zijn in het RDI-callbook.

Doorloopt standaard alle combinaties PD0A t/m PD9Z (derde teken 0-9, vierde teken
A-Z, dus 10 x 26 = 260 calls) en vraagt per call aan het callbook van de RDI
(https://callbook.rdi.nl/) of die roepletters geregistreerd staan.

Het callbook is een JavaScript-applicatie zonder gepubliceerde, stabiele API.
Dit script gokt daarom geen endpoint, maar herhaalt het request dat de site zelf
doet. Werkwijze:

  1. Zoek in de browser eenmalig een call op (bijvoorbeeld je eigen call).
  2. DevTools -> Network -> het zoek-request -> rechtermuis -> Copy -> Copy as cURL (bash).
  3. Plak dat in een bestand, bijvoorbeeld request.curl.
  4. Draai:
       python3 rdi_callsign_check.py --curl-file request.curl --sample-call PD1ABC \
           --calibrate-taken PD1ABC --calibrate-free PD9ZZ9

Het script vervangt de voorbeeldcall in URL, headers en body door elke te
controleren call en classificeert het antwoord als 'vrij', 'bezet' of 'onbekend'.

Zonder --curl-file kan het script ook zelf een verzoek opbouwen (--url /
--method / --data met {call} als plaatshouder), en met --discover de site
inspecteren om het endpoint te vinden.

Alleen standaardbibliotheek, dus geen pip install nodig. Python 3.8+.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import random
import re
import shlex
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from typing import Dict, List, Optional, Tuple

BASE_URL = "https://callbook.rdi.nl/"
PLACEHOLDER = "{call}"
DEFAULT_UA = (
    "rdi-callsign-check/1.0 (persoonlijke controle op vrije roepletters; "
    "lage aanvraagsnelheid)"
)

# Standaard herkenningspatronen voor het antwoord van het callbook.
# Ze zijn met opzet ruim: bij twijfel wordt 'onbekend' gerapporteerd in plaats
# van een gok. Pas ze aan met --free-regex / --taken-regex zodra je weet welke
# tekst de site precies teruggeeft.
DEFAULT_FREE_REGEX = (
    r"niet\s+gevonden|geen\s+resultaa?t|geen\s+resultaten|geen\s+registratie|"
    r"geen\s+gegevens|niet\s+geregistreerd|niet\s+bekend|niet\s+uitgegeven|"
    r"\b0\s+resultaten\b|no\s+results|not\s+found"
)
DEFAULT_TAKEN_REGEX = (
    r"\bin\s+gebruik\b|geregistreerd\s+op|registratienummer|vergunninghouder|"
    r"\bcallsign\b\s*[:=]|\broepletters\b\s*[:=]|licentie"
)

STATUS_FREE = "vrij"
STATUS_TAKEN = "bezet"
STATUS_UNKNOWN = "onbekend"
STATUS_ERROR = "fout"

CSV_FIELDS = ["callsign", "status", "http_status", "checked_at", "detail"]


# --------------------------------------------------------------------------- #
# Callsign-generatie
# --------------------------------------------------------------------------- #
def build_callsigns(prefix: str, digits: str, letters: str) -> List[str]:
    """PD0A, PD0B, ... PD0Z, PD1A, ... PD9Z."""
    return [f"{prefix}{d}{l}" for d in digits for l in letters]


def expand_range(spec: str, full: str) -> str:
    """Zet '0-9', 'A-C', 'AQZ' om naar een reeks tekens; leeg betekent alles."""
    if not spec:
        return full
    out: List[str] = []
    i = 0
    spec = spec.upper()
    while i < len(spec):
        if i + 2 < len(spec) and spec[i + 1] == "-":
            start, end = spec[i], spec[i + 2]
            if start not in full or end not in full or full.index(start) > full.index(end):
                raise ValueError(f"ongeldig bereik: {start}-{end}")
            out.extend(full[full.index(start): full.index(end) + 1])
            i += 3
        else:
            if spec[i] not in full:
                raise ValueError(f"ongeldig teken: {spec[i]}")
            out.append(spec[i])
            i += 1
    # dubbele tekens verwijderen, volgorde behouden
    seen: Dict[str, None] = {}
    for c in out:
        seen.setdefault(c, None)
    return "".join(seen)


# --------------------------------------------------------------------------- #
# Requestbeschrijving
# --------------------------------------------------------------------------- #
@dataclass
class RequestSpec:
    url: str
    method: str = "GET"
    headers: Dict[str, str] = field(default_factory=dict)
    body: Optional[str] = None

    def copy(self) -> "RequestSpec":
        return RequestSpec(self.url, self.method, dict(self.headers), self.body)


def parse_curl(text: str) -> RequestSpec:
    """Lees een 'Copy as cURL (bash)'-commando uit DevTools."""
    text = re.sub(r"\\\s*\n", " ", text)          # bash regel-continuaties
    text = re.sub(r"\^\s*\n", " ", text)          # cmd.exe regel-continuaties
    text = text.replace("$'", "'")                # Chrome's ANSI-C quoting
    tokens = shlex.split(text)
    if tokens and os.path.basename(tokens[0]).startswith("curl"):
        tokens.pop(0)

    url: Optional[str] = None
    method: Optional[str] = None
    headers: Dict[str, str] = {}
    data_parts: List[str] = []

    no_arg = {
        "--compressed", "-s", "--silent", "-S", "--show-error", "-k", "--insecure",
        "-L", "--location", "-i", "--include", "-v", "--verbose", "-g", "--globoff",
        "-#", "--progress-bar", "--http1.1", "--http2", "--http2-prior-knowledge",
        "-4", "-6", "--fail", "-f", "--no-buffer",
    }
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if tok in ("-X", "--request") and nxt is not None:
            method, i = nxt.upper(), i + 2
        elif tok in ("-H", "--header") and nxt is not None:
            key, sep, val = nxt.partition(":")
            if sep:
                headers[key.strip()] = val.strip()
            i += 2
        elif tok in ("-b", "--cookie") and nxt is not None:
            headers["Cookie"] = nxt
            i += 2
        elif tok in ("-A", "--user-agent") and nxt is not None:
            headers["User-Agent"] = nxt
            i += 2
        elif tok in ("-e", "--referer") and nxt is not None:
            headers["Referer"] = nxt.split(";")[0]
            i += 2
        elif tok in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii") and nxt is not None:
            data_parts.append(nxt)
            i += 2
        elif tok == "--data-urlencode" and nxt is not None:
            key, sep, val = nxt.partition("=")
            data_parts.append(f"{key}={urllib.parse.quote(val)}" if sep else urllib.parse.quote(key))
            i += 2
        elif tok == "--url" and nxt is not None:
            url, i = nxt, i + 2
        elif tok in no_arg:
            i += 1
        elif tok.startswith("-"):
            # onbekende optie: sla hem over, inclusief een eventuele losse waarde
            i += 2 if (nxt is not None and not nxt.startswith("-")) else 1
        else:
            url, i = tok, i + 1

    if not url:
        raise ValueError("geen URL gevonden in het cURL-commando")
    body = "&".join(data_parts) if data_parts else None
    if method is None:
        method = "POST" if body is not None else "GET"
    return RequestSpec(url=url, method=method, headers=headers, body=body)


def substitute_call(spec: RequestSpec, sample: str, call: str) -> RequestSpec:
    """Vervang de voorbeeldcall (of de plaatshouder) overal door de te controleren call."""
    pattern = re.compile(re.escape(sample), re.IGNORECASE)
    # Alleen bij een echte voorbeeldcall nemen we het hoofdlettergebruik over;
    # een plaatshouder als {call} zegt niets over de gewenste schrijfwijze.
    mirror_case = sample.isalnum()

    def swap(match: "re.Match[str]") -> str:
        found = match.group(0)
        if not mirror_case:
            return call
        if found.islower():
            return call.lower()
        if found.isupper():
            return call.upper()
        return call

    out = spec.copy()
    out.url = pattern.sub(swap, out.url)
    out.headers = {k: pattern.sub(swap, v) for k, v in out.headers.items()}
    if out.body is not None:
        out.body = pattern.sub(swap, out.body)
    return out


def spec_contains(spec: RequestSpec, needle: str) -> bool:
    hay = [spec.url, spec.body or ""] + list(spec.headers.values())
    return any(needle.lower() in part.lower() for part in hay)


def build_spec_from_flags(url: str, method: str, data: Optional[str],
                          extra_headers: List[str]) -> RequestSpec:
    headers: Dict[str, str] = {}
    for item in extra_headers:
        key, sep, val = item.partition(":")
        if not sep:
            raise ValueError(f"header moet 'Naam: waarde' zijn, niet: {item}")
        headers[key.strip()] = val.strip()
    return RequestSpec(url=url, method=method.upper(), headers=headers, body=data)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
@dataclass
class Response:
    status: int
    headers: Dict[str, str]
    text: str
    error: Optional[str] = None

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


def make_opener() -> Tuple[urllib.request.OpenerDirector, CookieJar]:
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = []  # wij zetten alle headers zelf
    return opener, jar


def _decode_body(raw: bytes, encoding_header: str, charset: str) -> str:
    enc = (encoding_header or "").lower()
    try:
        if "gzip" in enc:
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    except OSError:
        pass  # niet ingepakt ondanks de header
    return raw.decode(charset, errors="replace")


def do_request(opener: urllib.request.OpenerDirector, spec: RequestSpec,
               timeout: float) -> Response:
    headers = dict(spec.headers)
    # brotli kan de standaardbibliotheek niet uitpakken
    headers["Accept-Encoding"] = "gzip, deflate"
    headers.setdefault("User-Agent", DEFAULT_UA)
    data = spec.body.encode("utf-8") if spec.body is not None else None
    req = urllib.request.Request(spec.url, data=data, headers=headers, method=spec.method)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            charset = resp.headers.get_content_charset() or "utf-8"
            return Response(resp.status, hdrs,
                            _decode_body(raw, hdrs.get("content-encoding", ""), charset))
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b""
        hdrs = {k.lower(): v for k, v in (exc.headers or {}).items()}
        charset = "utf-8"
        if exc.headers is not None:
            charset = exc.headers.get_content_charset() or "utf-8"
        return Response(exc.code, hdrs,
                        _decode_body(raw, hdrs.get("content-encoding", ""), charset),
                        error=f"HTTP {exc.code}")
    except urllib.error.URLError as exc:
        return Response(0, {}, "", error=f"netwerkfout: {exc.reason}")
    except TimeoutError:
        return Response(0, {}, "", error="timeout")


def fetch_with_retries(opener: urllib.request.OpenerDirector, spec: RequestSpec,
                       timeout: float, retries: int, backoff: float,
                       verbose: bool = False) -> Response:
    resp = Response(0, {}, "", error="niet uitgevoerd")
    for attempt in range(retries + 1):
        resp = do_request(opener, spec, timeout)
        transient = resp.status in (0, 408, 425, 429, 500, 502, 503, 504)
        if not transient:
            return resp
        if attempt == retries:
            break
        wait = backoff * (2 ** attempt)
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                wait = max(wait, float(retry_after))
            except ValueError:
                pass
        if verbose:
            print(f"    herkansing {attempt + 1}/{retries} over {wait:.1f}s "
                  f"({resp.error or resp.status})", file=sys.stderr)
        time.sleep(wait)
    return resp


# --------------------------------------------------------------------------- #
# Classificatie
# --------------------------------------------------------------------------- #
class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def visible_text(html_text: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html_text)
    except Exception:
        return html_text
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def json_result_count(obj) -> Optional[int]:
    """Probeer het aantal zoekresultaten uit een JSON-antwoord te halen."""
    if isinstance(obj, list):
        return len(obj)
    if isinstance(obj, dict):
        for key in ("total", "totalCount", "totalElements", "count", "aantal",
                    "totaal", "numberOfResults", "numFound", "hitCount"):
            val = obj.get(key)
            if isinstance(val, bool):
                continue
            if isinstance(val, int):
                return val
        for key in ("results", "result", "items", "data", "hits", "records",
                    "content", "rows", "callsigns", "registraties", "registrations",
                    "vergunningen", "list"):
            val = obj.get(key)
            if isinstance(val, list):
                return len(val)
            if isinstance(val, dict):
                nested = json_result_count(val)
                if nested is not None:
                    return nested
    return None


@dataclass
class Classifier:
    free_re: "re.Pattern[str]"
    taken_re: "re.Pattern[str]"
    tiebreak: str = "none"          # none | free | taken
    echo_check: bool = False

    def classify(self, resp: Response, call: str) -> Tuple[str, str]:
        if resp.error and resp.status == 0:
            return STATUS_ERROR, resp.error
        if resp.status >= 400:
            return STATUS_ERROR, f"HTTP {resp.status}: {visible_text(resp.text)[:120]}"

        body = resp.text
        stripped = body.lstrip()
        looks_json = "json" in resp.content_type or stripped[:1] in ("{", "[")
        if looks_json:
            try:
                obj = json.loads(body)
            except ValueError:
                obj = None
            if obj is not None:
                count = json_result_count(obj)
                if count is not None:
                    return ((STATUS_FREE, "json: 0 resultaten") if count == 0
                            else (STATUS_TAKEN, f"json: {count} resultaat/resultaten"))
                haystack = json.dumps(obj, ensure_ascii=False).lower()
                return self._by_regex(haystack, call, "json-tekst")

        return self._by_regex(visible_text(body).lower(), call, "tekst")

    def _by_regex(self, haystack: str, call: str, source: str) -> Tuple[str, str]:
        free_hit = self.free_re.search(haystack)
        taken_hit = self.taken_re.search(haystack)
        if free_hit and not taken_hit:
            return STATUS_FREE, f"{source}: '{free_hit.group(0).strip()}'"
        if taken_hit and not free_hit:
            return STATUS_TAKEN, f"{source}: '{taken_hit.group(0).strip()}'"
        if self.echo_check and not free_hit and not taken_hit:
            if call.lower() in haystack:
                return STATUS_TAKEN, f"{source}: call komt terug in antwoord"
        if self.tiebreak == "free":
            return STATUS_FREE, f"{source}: geen duidelijk signaal, tiebreak=vrij"
        if self.tiebreak == "taken":
            return STATUS_TAKEN, f"{source}: geen duidelijk signaal, tiebreak=bezet"
        both = "beide patronen" if (free_hit and taken_hit) else "geen patroon"
        return STATUS_UNKNOWN, f"{source}: {both} herkend"


# --------------------------------------------------------------------------- #
# Discover-modus
# --------------------------------------------------------------------------- #
class _FormFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: List[str] = []
        self.inputs: List[str] = []
        self.scripts: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        a = dict(attrs)
        if tag == "form":
            self.forms.append(f"action={a.get('action')!r} method={a.get('method')!r}")
        elif tag in ("input", "select", "textarea"):
            self.inputs.append(
                f"<{tag} name={a.get('name')!r} id={a.get('id')!r} type={a.get('type')!r}>")
        elif tag == "script" and a.get("src"):
            self.scripts.append(a["src"])


def discover(base_url: str, timeout: float, max_scripts: int) -> int:
    opener, _ = make_opener()
    print(f"# GET {base_url}")
    resp = do_request(opener, RequestSpec(base_url, headers={"Accept": "text/html,*/*"}), timeout)
    if resp.error and resp.status == 0:
        print(f"  mislukt: {resp.error}")
        return 1
    print(f"  HTTP {resp.status}, {len(resp.text)} tekens, type {resp.content_type!r}\n")

    finder = _FormFinder()
    try:
        finder.feed(resp.text)
    except Exception:
        pass

    print("## Formulieren")
    print("\n".join(f"  {f}" for f in finder.forms) or "  (geen <form> gevonden)")
    print("\n## Invoervelden")
    print("\n".join(f"  {i}" for i in finder.inputs[:40]) or "  (geen)")

    url_re = re.compile(r"""["'`](/[A-Za-z0-9_\-./]*(?:api|search|zoek|call|register)"""
                        r"""[A-Za-z0-9_\-./]*)["'`]""", re.IGNORECASE)
    found = set(url_re.findall(resp.text))

    print("\n## Scripts")
    for src in finder.scripts[:max_scripts]:
        full = urllib.parse.urljoin(base_url, src)
        print(f"  {full}")
        js = do_request(opener, RequestSpec(full, headers={"Accept": "*/*"}), timeout)
        if js.status == 200:
            found.update(url_re.findall(js.text))

    print("\n## Mogelijke endpoints (paden uit HTML/JS)")
    if found:
        for path in sorted(found):
            print(f"  {urllib.parse.urljoin(base_url, path)}")
    else:
        print("  (niets gevonden - gebruik DevTools -> Network -> Copy as cURL)")
    print("\nTip: bevestig het echte verzoek altijd via DevTools en draai daarna "
          "met --curl-file.")
    return 0


# --------------------------------------------------------------------------- #
# Resultaatopslag
# --------------------------------------------------------------------------- #
def load_existing(path: str) -> Dict[str, Dict[str, str]]:
    if not path or not os.path.exists(path):
        return {}
    out: Dict[str, Dict[str, str]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            call = (row.get("callsign") or "").strip().upper()
            if call:
                out[call] = row
    return out


def open_writer(path: str, existing: Dict[str, Dict[str, str]]):
    is_new = not existing and not os.path.exists(path)
    fh = open(path, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
    if is_new:
        writer.writeheader()
        fh.flush()
    return fh, writer


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Controleer beschikbaarheid van roepletters PD0A t/m PD9Z in het RDI-callbook.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    src = p.add_argument_group("verzoek")
    src.add_argument("--curl-file", help="bestand met 'Copy as cURL (bash)' uit DevTools")
    src.add_argument("--sample-call", help="de call die in dat cURL-verzoek staat, bv. PD1ABC")
    src.add_argument("--url", help=f"endpoint met {PLACEHOLDER} als plaatshouder (i.p.v. --curl-file)")
    src.add_argument("--method", default="GET", help="HTTP-methode bij --url (standaard GET)")
    src.add_argument("--data", help=f"request body bij --url, met {PLACEHOLDER} als plaatshouder")
    src.add_argument("--header", action="append", default=[], metavar="'Naam: waarde'",
                     help="extra header bij --url (herhaalbaar)")
    src.add_argument("--warmup-url", default=BASE_URL,
                     help="pagina die eerst wordt opgehaald voor cookies (leeg = overslaan)")

    rng = p.add_argument_group("reeks")
    rng.add_argument("--prefix", default="PD", help="voorvoegsel (standaard PD)")
    rng.add_argument("--digits", default="0-9", help="derde teken, bv. '0-9' of '024' (standaard 0-9)")
    rng.add_argument("--letters", default="A-Z", help="vierde teken, bv. 'A-Z' of 'A-C' (standaard A-Z)")
    rng.add_argument("--limit", type=int, help="stop na N calls (handig om te testen)")

    beh = p.add_argument_group("gedrag")
    beh.add_argument("--delay", type=float, default=2.0, help="pauze tussen aanvragen in seconden (standaard 2.0)")
    beh.add_argument("--jitter", type=float, default=0.3, help="willekeurige spreiding op de pauze, 0-1 (standaard 0.3)")
    beh.add_argument("--timeout", type=float, default=30.0, help="timeout per aanvraag (standaard 30s)")
    beh.add_argument("--retries", type=int, default=3, help="herkansingen bij tijdelijke fouten (standaard 3)")
    beh.add_argument("--backoff", type=float, default=2.0, help="basiswachttijd voor herkansingen (standaard 2.0s)")
    beh.add_argument("--recheck", action="store_true", help="ook calls opnieuw doen die al in de CSV staan")
    beh.add_argument("--max-consecutive-errors", type=int, default=10, metavar="N",
                     help="stop na N fouten op rij (standaard 10, 0 = nooit stoppen)")
    beh.add_argument("--ignore-robots", action="store_true", help="ga door ook als robots.txt het verbiedt")
    beh.add_argument("--dry-run", action="store_true", help="toon het eerste verzoek en de reeks, verstuur niets")
    beh.add_argument("--discover", action="store_true", help="inspecteer de site en stop")
    beh.add_argument("-v", "--verbose", action="store_true", help="meer uitvoer")

    cls = p.add_argument_group("classificatie")
    cls.add_argument("--free-regex", default=DEFAULT_FREE_REGEX, help="patroon dat 'vrij' aangeeft")
    cls.add_argument("--taken-regex", default=DEFAULT_TAKEN_REGEX, help="patroon dat 'bezet' aangeeft")
    cls.add_argument("--tiebreak", choices=["none", "free", "taken"], default="none",
                     help="uitkomst als geen of beide patronen matchen (standaard none = onbekend)")
    cls.add_argument("--echo-check", action="store_true",
                     help="beschouw het terugkomen van de call in het antwoord als 'bezet'")
    cls.add_argument("--calibrate-taken", metavar="CALL", help="bekende bezette call als controle vooraf")
    cls.add_argument("--calibrate-free", metavar="CALL", help="bekende vrije call als controle vooraf")

    out = p.add_argument_group("uitvoer")
    out.add_argument("-o", "--output", default="rdi_callsigns.csv", help="CSV-bestand (standaard rdi_callsigns.csv)")
    out.add_argument("--dump-dir", help="map om ruwe antwoorden in te bewaren bij 'onbekend'/'fout'")
    return p


def robots_allows(url: str, timeout: float, verbose: bool) -> Optional[bool]:
    import urllib.robotparser
    parts = urllib.parse.urlsplit(url)
    robots_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.read()
    except Exception as exc:                     # geen robots.txt of niet bereikbaar
        if verbose:
            print(f"robots.txt niet gelezen ({exc}); ga door", file=sys.stderr)
        return None
    return rp.can_fetch(DEFAULT_UA, url)


def resolve_spec(args: argparse.Namespace) -> Tuple[RequestSpec, str]:
    """Geeft het sjabloonverzoek plus de tekst die per call vervangen wordt."""
    if args.curl_file:
        if not args.sample_call:
            raise SystemExit("--sample-call is verplicht bij --curl-file: de call die "
                             "je in de browser opzocht toen je het verzoek kopieerde.")
        with open(args.curl_file, encoding="utf-8") as fh:
            spec = parse_curl(fh.read())
        sample = args.sample_call.strip()
        if not spec_contains(spec, sample):
            raise SystemExit(
                f"'{sample}' komt niet voor in URL, headers of body van het cURL-verzoek.\n"
                "Controleer of je het juiste zoekverzoek hebt gekopieerd en of de call "
                "daar letterlijk (niet ge-encodeerd) in staat.")
        return spec, sample
    if args.url:
        spec = build_spec_from_flags(args.url, args.method, args.data, args.header)
        if not spec_contains(spec, PLACEHOLDER):
            raise SystemExit(f"gebruik {PLACEHOLDER} in --url, --data of --header als plaatshouder")
        return spec, PLACEHOLDER
    raise SystemExit("geef --curl-file (aanbevolen) of --url op; of gebruik --discover")


def dump_response(dump_dir: str, call: str, resp: Response) -> None:
    os.makedirs(dump_dir, exist_ok=True)
    path = os.path.join(dump_dir, f"{call}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"HTTP {resp.status}\n")
        for k, v in resp.headers.items():
            fh.write(f"{k}: {v}\n")
        fh.write("\n")
        fh.write(resp.text)


def check_one(opener, template: RequestSpec, sample: str, call: str,
              classifier: Classifier, args: argparse.Namespace) -> Tuple[str, str, Response]:
    spec = substitute_call(template, sample, call)
    resp = fetch_with_retries(opener, spec, args.timeout, args.retries, args.backoff, args.verbose)
    status, detail = classifier.classify(resp, call)
    return status, detail, resp


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.discover:
        return discover(args.warmup_url or BASE_URL, args.timeout, max_scripts=6)

    try:
        digits = expand_range(args.digits, string.digits)
        letters = expand_range(args.letters, string.ascii_uppercase)
    except ValueError as exc:
        raise SystemExit(f"fout in --digits/--letters: {exc}")

    calls = build_callsigns(args.prefix.upper(), digits, letters)
    if args.limit:
        calls = calls[: args.limit]

    template, sample = resolve_spec(args)
    classifier = Classifier(
        free_re=re.compile(args.free_regex, re.IGNORECASE),
        taken_re=re.compile(args.taken_regex, re.IGNORECASE),
        tiebreak=args.tiebreak,
        echo_check=args.echo_check,
    )

    if args.dry_run:
        first = substitute_call(template, sample, calls[0])
        print(f"{len(calls)} calls: {', '.join(calls[:8])}"
              f"{' ...' if len(calls) > 8 else ''} (laatste: {calls[-1]})")
        print(f"\nEerste verzoek:\n  {first.method} {first.url}")
        for k, v in first.headers.items():
            print(f"  {k}: {v}")
        if first.body:
            print(f"\n  body: {first.body}")
        geschat = len(calls) * (args.delay + 0.5) / 60
        print(f"\nGeschatte looptijd: ongeveer {geschat:.0f} minuten bij --delay {args.delay}")
        return 0

    allowed = robots_allows(template.url, args.timeout, args.verbose)
    if allowed is False and not args.ignore_robots:
        raise SystemExit("robots.txt van de RDI staat geautomatiseerd ophalen van dit pad niet toe.\n"
                         "Stop hier, of draai bewust met --ignore-robots en houd --delay ruim.")

    opener, _ = make_opener()
    if args.warmup_url:
        if args.verbose:
            print(f"cookies ophalen via {args.warmup_url}")
        do_request(opener, RequestSpec(args.warmup_url, headers={"Accept": "text/html,*/*"}),
                   args.timeout)

    # IJken: eerst controleren of de herkenning klopt, voordat 260 aanvragen volgen.
    for label, call, expected in (("bezet", args.calibrate_taken, STATUS_TAKEN),
                                  ("vrij", args.calibrate_free, STATUS_FREE)):
        if not call:
            continue
        status, detail, resp = check_one(opener, template, sample, call.upper(), classifier, args)
        ok = "OK" if status == expected else "AFWIJKING"
        print(f"[ijken] {call.upper()} verwacht '{expected}' -> '{status}' ({detail}) [{ok}]")
        if status != expected:
            if args.dump_dir:
                dump_response(args.dump_dir, f"ijk_{call.upper()}", resp)
            raise SystemExit(
                "De herkenning klopt nog niet. Bekijk het antwoord (--dump-dir) en pas "
                "--free-regex / --taken-regex aan voordat je de hele reeks draait.")
        time.sleep(args.delay)

    existing = load_existing(args.output)
    todo = [c for c in calls
            if args.recheck or existing.get(c, {}).get("status") not in (STATUS_FREE, STATUS_TAKEN)]
    done_before = len(calls) - len(todo)
    if done_before:
        print(f"{done_before} calls staan al in {args.output} en worden overgeslagen "
              f"(gebruik --recheck om ze opnieuw te doen).")

    fh, writer = open_writer(args.output, existing)
    tally = {STATUS_FREE: 0, STATUS_TAKEN: 0, STATUS_UNKNOWN: 0, STATUS_ERROR: 0}
    free_calls: List[str] = []
    consecutive_errors = 0
    aborted = False
    try:
        for idx, call in enumerate(todo, 1):
            status, detail, resp = check_one(opener, template, sample, call, classifier, args)
            tally[status] = tally.get(status, 0) + 1
            if status == STATUS_FREE:
                free_calls.append(call)
            writer.writerow({
                "callsign": call,
                "status": status,
                "http_status": resp.status,
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "detail": detail,
            })
            fh.flush()
            if args.dump_dir and status in (STATUS_UNKNOWN, STATUS_ERROR):
                dump_response(args.dump_dir, call, resp)
            print(f"[{idx}/{len(todo)}] {call}: {status}"
                  + (f"  ({detail})" if args.verbose or status != STATUS_TAKEN else ""))

            consecutive_errors = consecutive_errors + 1 if status == STATUS_ERROR else 0
            if args.max_consecutive_errors and consecutive_errors >= args.max_consecutive_errors:
                print(f"\n{consecutive_errors} fouten op rij - gestopt om de site niet "
                      f"onnodig te belasten. Laatste fout: {detail}", file=sys.stderr)
                aborted = True
                break

            if idx < len(todo):
                pause = args.delay * (1 + random.uniform(-args.jitter, args.jitter))
                time.sleep(max(0.0, pause))
    except KeyboardInterrupt:
        print("\nOnderbroken. Resultaten tot nu toe staan in "
              f"{args.output}; opnieuw starten pakt de rest op.", file=sys.stderr)
        return 130
    finally:
        fh.close()

    print("\n--- samenvatting ---")
    print(f"vrij:     {tally[STATUS_FREE]}")
    print(f"bezet:    {tally[STATUS_TAKEN]}")
    print(f"onbekend: {tally[STATUS_UNKNOWN]}")
    print(f"fout:     {tally[STATUS_ERROR]}")
    if free_calls:
        print("\nMogelijk vrije roepletters:")
        for i in range(0, len(free_calls), 10):
            print("  " + " ".join(free_calls[i:i + 10]))
    if tally[STATUS_UNKNOWN] or tally[STATUS_ERROR]:
        print("\nLet op: niet alles is eenduidig herkend. Draai die calls opnieuw met "
              "--recheck en --dump-dir om de antwoorden te bekijken.")
    print(f"\nVolledige resultaten: {args.output}")
    if aborted:
        print("De reeks is voortijdig afgebroken; start opnieuw om de rest op te pakken.")
    print("Een 'vrije' call hier betekent alleen: niet in het callbook gevonden. "
          "De RDI bepaalt bij aanvraag of je hem echt kunt krijgen.")
    return 2 if aborted else 0


if __name__ == "__main__":
    sys.exit(main())
