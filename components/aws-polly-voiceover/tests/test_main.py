import io
from unittest import mock

import pytest
from botocore.exceptions import ClientError

import patchbae
from patchbae.testing import load_component

main = load_component(__file__)

PREFIX = f"voice-over/patchbae-{patchbae.task.id}"
POLLY_TASK = "7d4f2c1e-0b7a-4c55-9a3e-2f6a1b9c8d10"
AUDIO_KEY = f"{PREFIX}.{POLLY_TASK}.mp3"
TEXT = "Welcome to the show."
INPUTS = {
    "text": TEXT,
    "voice_id": "Joanna",
    "engine": "neural",
    "text_type": "text",
    "output_format": "mp3",
    "output_uri": "s3://work/voice-over/",
}


def client_error(code, message=None):
    return ClientError({"Error": {"Code": code, "Message": message or code}}, "op")


def task(status, **extra):
    return {"SynthesisTask": {"TaskId": POLLY_TASK, "TaskStatus": status, **extra}}


def completed(key=AUDIO_KEY, characters=len(TEXT)):
    return task(
        "completed",
        OutputUri=f"https://s3.eu-north-1.amazonaws.com/work/{key}",
        RequestCharacters=characters,
    )


def store(s3, content):
    """Puts a text file where the component reads it."""
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    s3.get_object.return_value = {"Body": io.BytesIO(data), "ContentLength": len(data)}


@pytest.fixture
def aws(monkeypatch):
    polly = mock.Mock()
    polly.start_speech_synthesis_task.return_value = task("scheduled")
    polly.get_speech_synthesis_task.side_effect = [task("inProgress"), completed()]
    s3 = mock.Mock()
    s3.list_objects_v2.return_value = {"KeyCount": 0}
    monkeypatch.setattr(main.boto3, "client", lambda name: {"polly": polly, "s3": s3}[name])
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)
    patchbae.reset()
    return polly, s3


def test_starts_a_task_and_returns_a_reference(aws):
    polly, s3 = aws

    output = main.run(INPUTS)

    s3.list_objects_v2.assert_called_once_with(Bucket="work", Prefix=f"{PREFIX}.", MaxKeys=10)
    polly.start_speech_synthesis_task.assert_called_once_with(
        OutputS3BucketName="work",
        OutputS3KeyPrefix=PREFIX,
        Text=TEXT,
        TextType="text",
        VoiceId="Joanna",
        Engine="neural",
        OutputFormat="mp3",
    )
    assert polly.get_speech_synthesis_task.call_args_list == [mock.call(TaskId=POLLY_TASK)] * 2
    assert output == {
        "audio_uri": f"s3://work/{AUDIO_KEY}",
        "characters": len(TEXT),
        "voice_id": "Joanna",
        "task_id": POLLY_TASK,
    }
    assert patchbae.emitted_assets == [
        {"label": "Voice-over", "locator": f"s3://work/{AUDIO_KEY}", "mediaType": "audio/mpeg"}
    ]
    s3.get_object.assert_not_called()


def test_a_redelivered_task_returns_the_audio_it_already_has(aws):
    polly, s3 = aws
    s3.list_objects_v2.return_value = {"KeyCount": 1, "Contents": [{"Key": AUDIO_KEY}]}
    polly.get_speech_synthesis_task.side_effect = [completed(characters=18)]

    output = main.run(INPUTS)

    polly.start_speech_synthesis_task.assert_not_called()
    polly.get_speech_synthesis_task.assert_called_once_with(TaskId=POLLY_TASK)
    assert output == {
        "audio_uri": f"s3://work/{AUDIO_KEY}",
        "characters": 18,
        "voice_id": "Joanna",
        "task_id": POLLY_TASK,
    }
    assert [asset["label"] for asset in patchbae.emitted_assets] == ["Voice-over"]


