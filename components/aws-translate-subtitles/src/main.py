"""Amazon Translate Subtitles: an SRT or WebVTT file on S3 into other languages.

A subtitle file is small text, so the pod reads it, sends the text of the
cues to Amazon Translate and writes one translated file per language back to
S3. Cue numbers and timings are copied as they are; only the text changes.

To keep every cue apart, the text travels as HTML: one paragraph per cue,
each with an id. Translate keeps the markup, so each translation is read
back by its id and can never slide into the cue next to it.
"""

import html
import posixpath
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

import patchbae

MEDIA_TYPES = {"srt": "application/x-subrip", "vtt": "text/vtt"}
FORMALITY = {"none": None, "formal": "FORMAL", "informal": "INFORMAL"}
# TranslateDocument takes documents up to 100 KB. Stay under it.
MAX_DOCUMENT_BYTES = 90_000
MAX_SUBTITLE_BYTES = 10 * 1024 * 1024
DOCUMENT_HEAD = '<!DOCTYPE html>\n<html>\n<head><meta charset="utf-8"></head>\n<body>\n'
DOCUMENT_TAIL = "\n</body>\n</html>\n"
# A slow answer must not be cut off and asked for (and paid for) again.
TRANSLATE_CONFIG = Config(read_timeout=300, retries={"max_attempts": 5, "mode": "standard"})
# Problems with the request itself, which the workflow builder can fix.
REQUEST_ERRORS = {"UnsupportedLanguagePairException", "InvalidRequestException"}
LANGUAGE_CODE = re.compile(r"[A-Za-z-]{2,5}")
VTT_OTHER_BLOCKS = ("NOTE", "STYLE", "REGION")
HTML_SPACE = re.compile(r"[ \t\r\n\f]+")


def run(inputs):
    bucket, key = split_s3_uri(inputs.get("subtitle_uri"), "subtitle_uri")
    fmt = subtitle_format(key)
    out_bucket, out_prefix = split_s3_uri(inputs.get("output_uri"), "output_uri")
    if out_prefix and not out_prefix.endswith("/"):
        patchbae.fail("output_uri must be a folder: end it with a slash, for example s3://bucket/subtitles/")
    source = language_code(inputs.get("source_language") or "auto", "source_language")
    targets = target_languages(inputs.get("target_languages"), source)
    formality = formality_setting(inputs.get("formality"))

    s3 = boto3.client("s3")
    text = read_subtitles(s3, bucket, key)
    blocks = parse_vtt(text) if fmt == "vtt" else parse_srt(text)
    paragraphs = cue_paragraphs(blocks)
    if not paragraphs:
        patchbae.fail("The subtitle file has no cues with text, so there is nothing to translate.")
    documents = html_documents(list(paragraphs.values()))
    newline = "\r\n" if "\r\n" in text else "\n"

    # Translate every language before writing any file: a failure leaves nothing half done.
    translate = boto3.client("translate", config=TRANSLATE_CONFIG)
    files = {}
    for target in targets:
        texts = {}
        for document in documents:
            response = translate_document(translate, document, source, target, formality)
            # Once Translate has named the language, later requests use it: the whole file is one language.
            source = response.get("SourceLanguageCode") or source
            texts.update(cue_texts(response["TranslatedDocument"]["Content"]))
        missing = [index for index in paragraphs if not texts.get(index)]
        if missing:
            patchbae.fail(
                f"Amazon Translate returned no text for {len(missing)} of {len(paragraphs)} cues when translating "
                f"to {target} (the first is the cue at {blocks[missing[0]]['head'][-1].strip()}). "
                "Nothing was written. Run the node again; if it keeps failing, check that cue in the source file."
            )
        files[target] = render(blocks, texts, newline)
        patchbae.log("translated", language=target, cues=len(paragraphs), requests=len(documents))

    # The file names depend only on the inputs, so a second delivery overwrites the same files.
    name = posixpath.basename(key).rsplit(".", 1)[0]
    subtitle_uris = {}
    for target, content in files.items():
        out_key = f"{out_prefix}{name}.{target}.{fmt}"
        s3.put_object(Bucket=out_bucket, Key=out_key, Body=content.encode("utf-8"), ContentType=MEDIA_TYPES[fmt])
        subtitle_uris[target] = f"s3://{out_bucket}/{out_key}"
        patchbae.emit_asset(f"Subtitles {target.upper()}", subtitle_uris[target], MEDIA_TYPES[fmt])

    return {
        "subtitle_uris": subtitle_uris,
        "cue_count": sum(1 for block in blocks if block["cue"]),
        "source_language": source,
        "format": fmt,
    }


