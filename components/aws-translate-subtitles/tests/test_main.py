import io
import re
from unittest import mock

import pytest
from botocore.exceptions import ClientError

import patchbae
from patchbae.testing import load_component

main = load_component(__file__)

INPUTS = {
    "subtitle_uri": "s3://media/subs/show.srt",
    "target_languages": ["sv"],
    "source_language": "auto",
    "output_uri": "s3://work/subtitles/",
    "formality": "none",
}

SRT = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "Hello & welcome.\n"
    "\n"
    "2\n"
    "00:00:05,000 --> 00:00:08,500\n"
    "- Is 2 < 3?\n"
    '- <i>Yes</i>, "always".\n'
    "\n"
    "3\n"
    "00:00:09,000 --> 00:00:11,000\n"
    "Smörgåsbord ♪\n"
)
SRT_DOCUMENT = (
    "<!DOCTYPE html>\n"
    "<html>\n"
    '<head><meta charset="utf-8"></head>\n'
    "<body>\n"
    '<p id="c0">Hello &amp; welcome.</p>\n'
    '<p id="c1">- Is 2 &lt; 3?<br>- &lt;i&gt;Yes&lt;/i&gt;, "always".</p>\n'
    '<p id="c2">Smörgåsbord ♪</p>\n'
    "</body>\n"
    "</html>\n"
)
SRT_IN_SWEDISH = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "[sv] Hello & welcome.\n"
    "\n"
    "2\n"
    "00:00:05,000 --> 00:00:08,500\n"
    "[sv] - Is 2 < 3?\n"
    '[sv] - <i>Yes</i>, "always".\n'
    "\n"
    "3\n"
    "00:00:09,000 --> 00:00:11,000\n"
    "[sv] Smörgåsbord ♪\n"
)

VTT = (
    "WEBVTT - Show\n"
    "Kind: captions\n"
    "\n"
    "NOTE\n"
    "This file was made by hand.\n"
    "\n"
    "STYLE\n"
    "::cue { color: yellow }\n"
    "\n"
    "intro\n"
    "00:00:01.000 --> 00:00:04.000 align:start position:10%\n"
    "Hello & welcome.\n"
    "\n"
    "00:00:05.000 --> 00:00:08.500\n"
    "<v Anna>Is 2 &lt; 3?\n"
    "Yes.\n"
)
VTT_IN_SWEDISH = (
    "WEBVTT - Show\n"
    "Kind: captions\n"
    "\n"
    "NOTE\n"
    "This file was made by hand.\n"
    "\n"
    "STYLE\n"
    "::cue { color: yellow }\n"
    "\n"
    "intro\n"
    "00:00:01.000 --> 00:00:04.000 align:start position:10%\n"
    "[sv] Hello & welcome.\n"
    "\n"
    "00:00:05.000 --> 00:00:08.500\n"
    "[sv] <v Anna>Is 2 &lt; 3?\n"
    "[sv] Yes.\n"
)


def client_error(code, message=None):
    return ClientError({"Error": {"Code": code, "Message": message or code}}, "op")


def translator(mark=True, detected="en", drop=None):
    """Stands in for Translate: keeps the markup and puts the target language in front of every piece of text."""

    def translate_document(**request):
        target = request["TargetLanguageCode"]
        document = request["Document"]["Content"].decode("utf-8")
        if mark:
            document = re.sub(r">([^<>]*[^<>\s][^<>]*)<", lambda match: f">[{target}] {match.group(1)}<", document)
        if drop:
            document = re.sub(rf'<p id="{drop}">.*?</p>', "", document)
        source = request["SourceLanguageCode"]
        return {
            "TranslatedDocument": {"Content": document.encode("utf-8")},
            "SourceLanguageCode": detected if source == "auto" else source,
            "TargetLanguageCode": target,
        }

    return translate_document


def store(s3, content):
    """Puts a subtitle file where the component reads it."""
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    s3.get_object.side_effect = lambda **_request: {"Body": io.BytesIO(data), "ContentLength": len(data)}


def written(s3):
    """What the component wrote to S3: key to text."""
    return {call.kwargs["Key"]: call.kwargs["Body"].decode("utf-8") for call in s3.put_object.call_args_list}


@pytest.fixture
def aws(monkeypatch):
    translate = mock.Mock()
    translate.translate_document.side_effect = translator()
    s3 = mock.Mock()
    store(s3, SRT)
    monkeypatch.setattr(main.boto3, "client", lambda name, **_config: {"translate": translate, "s3": s3}[name])
    patchbae.reset()
    return translate, s3