def test_audio_is_reused_even_when_polly_no_longer_keeps_the_task(aws):
    polly, s3 = aws
    s3.list_objects_v2.return_value = {"KeyCount": 1, "Contents": [{"Key": AUDIO_KEY}]}
    polly.get_speech_synthesis_task.side_effect = client_error("SynthesisTaskNotFoundException")

    output = main.run(INPUTS)

    polly.start_speech_synthesis_task.assert_not_called()
    assert output["audio_uri"] == f"s3://work/{AUDIO_KEY}"
    assert output["task_id"] == POLLY_TASK
    assert output["characters"] == len(TEXT)


def test_a_task_that_fails_fails_the_node_with_the_reason(aws):
    polly, _ = aws
    polly.get_speech_synthesis_task.side_effect = [
        task("scheduled"),
        task("failed", TaskStatusReason="The output bucket cannot be written to."),
    ]

    with pytest.raises(patchbae.ComponentFailed, match="could not synthesize the text: The output bucket cannot be written to"):
        main.run(INPUTS)

    assert patchbae.emitted_assets == []


def test_a_request_polly_turns_down_fails_the_node_with_the_reason(aws):
    polly, _ = aws
    polly.start_speech_synthesis_task.side_effect = client_error(
        "EngineNotSupportedException", "This voice does not support the selected engine: long-form"
    )

    with pytest.raises(patchbae.ComponentFailed, match="did not accept the request: This voice does not support"):
        main.run({**INPUTS, "engine": "long-form"})

    polly.get_speech_synthesis_task.assert_not_called()


def test_an_unexpected_aws_error_is_raised_as_it_is(aws):
    polly, _ = aws
    polly.start_speech_synthesis_task.side_effect = client_error("ServiceFailureException")

    with pytest.raises(ClientError):
        main.run(INPUTS)


def test_text_from_a_file_with_a_voice_that_speaks_two_languages(aws):
    polly, s3 = aws
    store(s3, "﻿Namaste, and welcome.\n")
    inputs = {key: value for key, value in INPUTS.items() if key != "text"}

    main.run({**inputs, "text_uri": "s3://scripts/in/intro.txt", "voice_id": "Aditi", "engine": "standard", "language_code": "en-IN"})

    s3.get_object.assert_called_once_with(Bucket="scripts", Key="in/intro.txt")
    request = polly.start_speech_synthesis_task.call_args.kwargs
    assert request["Text"] == "Namaste, and welcome.\n"
    assert request["TextType"] == "text"
    assert request["VoiceId"] == "Aditi"
    assert request["Engine"] == "standard"
    assert request["LanguageCode"] == "en-IN"


def test_an_ssml_file_is_sent_as_ssml(aws):
    polly, s3 = aws
    store(s3, '<speak>Hello <break time="1s"/> world.</speak>')

    main.run({"text_uri": "s3://scripts/in/intro.SSML", "output_uri": "s3://work/voice-over/"})

    request = polly.start_speech_synthesis_task.call_args.kwargs
    assert request["TextType"] == "ssml"
    assert request["Text"] == '<speak>Hello <break time="1s"/> world.</speak>'
    # Nothing but output_uri and the text was given: the rest are the defaults.
    assert (request["VoiceId"], request["Engine"], request["OutputFormat"]) == ("Joanna", "neural", "mp3")
    assert "LanguageCode" not in request


@pytest.mark.parametrize(
    "output_format, extension, media_type",
    [("mp3", "mp3", "audio/mpeg"), ("ogg_vorbis", "ogg", "audio/ogg"), ("pcm", "pcm", "audio/pcm")],
)
def test_each_audio_format_has_its_media_type(aws, output_format, extension, media_type):
    polly, _ = aws
    key = f"{PREFIX}.{POLLY_TASK}.{extension}"
    # No OutputUri in the answer: the key is built from what is known.
    polly.get_speech_synthesis_task.side_effect = [task("completed", RequestCharacters=20)]

    output = main.run({**INPUTS, "output_format": output_format})

    assert polly.start_speech_synthesis_task.call_args.kwargs["OutputFormat"] == output_format
    assert output["audio_uri"] == f"s3://work/{key}"
    assert patchbae.emitted_assets[0]["mediaType"] == media_type


