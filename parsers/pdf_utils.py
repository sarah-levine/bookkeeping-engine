import re
import zlib
import struct
import subprocess
import tempfile
import os
from pathlib import Path


# ---------------------------------------------------------------------------
# Low-level PDF tokeniser / object parser
# ---------------------------------------------------------------------------

def _unescape_pdf_string(raw: bytes) -> bytes:
    """Decode PDF literal string escape sequences."""
    out = bytearray()
    i = 0
    while i < len(raw):
        b = raw[i]
        if b == ord('\\') and i + 1 < len(raw):
            c = raw[i + 1]
            if c == ord('n'):
                out.append(ord('\n')); i += 2
            elif c == ord('r'):
                out.append(ord('\r')); i += 2
            elif c == ord('t'):
                out.append(ord('\t')); i += 2
            elif c == ord('b'):
                out.append(ord('\b')); i += 2
            elif c == ord('f'):
                out.append(ord('\f')); i += 2
            elif c == ord('('):
                out.append(ord('(')); i += 2
            elif c == ord(')'):
                out.append(ord(')')); i += 2
            elif c == ord('\\'):
                out.append(ord('\\')); i += 2
            elif c == ord('\n'):
                i += 2  # escaped newline = continuation
            elif c == ord('\r'):
                i += 2
                if i < len(raw) and raw[i] == ord('\n'):
                    i += 1
            elif 0x30 <= c <= 0x37:  # octal \nnn
                octal = raw[i+1:i+4]
                digits = b''
                for d in octal:
                    if 0x30 <= d <= 0x37:
                        digits += bytes([d])
                    else:
                        break
                out.append(int(digits, 8) & 0xFF)
                i += 1 + len(digits)
            else:
                out.append(c); i += 2
        else:
            out.append(b); i += 1
    return bytes(out)


def _decode_hex_string(s: str) -> bytes:
    s = re.sub(r'\s+', '', s)
    if len(s) % 2:
        s += '0'
    return bytes.fromhex(s)


# ---------------------------------------------------------------------------
# Stream decompressor
# ---------------------------------------------------------------------------

def _decompress_stream(data: bytes, filters) -> bytes:
    if not filters:
        return data
    if isinstance(filters, str):
        filters = [filters]
    for f in filters:
        if f in ('/FlateDecode', 'FlateDecode'):
            try:
                data = zlib.decompress(data)
            except zlib.error:
                try:
                    data = zlib.decompress(data, -15)
                except zlib.error:
                    pass
        # ASCIIHexDecode / ASCII85Decode not needed for bank statements
    return data


# ---------------------------------------------------------------------------
# Minimal PDF object parser
# ---------------------------------------------------------------------------

_WS = b' \t\n\r\f\x00'


def _skip_ws(data: bytes, pos: int) -> int:
    while pos < len(data) and data[pos:pos+1] in [bytes([b]) for b in _WS]:
        pos += 1
    return pos


def _skip_ws_and_comments(data: bytes, pos: int) -> int:
    while pos < len(data):
        if data[pos:pos+1] in [bytes([b]) for b in _WS]:
            pos += 1
        elif data[pos:pos+1] == b'%':
            while pos < len(data) and data[pos:pos+1] not in (b'\n', b'\r'):
                pos += 1
        else:
            break
    return pos


def _read_token(data: bytes, pos: int):
    """Return (token_bytes, new_pos). token_bytes is None at EOF."""
    pos = _skip_ws_and_comments(data, pos)
    if pos >= len(data):
        return None, pos
    delimiters = b'()<>[]{}/%'
    c = data[pos:pos+1]
    if c in (b'(', b'<', b'[', b'{', b'>', b']', b')'):
        return c, pos + 1
    if c == b'/':
        end = pos + 1
        while end < len(data) and data[end:end+1] not in [bytes([b]) for b in _WS] and data[end:end+1] not in [bytes([d]) for d in delimiters]:
            end += 1
        return data[pos:end], end
    if c == b'%':
        end = pos
        while end < len(data) and data[end:end+1] not in (b'\n', b'\r'):
            end += 1
        return None, end  # comments return None but advance pos
    # regular token
    end = pos
    while end < len(data) and data[end:end+1] not in [bytes([b]) for b in _WS] and data[end:end+1] not in [bytes([d]) for d in delimiters]:
        end += 1
    if end == pos:
        return data[pos:pos+1], pos + 1
    return data[pos:end], end