def read_subtitles(s3, bucket, key):
    """The subtitle file as text."""
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error_code(error) in ("NoSuchKey", "NoSuchBucket"):
            patchbae.fail(f"There is no subtitle file at s3://{bucket}/{key}.")
        raise
    if response.get("ContentLength", 0) > MAX_SUBTITLE_BYTES:
        patchbae.fail("The file at subtitle_uri is larger than 10 MB, which is not a subtitle file.")
    try:
        return response["Body"].read().decode("utf-8-sig")
    except UnicodeDecodeError:
        patchbae.fail("The subtitle file is not UTF-8 text. Save it as UTF-8 and run the node again.")


def split_blocks(text):
    """The lines of the file, grouped into blocks that blank lines keep apart."""
    blocks, current = [], []
    for line in re.split(r"\r\n|\r|\n", text):
        if line.strip():
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def timing_line(lines):
    """Where a block's timing line is: first, or second after a cue number or name. None for no cue."""
    for index, line in enumerate(lines[:2]):
        if "-->" in line:
            return index
    return None


def cue(lines, timing):
    """A cue: the lines up to its timing are kept as they are, the rest is text to translate."""
    return {"cue": True, "head": lines[: timing + 1], "text": lines[timing + 1 :]}


def parse_srt(text):
    """An SRT file as a list of cues."""
    blocks = []
    for lines in split_blocks(text):
        timing = timing_line(lines)
        if timing is not None:
            blocks.append(cue(lines, timing))
        elif blocks:
            # A blank line inside a cue's text: these lines still belong to the cue before them.
            blocks[-1]["text"] += lines
        else:
            patchbae.fail(
                "The file does not look like SRT: it must start with a cue number and a timing line "
                "such as 00:00:01,000 --> 00:00:04,000."
            )
    return blocks


def parse_vtt(text):
    """A WebVTT file as a list of blocks: the header, notes and styles (copied as they are) and cues."""
    groups = split_blocks(text)
    if not groups or not groups[0][0].startswith("WEBVTT"):
        patchbae.fail("The file does not look like WebVTT: its first line must be WEBVTT.")
    blocks = [{"cue": False, "head": groups[0], "text": []}]
    for lines in groups[1:]:
        timing = timing_line(lines)
        if timing is None or lines[0].startswith(VTT_OTHER_BLOCKS):
            blocks.append({"cue": False, "head": lines, "text": []})
        else:
            blocks.append(cue(lines, timing))
    return blocks


def cue_paragraphs(blocks):
    """One HTML paragraph per cue that has text, keyed by the cue's place in the file."""
    paragraphs = {}
    for index, block in enumerate(blocks):
        lines = [html.escape(line.strip(), quote=False) for line in block["text"] if line.strip()]
        if block["cue"] and lines:
            paragraphs[index] = f'<p id="c{index}">' + "<br>".join(lines) + "</p>"
    return paragraphs


def html_documents(paragraphs):
    """The paragraphs packed into as few HTML documents as fit under the size Translate takes."""
    wrapper = len(DOCUMENT_HEAD.encode("utf-8")) + len(DOCUMENT_TAIL.encode("utf-8"))
    documents, current, size = [], [], wrapper
    for paragraph in paragraphs:
        length = len(paragraph.encode("utf-8")) + 1
        if wrapper + length > MAX_DOCUMENT_BYTES:
            patchbae.fail("One cue holds more text than Amazon Translate takes in a request. Split that cue.")
        if current and size + length > MAX_DOCUMENT_BYTES:
            documents.append(html_document(current))
            current, size = [], wrapper
        current.append(paragraph)
        size += length
    if current:
        documents.append(html_document(current))
    return documents