@pytest.mark.parametrize(
    "output_uri",
    [
        f"https://s3.eu-north-1.amazonaws.com/work/{AUDIO_KEY}",
        f"https://work.s3.eu-north-1.amazonaws.com/{AUDIO_KEY}",
        f"https://s3.amazonaws.com/work/{AUDIO_KEY.replace('/', '%2F')}",
    ],
)
def test_the_key_is_read_from_the_url_polly_returns_whatever_the_host(output_uri):
    key = main.written_key({"TaskId": POLLY_TASK, "OutputUri": output_uri}, PREFIX, "mp3")

    assert key == AUDIO_KEY


def test_the_bucket_root_works_as_an_output_folder(aws):
    polly, s3 = aws
    key = f"patchbae-{patchbae.task.id}.{POLLY_TASK}.mp3"
    polly.get_speech_synthesis_task.side_effect = [completed(key=key)]

    output = main.run({**INPUTS, "output_uri": "s3://work"})

    assert polly.start_speech_synthesis_task.call_args.kwargs["OutputS3KeyPrefix"] == f"patchbae-{patchbae.task.id}"
    s3.list_objects_v2.assert_called_once_with(Bucket="work", Prefix=f"patchbae-{patchbae.task.id}.", MaxKeys=10)
    assert output["audio_uri"] == f"s3://work/{key}"


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"text_uri": "s3://scripts/in/intro.txt"}, "either text or text_uri, not both"),
        ({"text": ""}, "Set text .* or text_uri"),
        ({"text": None}, "Set text .* or text_uri"),
        ({"text": "  \n "}, "The text is empty"),
        ({"text": "x" * 200_001}, "has 200,001 characters. Polly takes at most 200,000"),
        ({"text": None, "text_uri": "https://example.com/intro.txt"}, "text_uri must be an S3 URI"),
        ({"text": None, "text_uri": "s3://scripts/in/intro.docx"}, "a .txt or .ssml file"),
        ({"output_uri": "work/voice-over/"}, "output_uri must be an S3 URI"),
        ({"output_uri": "s3://work/voice-over"}, "output_uri must be a folder"),
        ({"engine": "turbo"}, "engine must be one of neural, standard, long-form, generative"),
        ({"output_format": "wav"}, "output_format must be one of mp3, ogg_vorbis, pcm"),
        ({"text_type": "markdown"}, "text_type must be one of text, ssml"),
    ],
)
def test_bad_inputs_fail_before_anything_is_started(aws, changes, message):
    polly, s3 = aws

    with pytest.raises(patchbae.ComponentFailed, match=message):
        main.run({**INPUTS, **changes})

    polly.start_speech_synthesis_task.assert_not_called()
    s3.list_objects_v2.assert_not_called()
    s3.get_object.assert_not_called()


@pytest.mark.parametrize(
    "content, message",
    [
        ("", "The text is empty"),
        ("Smörgåsbord".encode("latin-1"), "not UTF-8 text"),
        ("x" * 200_001, "at most 200,000"),
    ],
)
def test_a_text_file_that_cannot_be_used_fails_before_anything_is_started(aws, content, message):
    polly, s3 = aws
    store(s3, content)

    with pytest.raises(patchbae.ComponentFailed, match=message):
        main.run({**INPUTS, "text": None, "text_uri": "s3://scripts/in/intro.txt"})

    polly.start_speech_synthesis_task.assert_not_called()


def test_a_text_file_that_is_not_there_or_too_large_fails_clearly(aws):
    polly, s3 = aws
    inputs = {**INPUTS, "text": None, "text_uri": "s3://scripts/in/intro.txt"}

    s3.get_object.side_effect = client_error("NoSuchKey")
    with pytest.raises(patchbae.ComponentFailed, match="no text file at s3://scripts/in/intro.txt"):
        main.run(inputs)

    s3.get_object.side_effect = None
    s3.get_object.return_value = {"Body": io.BytesIO(b""), "ContentLength": 800_001}
    with pytest.raises(patchbae.ComponentFailed, match="more than the 200,000 characters"):
        main.run(inputs)

    polly.start_speech_synthesis_task.assert_not_called()