def _parse_literal_string(data: bytes, pos: int):
    """pos points just after opening '('. Returns (bytes, new_pos)."""
    depth = 1
    out = bytearray()
    while pos < len(data):
        b = data[pos]
        if b == ord('\\') and pos + 1 < len(data):
            out.append(b)
            out.append(data[pos+1])
            pos += 2
        elif b == ord('('):
            depth += 1
            out.append(b); pos += 1
        elif b == ord(')'):
            depth -= 1
            if depth == 0:
                pos += 1; break
            out.append(b); pos += 1
        else:
            out.append(b); pos += 1
    return _unescape_pdf_string(bytes(out)), pos


def _parse_hex_string(data: bytes, pos: int):
    """pos points just after opening '<'. Returns (bytes, new_pos)."""
    end = data.find(b'>', pos)
    if end == -1:
        return b'', len(data)
    hex_str = data[pos:end].decode('ascii', errors='replace')
    return _decode_hex_string(hex_str), end + 1


def _parse_array(data: bytes, pos: int):
    """pos just after '['. Returns (list, new_pos)."""
    items = []
    while pos < len(data):
        pos = _skip_ws_and_comments(data, pos)
        if pos >= len(data):
            break
        c = data[pos:pos+1]
        if c == b']':
            pos += 1; break
        obj, pos = _parse_object(data, pos)
        if obj is not None:
            items.append(obj)
    return items, pos


def _parse_dict(data: bytes, pos: int):
    """pos just after '<<'. Returns (dict, new_pos)."""
    d = {}
    while pos < len(data):
        pos = _skip_ws_and_comments(data, pos)
        if pos >= len(data):
            break
        if data[pos:pos+2] == b'>>':
            pos += 2; break
        # read key
        key, pos = _parse_object(data, pos)
        if not isinstance(key, bytes) or not key.startswith(b'/'):
            continue
        pos = _skip_ws_and_comments(data, pos)
        val, pos = _parse_object(data, pos)
        d[key[1:].decode('latin-1')] = val
    return d, pos


def _parse_object(data: bytes, pos: int):
    """Parse one PDF object. Returns (obj, new_pos)."""
    pos = _skip_ws_and_comments(data, pos)
    if pos >= len(data):
        return None, pos
    c = data[pos:pos+1]

    if c == b'(':
        return _parse_literal_string(data, pos + 1)

    if c == b'<':
        if data[pos:pos+2] == b'<<':
            return _parse_dict(data, pos + 2)
        return _parse_hex_string(data, pos + 1)

    if c == b'[':
        return _parse_array(data, pos + 1)

    if c == b'/':
        tok, pos = _read_token(data, pos)
        return tok, pos

    if c in (b'>', b']', b')'):
        return None, pos + 1

    tok, pos = _read_token(data, pos)
    if tok is None:
        return None, pos
    s = tok.decode('latin-1')
    if s == 'true':
        return True, pos
    if s == 'false':
        return False, pos
    if s == 'null':
        return None, pos
    try:
        return int(s), pos
    except ValueError:
        pass
    try:
        return float(s), pos
    except ValueError:
        pass
    return tok, pos


# ---------------------------------------------------------------------------
# PDF file-level scanner
# ---------------------------------------------------------------------------

# Matches: "<num> <num> obj"
_OBJ_RE = re.compile(rb'(\d+)\s+(\d+)\s+obj\b')
# Matches stream start
_STREAM_RE = re.compile(rb'stream\s*(\r\n|\n|\r)')
_ENDSTREAM_RE = re.compile(rb'\bendstream\b')


def _get_stream_data(data: bytes, dict_end_pos: int, obj_dict: dict) -> bytes:
    """Extract and decompress stream bytes following a dict."""
    m = _STREAM_RE.search(data, dict_end_pos)
    if not m or m.start() > dict_end_pos + 20:
        return b''
    stream_start = m.end()

    length = obj_dict.get('Length')
    if isinstance(length, int) and length > 0:
        raw = data[stream_start: stream_start + length]
    else:
        m2 = _ENDSTREAM_RE.search(data, stream_start)
        raw = data[stream_start: m2.start()].rstrip(b'\r\n') if m2 else b''

    filters = obj_dict.get('Filter')
    if filters is None:
        return raw
    if isinstance(filters, list):
        filter_names = [f.decode('latin-1') if isinstance(f, bytes) else str(f) for f in filters]
    else:
        filter_names = [filters.decode('latin-1') if isinstance(filters, bytes) else str(filters)]
    return _decompress_stream(raw, filter_names)