def test_translates_an_srt_file_and_returns_references(aws):
    translate, s3 = aws

    output = main.run(INPUTS)

    s3.get_object.assert_called_once_with(Bucket="media", Key="subs/show.srt")
    translate.translate_document.assert_called_once_with(
        Document={"Content": SRT_DOCUMENT.encode("utf-8"), "ContentType": "text/html"},
        SourceLanguageCode="auto",
        TargetLanguageCode="sv",
    )
    s3.put_object.assert_called_once_with(
        Bucket="work",
        Key="subtitles/show.sv.srt",
        Body=SRT_IN_SWEDISH.encode("utf-8"),
        ContentType="application/x-subrip",
    )
    assert output == {
        "subtitle_uris": {"sv": "s3://work/subtitles/show.sv.srt"},
        "cue_count": 3,
        "source_language": "en",
        "format": "srt",
    }
    assert patchbae.emitted_assets == [
        {"label": "Subtitles SV", "locator": "s3://work/subtitles/show.sv.srt", "mediaType": "application/x-subrip"}
    ]


def test_writes_one_file_per_language(aws):
    translate, s3 = aws

    output = main.run({**INPUTS, "target_languages": ["sv", "de", "sv", "zh-TW"]})

    requests = [call.kwargs for call in translate.translate_document.call_args_list]
    assert [request["TargetLanguageCode"] for request in requests] == ["sv", "de", "zh-TW"]
    # The language is identified once; later requests name it.
    assert [request["SourceLanguageCode"] for request in requests] == ["auto", "en", "en"]
    assert output["subtitle_uris"] == {
        "sv": "s3://work/subtitles/show.sv.srt",
        "de": "s3://work/subtitles/show.de.srt",
        "zh-TW": "s3://work/subtitles/show.zh-TW.srt",
    }
    assert "[de] Hello & welcome." in written(s3)["subtitles/show.de.srt"]
    assert [asset["label"] for asset in patchbae.emitted_assets] == ["Subtitles SV", "Subtitles DE", "Subtitles ZH-TW"]


def test_a_vtt_file_keeps_its_header_notes_styles_and_cue_settings(aws):
    translate, s3 = aws
    store(s3, VTT)

    output = main.run({**INPUTS, "subtitle_uri": "s3://media/subs/show.en.vtt", "source_language": "en"})

    request = translate.translate_document.call_args.kwargs
    assert request["SourceLanguageCode"] == "en"
    # The header, the note and the style are blocks 0 to 2 and are not sent.
    assert re.findall(r"<p .*?</p>", request["Document"]["Content"].decode("utf-8")) == [
        '<p id="c3">Hello &amp; welcome.</p>',
        '<p id="c4">&lt;v Anna&gt;Is 2 &amp;lt; 3?<br>Yes.</p>',
    ]
    s3.put_object.assert_called_once_with(
        Bucket="work",
        Key="subtitles/show.en.sv.vtt",
        Body=VTT_IN_SWEDISH.encode("utf-8"),
        ContentType="text/vtt",
    )
    assert output == {
        "subtitle_uris": {"sv": "s3://work/subtitles/show.en.sv.vtt"},
        "cue_count": 2,
        "source_language": "en",
        "format": "vtt",
    }
    assert patchbae.emitted_assets[0]["mediaType"] == "text/vtt"


@pytest.mark.parametrize(
    "key, content",
    [
        ("subs/show.srt", SRT),
        ("subs/show.vtt", VTT),
        ("subs/show.SRT", SRT.replace("\n", "\r\n")),
        ("subs/show.srt", "﻿" + SRT),
        ("subs/show.srt", "7\n00:00:01,000 --> 00:00:02,000\n😀 a b &amp; &#39; <b>c</b> 100% {\\an8}\n"),
        ("subs/show.srt", "00:00:01,000 --> 00:00:02,000\nA cue without a number.\n\n9\n00:00:03,000 --> 00:00:04,000\n"),
    ],
)
def test_text_that_comes_back_unchanged_gives_the_same_file(aws, key, content):
    translate, s3 = aws
    translate.translate_document.side_effect = translator(mark=False)
    store(s3, content)

    main.run({**INPUTS, "subtitle_uri": f"s3://media/{key}"})

    (body,) = written(s3).values()
    assert body == content.lstrip("﻿")