def html_document(paragraphs):
    return (DOCUMENT_HEAD + "\n".join(paragraphs) + DOCUMENT_TAIL).encode("utf-8")


def translate_document(translate, document, source, target, formality):
    """One TranslateDocument request. Fails the node when Translate turns the request down."""
    request = {
        "Document": {"Content": document, "ContentType": "text/html"},
        "SourceLanguageCode": source,
        "TargetLanguageCode": target,
    }
    if formality:
        request["Settings"] = {"Formality": formality}
    try:
        return translate.translate_document(**request)
    except ClientError as error:
        if error_code(error) in REQUEST_ERRORS:
            patchbae.fail(f"Amazon Translate could not translate from {source} to {target}: {error_message(error)}")
        raise


class CueTexts(HTMLParser):
    """Collects the lines of text in every <p id="c…"> of a translated document."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cues = {}
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "p":
            match = re.fullmatch(r"c(\d+)", dict(attrs).get("id") or "")
            self.current = int(match.group(1)) if match else None
            if match:
                self.cues[self.current] = [""]
        elif tag == "br" and self.current is not None:
            self.cues[self.current].append("")

    def handle_endtag(self, tag):
        if tag == "p":
            self.current = None

    def handle_data(self, data):
        if self.current is not None:
            self.cues[self.current][-1] += data


def cue_texts(document):
    """The translated lines of each cue in a document, keyed like cue_paragraphs()."""
    parser = CueTexts()
    parser.feed(document.decode("utf-8"))
    parser.close()
    texts = {}
    for index, lines in parser.cues.items():
        cleaned = [HTML_SPACE.sub(" ", line).strip() for line in lines]
        texts[index] = [line for line in cleaned if line]
    return texts


def render(blocks, texts, newline):
    """The subtitle file again, with the text of each translated cue replaced."""
    parts = [newline.join(block["head"] + texts.get(index, block["text"])) for index, block in enumerate(blocks)]
    return (newline * 2).join(parts) + newline


def subtitle_format(key):
    name = posixpath.basename(key)
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension not in MEDIA_TYPES:
        patchbae.fail("subtitle_uri must point to an .srt or .vtt file.")
    return extension


def target_languages(value, source):
    """The languages to translate to, in the order given, each once."""
    items = value.split(",") if isinstance(value, str) else (value or [])
    codes = []
    for item in items:
        code = str(item).strip()
        if code and code not in codes:
            codes.append(language_code(code, "target_languages"))
    if not codes:
        patchbae.fail("target_languages is empty: list at least one language code, such as sv, de or fr.")
    for code in codes:
        if code.lower() == "auto":
            patchbae.fail("target_languages cannot hold auto: name the languages to translate to.")
        if code.lower() == source.lower():
            patchbae.fail(f"target_languages holds {code}, the source language. Remove it or change source_language.")
    return codes


def language_code(value, name):
    code = str(value).strip()
    if not LANGUAGE_CODE.fullmatch(code):
        patchbae.fail(f"{name} has {code!r}, which is not a language code. Use codes such as sv, de, fr or zh-TW.")
    return code


def formality_setting(value):
    name = str(value or "none").strip().lower()
    if name not in FORMALITY:
        patchbae.fail(f"formality must be none, formal or informal, got {value!r}.")
    return FORMALITY[name]


def split_s3_uri(uri, name):
    parsed = urlparse(str(uri or ""))
    if parsed.scheme != "s3" or not parsed.netloc:
        patchbae.fail(f"{name} must be an S3 URI such as s3://bucket/path, got {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def error_code(error):
    return error.response.get("Error", {}).get("Code", "")


def error_message(error):
    return error.response.get("Error", {}).get("Message", "") or error_code(error)