class _PDFReader:
    def __init__(self, data: bytes):
        self.data = data
        # obj_num -> (dict, raw_stream_bytes_or_None, start_pos)
        self._objs: dict = {}
        self._scanned = False

    def _scan_all_objects(self):
        data = self.data
        for m in _OBJ_RE.finditer(data):
            num = int(m.group(1))
            pos = m.end()
            obj, pos2 = _parse_object(data, pos)
            if isinstance(obj, dict):
                self._objs[num] = (obj, pos2)
            else:
                self._objs[num] = ({}, pos2)
        self._scanned = True

    def _expand_objstm(self):
        """Decompress /Type /ObjStm object streams and parse embedded objects."""
        data = self.data
        for num, (d, pos2) in list(self._objs.items()):
            t = d.get('Type')
            type_name = t.decode('latin-1') if isinstance(t, bytes) else (t or '')
            if type_name != 'ObjStm':
                continue
            raw = _get_stream_data(data, pos2, d)
            if not raw:
                continue
            n = d.get('N', 0)
            first = d.get('First', 0)
            # Header section: n pairs of (obj_num offset)
            header = raw[:first]
            body = raw[first:]
            pairs = header.split()
            for i in range(0, len(pairs) - 1, 2):
                try:
                    obj_num = int(pairs[i])
                    offset = int(pairs[i+1])
                    obj, _ = _parse_object(body, offset)
                    if obj_num not in self._objs:
                        self._objs[obj_num] = (obj if isinstance(obj, dict) else {}, 0)
                    else:
                        existing_d, existing_pos = self._objs[obj_num]
                        if not existing_d and isinstance(obj, dict):
                            self._objs[obj_num] = (obj, 0)
                except (ValueError, IndexError):
                    continue

    def get_obj(self, ref):
        """Resolve an indirect reference [num, gen] or direct object."""
        if isinstance(ref, list) and len(ref) == 2:
            num = ref[0]
            return self._objs.get(num, ({}, 0))[0]
        return ref

    def get_obj_with_stream(self, num: int):
        if num not in self._objs:
            return {}, b''
        d, pos2 = self._objs[num]
        if not d.get('Filter') and not d.get('Length'):
            return d, b''
        raw = _get_stream_data(self.data, pos2, d)
        return d, raw

    def prepare(self):
        self._scan_all_objects()
        self._expand_objstm()


# ---------------------------------------------------------------------------
# Indirect reference detection
# ---------------------------------------------------------------------------

def _is_ref(obj):
    """True if obj looks like [int, int] i.e. an indirect reference."""
    return (isinstance(obj, list) and len(obj) == 2
            and isinstance(obj[0], int) and isinstance(obj[1], int))


def _resolve(reader: _PDFReader, obj):
    while _is_ref(obj):
        obj = reader.get_obj(obj)
    return obj


# ---------------------------------------------------------------------------
# Page tree walker
# ---------------------------------------------------------------------------

def _collect_page_nums(reader: _PDFReader, node_obj, acc: list):
    """Recursively walk /Pages tree, appending /Page object-numbers to acc."""
    node = _resolve(reader, node_obj)
    if not isinstance(node, dict):
        return
    t = node.get('Type', b'')
    type_s = t.decode('latin-1') if isinstance(t, bytes) else str(t)
    if type_s == 'Page':
        # Find its object number
        for num, (d, _) in reader._objs.items():
            if d is node:
                acc.append(num)
                return
        # fallback: add the dict reference so caller can iterate
        acc.append(node)
        return
    kids = _resolve(reader, node.get('Kids', []))
    if not isinstance(kids, list):
        return
    for kid in kids:
        _collect_page_nums(reader, kid, acc)


def _get_pages(reader: _PDFReader) -> list:
    """Return list of page dicts."""
    # Find catalog
    catalog = None
    for num, (d, _) in reader._objs.items():
        t = d.get('Type', b'')
        type_s = t.decode('latin-1') if isinstance(t, bytes) else str(t)
        if type_s == 'Catalog':
            catalog = d
            break
    if catalog is None:
        return []

    pages_ref = catalog.get('Pages')
    pages_node = _resolve(reader, pages_ref)
    if not isinstance(pages_node, dict):
        return []

    page_nums = []
    _collect_page_nums(reader, pages_node, page_nums)

    pages = []
    for item in page_nums:
        if isinstance(item, int):
            d, _ = reader._objs.get(item, ({}, 0))
            pages.append(d)
        elif isinstance(item, dict):
            pages.append(item)
    return pages