def test_a_long_file_is_split_into_several_documents(aws, monkeypatch):
    translate, s3 = aws
    cues = [f"{n}\n00:00:{n:02d},000 --> 00:00:{n:02d},900\nLine number {n} of the film.\n" for n in range(1, 13)]
    store(s3, "\n".join(cues))
    monkeypatch.setattr(main, "MAX_DOCUMENT_BYTES", 400)

    output = main.run(INPUTS)

    requests = [call.kwargs for call in translate.translate_document.call_args_list]
    documents = [request["Document"]["Content"] for request in requests]
    assert len(documents) > 1
    for document in documents:
        assert len(document) <= 400
        assert document.startswith(b"<!DOCTYPE html>") and document.endswith(b"</html>\n")
    # Every cue is sent exactly once, in order.
    sent = [int(n) for document in documents for n in re.findall(rb'<p id="c(\d+)">', document)]
    assert sent == list(range(12))
    assert [request["SourceLanguageCode"] for request in requests] == ["auto"] + ["en"] * (len(documents) - 1)
    (body,) = written(s3).values()
    assert body == "\n".join(cue.replace("Line", "[sv] Line") for cue in cues)
    assert output["cue_count"] == 12


def test_formality_is_sent_only_when_asked_for(aws):
    translate, _ = aws

    main.run({**INPUTS, "formality": "formal"})
    main.run({**INPUTS, "formality": "informal"})
    main.run({**INPUTS, "formality": "none"})

    first, second, third = [call.kwargs for call in translate.translate_document.call_args_list]
    assert first["Settings"] == {"Formality": "FORMAL"}
    assert second["Settings"] == {"Formality": "INFORMAL"}
    assert "Settings" not in third


def test_a_redelivered_task_overwrites_the_same_files(aws):
    _, s3 = aws

    first = main.run({**INPUTS, "target_languages": ["sv", "de"]})
    second = main.run({**INPUTS, "target_languages": ["sv", "de"]})

    assert first == second
    keys = [call.kwargs["Key"] for call in s3.put_object.call_args_list]
    assert keys == ["subtitles/show.sv.srt", "subtitles/show.de.srt"] * 2


def test_a_cue_missing_from_the_translation_fails_the_node(aws):
    translate, s3 = aws
    translate.translate_document.side_effect = translator(drop="c1")

    with pytest.raises(patchbae.ComponentFailed, match=r"no text for 1 of 3 cues .* to sv .*00:00:05,000 --> 00:00:08,500"):
        main.run(INPUTS)

    s3.put_object.assert_not_called()
    assert patchbae.emitted_assets == []


def test_a_request_translate_turns_down_fails_the_node_with_the_reason(aws):
    translate, s3 = aws
    translate.translate_document.side_effect = client_error(
        "UnsupportedLanguagePairException", "Translation from en to xx is not supported."
    )

    with pytest.raises(patchbae.ComponentFailed, match="from auto to xx: Translation from en to xx is not supported"):
        main.run({**INPUTS, "target_languages": ["xx"]})

    s3.put_object.assert_not_called()


def test_a_failure_in_a_later_language_writes_nothing(aws):
    translate, s3 = aws
    good = translator()

    def translate_document(**request):
        if request["TargetLanguageCode"] == "de":
            raise client_error("InvalidRequestException", "Unsupported target language.")
        return good(**request)

    translate.translate_document.side_effect = translate_document

    with pytest.raises(patchbae.ComponentFailed, match="from en to de: Unsupported target language"):
        main.run({**INPUTS, "target_languages": ["sv", "de"]})

    s3.put_object.assert_not_called()


def test_an_unexpected_aws_error_is_raised_as_it_is(aws):
    translate, _ = aws
    translate.translate_document.side_effect = client_error("InternalServerException")

    with pytest.raises(ClientError):
        main.run(INPUTS)