# ---------------------------------------------------------------------------
# Content stream text extractor
# ---------------------------------------------------------------------------

def _read_cs_string(tok: bytes) -> bytes:
    """Parse a single content-stream string token (literal or hex)."""
    if tok.startswith(b'(') and tok.endswith(b')'):
        return _unescape_pdf_string(tok[1:-1])
    if tok.startswith(b'<') and tok.endswith(b'>'):
        return _decode_hex_string(tok[1:-1].decode('ascii', errors='replace'))
    return b''


def _extract_text_from_stream(stream: bytes) -> str:
    """Parse PDF content stream operators and extract text."""
    # Tokenise properly — handle nested parens by re-scanning
    tokens = []
    i = 0
    data = stream
    while i < len(data):
        # skip whitespace
        while i < len(data) and data[i:i+1] in [bytes([b]) for b in _WS]:
            i += 1
        if i >= len(data):
            break
        c = data[i:i+1]
        if c == b'(':
            # find matching close paren, respecting nesting and escapes
            depth = 0
            j = i
            while j < len(data):
                ch = data[j]
                if ch == ord('\\'):
                    j += 2; continue
                if ch == ord('('):
                    depth += 1
                elif ch == ord(')'):
                    depth -= 1
                    if depth == 0:
                        j += 1; break
                j += 1
            tokens.append(data[i:j]); i = j
        elif c == b'<':
            if data[i:i+2] == b'<<':
                # inline dict — skip to >>
                end = data.find(b'>>', i+2)
                i = end + 2 if end != -1 else len(data)
            else:
                end = data.find(b'>', i+1)
                if end == -1:
                    i += 1
                else:
                    tokens.append(data[i:end+1]); i = end + 1
        elif c == b'[':
            tokens.append(b'['); i += 1
        elif c == b']':
            tokens.append(b']'); i += 1
        elif c == b'%':
            while i < len(data) and data[i:i+1] not in (b'\n', b'\r'):
                i += 1
        else:
            j = i
            delims = b'()<>[]{}/%'
            while j < len(data) and data[j:j+1] not in [bytes([b]) for b in _WS] and data[j:j+1] not in [bytes([d]) for d in delims]:
                j += 1
            if j > i:
                tokens.append(data[i:j]); i = j
            else:
                i += 1

    parts = []
    stack = []  # operand stack
    in_bt = False
    prev_y = None

    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        idx += 1

        if tok == b'BT':
            in_bt = True
            prev_y = None
            continue
        if tok == b'ET':
            in_bt = False
            continue

        if not in_bt:
            continue

        # Accumulate operands
        if tok == b'[':
            # collect array
            arr = []
            while idx < len(tokens) and tokens[idx] != b']':
                arr.append(tokens[idx]); idx += 1
            idx += 1  # skip ']'
            stack.append(arr)
            continue

        # operators
        if tok in (b'Tj', b"'"):
            if stack:
                s = stack.pop()
                if isinstance(s, bytes):
                    parts.append(s.decode('latin-1'))
            stack.clear()
            if tok == b"'":
                parts.append('\n')
            continue

        if tok == b'TJ':
            if stack:
                arr = stack.pop()
                if isinstance(arr, list):
                    word = ''
                    for item in arr:
                        if isinstance(item, bytes):
                            if item.startswith(b'(') and item.endswith(b')'):
                                word += _unescape_pdf_string(item[1:-1]).decode('latin-1')
                            elif item.startswith(b'<') and item.endswith(b'>'):
                                word += _decode_hex_string(item[1:-1].decode('ascii', errors='replace')).decode('latin-1', errors='replace')
                            else:
                                word += item.decode('latin-1', errors='replace')
                        elif isinstance(item, (int, float)):
                            # large negative kerning = word space
                            if item < -200:
                                word += ' '
                    parts.append(word)
            stack.clear()
            continue

        if tok in (b'Td', b'TD'):
            if len(stack) >= 2:
                try:
                    x_val = float(stack[-2]) if isinstance(stack[-2], (int, float)) else float(stack[-2])
                    y_val = float(stack[-1]) if isinstance(stack[-1], (int, float)) else float(stack[-1])
                    if y_val < -2:  # moved down = new line
                        parts.append('\n')
                    elif y_val > 2:  # moved up = new line boundary
                        parts.append('\n')
                except (ValueError, TypeError, IndexError):
                    pass
            stack.clear()
            continue

        if tok == b'T*':
            parts.append('\n')
            stack.clear()
            continue

        if tok in (b'Tm', b'Tf', b'Ts', b'Tc', b'Tw', b'Tz', b'TL', b'Tr'):
            stack.clear()
            continue

        # Try to push as operand
        try:
            stack.append(int(tok))
            continue
        except (ValueError, TypeError):
            pass
        try:
            stack.append(float(tok))
            continue
        except (ValueError, TypeError):
            pass
        # it's a string or name token
        stack.append(tok)

    return ''.join(parts)


# ---------------------------------------------------------------------------
# Pure-Python extraction (digital PDFs only)
# ---------------------------------------------------------------------------

def _pdf_to_text_pure(pdf_path: str) -> str:
    """Extract text from a digital PDF using pure Python. Returns '' for image-only PDFs."""
    with open(pdf_path, 'rb') as f:
        data = f.read()

    if not data.startswith(b'%PDF'):
        return ''

    reader = _PDFReader(data)
    reader.prepare()

    pages = _get_pages(reader)
    if not pages:
        return ''

    page_texts = []
    for page_dict in pages:
        contents = page_dict.get('Contents')
        if contents is None:
            page_texts.append('')
            continue

        contents = _resolve(reader, contents)

        # Normalise to list of object numbers
        if _is_ref(contents):
            ref_list = [contents[0]]
        elif isinstance(contents, list):
            ref_list = []
            for item in contents:
                item = _resolve(reader, item)
                if isinstance(item, int):
                    ref_list.append(item)
                elif _is_ref(item):
                    ref_list.append(item[0])
        elif isinstance(contents, int):
            ref_list = [contents]
        else:
            ref_list = []

        page_stream = b''
        for obj_num in ref_list:
            _, stream_bytes = reader.get_obj_with_stream(obj_num)
            page_stream += stream_bytes + b'\n'

        page_texts.append(_extract_text_from_stream(page_stream))

    text = '\f'.join(page_texts)
    # Collapse excessive blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


# ---------------------------------------------------------------------------
# OCR fallback — requires pdftoppm (poppler) + tesseract installed as binaries
# ---------------------------------------------------------------------------

def _ocr_pdf(pdf_path: str) -> str:
    """Convert PDF pages to images via pdftoppm, then OCR with tesseract.

    Returns empty string if either binary is not installed.
    Install on Mac: brew install poppler tesseract
    Install on Ubuntu: sudo apt-get install poppler-utils tesseract-ocr
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        prefix = os.path.join(tmpdir, 'page')
        try:
            subprocess.run(
                ['pdftoppm', '-png', '-r', '300', pdf_path, prefix],
                check=True, capture_output=True,
            )
        except FileNotFoundError:
            return ''
        except subprocess.CalledProcessError:
            return ''

        pages = sorted(Path(tmpdir).glob('page-*.png'))
        if not pages:
            pages = sorted(Path(tmpdir).glob('page*.png'))

        texts = []
        for page_img in pages:
            try:
                result = subprocess.run(
                    ['tesseract', str(page_img), 'stdout', '--psm', '6'],
                    capture_output=True, text=True, check=True,
                )
                texts.append(result.stdout)
            except FileNotFoundError:
                return ''
            except subprocess.CalledProcessError:
                continue

        return '\n'.join(texts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def pdf_to_text(pdf_path: str) -> str:
    """Extract text from a PDF.

    Tries pure-Python extraction first (works for digital PDFs with a text
    layer). If that yields nothing, falls back to OCR via pdftoppm + tesseract
    for scanned image PDFs (requires those binaries to be installed).
    """
    text = _pdf_to_text_pure(pdf_path)
    if text.strip():
        return text
    return _ocr_pdf(pdf_path)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import sys
    import os

    test_pdf = '/root/.claude/uploads/3e5fe079-ff38-55ea-853a-bc14f5eda6ad/3053e793-MO.pdf'
    if not os.path.exists(test_pdf):
        print('Test PDF not found; skipping self-test.', file=sys.stderr)
        sys.exit(0)

    result = pdf_to_text(test_pdf)
    char_count = len(result.strip())
    print(f'Extracted {char_count} non-whitespace characters from scanned PDF.')
    if char_count < 100:
        print('PASS: near-empty result (image-only PDF detected correctly).')
    else:
        print('WARN: extracted text is non-empty — may not be image-only:')
        print(repr(result[:300]))