@pytest.mark.parametrize(
    "inputs, message",
    [
        ({**INPUTS, "subtitle_uri": "https://example.com/show.srt"}, "subtitle_uri must be an S3 URI"),
        ({**INPUTS, "subtitle_uri": "s3://media/subs/show.txt"}, "an .srt or .vtt file"),
        ({**INPUTS, "subtitle_uri": "s3://media/subs/show"}, "an .srt or .vtt file"),
        ({**INPUTS, "output_uri": "work/subtitles/"}, "output_uri must be an S3 URI"),
        ({**INPUTS, "output_uri": "s3://work/subtitles"}, "output_uri must be a folder"),
        ({**INPUTS, "target_languages": []}, "target_languages is empty"),
        ({**INPUTS, "target_languages": None}, "target_languages is empty"),
        ({**INPUTS, "target_languages": ["swedish"]}, "target_languages has 'swedish', which is not a language code"),
        ({**INPUTS, "target_languages": ["auto"]}, "target_languages cannot hold auto"),
        ({**INPUTS, "source_language": "en", "target_languages": ["sv", "EN"]}, "holds EN, the source language"),
        ({**INPUTS, "source_language": "english"}, "source_language has 'english'"),
        ({**INPUTS, "formality": "polite"}, "formality must be none, formal or informal"),
    ],
)
def test_bad_inputs_fail_before_anything_is_read_or_translated(aws, inputs, message):
    translate, s3 = aws

    with pytest.raises(patchbae.ComponentFailed, match=message):
        main.run(inputs)

    s3.get_object.assert_not_called()
    translate.translate_document.assert_not_called()
    s3.put_object.assert_not_called()


@pytest.mark.parametrize(
    "key, content, message",
    [
        ("subs/show.srt", "Smörgåsbord".encode("latin-1"), "not UTF-8 text"),
        ("subs/show.srt", "", "no cues with text"),
        ("subs/show.srt", "1\n00:00:01,000 --> 00:00:02,000\n", "no cues with text"),
        ("subs/show.srt", "Just some notes.\n", "does not look like SRT"),
        ("subs/show.vtt", SRT, "first line must be WEBVTT"),
        ("subs/show.vtt", "WEBVTT\n\nNOTE nothing else\n", "no cues with text"),
    ],
)
def test_a_file_that_cannot_be_used_fails_before_anything_is_translated(aws, key, content, message):
    translate, s3 = aws
    store(s3, content)

    with pytest.raises(patchbae.ComponentFailed, match=message):
        main.run({**INPUTS, "subtitle_uri": f"s3://media/{key}"})

    translate.translate_document.assert_not_called()
    s3.put_object.assert_not_called()


def test_a_file_that_is_not_there_or_too_large_fails_clearly(aws):
    translate, s3 = aws
    s3.get_object.side_effect = client_error("NoSuchKey")
    with pytest.raises(patchbae.ComponentFailed, match="no subtitle file at s3://media/subs/show.srt"):
        main.run(INPUTS)

    s3.get_object.side_effect = lambda **_request: {"Body": io.BytesIO(b""), "ContentLength": 11 * 1024 * 1024}
    with pytest.raises(patchbae.ComponentFailed, match="larger than 10 MB"):
        main.run(INPUTS)

    s3.get_object.side_effect = client_error("AccessDenied")
    with pytest.raises(ClientError):
        main.run(INPUTS)
    translate.translate_document.assert_not_called()


def test_a_blank_line_inside_a_cue_stays_with_that_cue():
    blocks = main.parse_srt("1\n00:00:01,000 --> 00:00:02,000\nFirst line\n\nsecond line\n\n2\n00:00:03,000 --> 00:00:04,000\nNext\n")

    assert [block["text"] for block in blocks] == [["First line", "second line"], ["Next"]]
    assert main.cue_paragraphs(blocks) == {0: '<p id="c0">First line<br>second line</p>', 1: '<p id="c1">Next</p>'}


def test_translated_html_is_read_back_by_cue_id():
    document = (
        "<html><head><title>ignored</title></head><body>\n"
        '<p id="c7">\n  Tom &amp; Jerry &lt;3<br/>second&nbsp;line<BR>  <span lang="sv">third</span> line\n</p>\n'
        "<p>not a cue</p>\n"
        '<P ID="c12">caf&eacute; &#9834;</P>\n'
        '<p id="c13"> <br> </p>\n'
        "</body></html>"
    ).encode("utf-8")

    assert main.cue_texts(document) == {
        7: ["Tom & Jerry <3", "second line", "third line"],
        12: ["café ♪"],
        13: [],
    }


def test_a_cue_too_large_for_one_request_fails(monkeypatch):
    monkeypatch.setattr(main, "MAX_DOCUMENT_BYTES", 200)

    with pytest.raises(patchbae.ComponentFailed, match="Split that cue"):
        main.html_documents(['<p id="c0">' + "x" * 200 + "</p>"])
