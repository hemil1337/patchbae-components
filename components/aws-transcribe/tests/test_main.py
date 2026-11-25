import io
import json
from unittest import mock

import pytest
from botocore.exceptions import ClientError

import patchbae
from patchbae.testing import load_component

main = load_component(__file__)

JOB = f"patchbae-{patchbae.task.id}"
INPUTS = {
    "media_uri": "s3://media/in/show.mp4",
    "output_uri": "s3://work/transcripts/",
    "language_code": "auto",
    "subtitle_formats": ["srt", "vtt"],
    "include_text": True,
}


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


def job(status, **extra):
    return {"TranscriptionJob": {"TranscriptionJobName": JOB, "TranscriptionJobStatus": status, **extra}}


@pytest.fixture
def aws(monkeypatch):
    transcribe = mock.Mock()
    s3 = mock.Mock()
    s3.get_object.return_value = {
        "Body": io.BytesIO(json.dumps({"results": {"transcripts": [{"transcript": "Hello world."}]}}).encode())
    }
    monkeypatch.setattr(main.boto3, "client", lambda name: {"transcribe": transcribe, "s3": s3}[name])
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)
    patchbae.reset()
    return transcribe, s3


def test_starts_a_job_and_returns_references(aws):
    transcribe, s3 = aws
    transcribe.get_transcription_job.side_effect = [
        client_error("BadRequestException"),
        job("IN_PROGRESS"),
        job("COMPLETED", LanguageCode="en-US"),
    ]

    output = main.run(INPUTS)

    request = transcribe.start_transcription_job.call_args.kwargs
    assert request["TranscriptionJobName"] == JOB
    assert request["Media"] == {"MediaFileUri": "s3://media/in/show.mp4"}
    assert request["OutputBucketName"] == "work"
    assert request["OutputKey"] == "transcripts/"
    assert request["IdentifyLanguage"] is True
    assert request["Subtitles"] == {"Formats": ["srt", "vtt"], "OutputStartIndex": 1}
    assert output == {
        "transcript_uri": f"s3://work/transcripts/{JOB}.json",
        "subtitle_uris": {
            "srt": f"s3://work/transcripts/{JOB}.srt",
            "vtt": f"s3://work/transcripts/{JOB}.vtt",
        },
        "language_code": "en-US",
        "job_name": JOB,
        "text": "Hello world.",
        "text_truncated": False,
    }
    s3.get_object.assert_called_once_with(Bucket="work", Key=f"transcripts/{JOB}.json")
    assert [asset["label"] for asset in patchbae.emitted_assets] == ["Transcript", "Subtitles SRT", "Subtitles VTT"]


def test_a_redelivered_task_waits_for_its_job_instead_of_starting_another(aws):
    transcribe, _ = aws
    transcribe.get_transcription_job.side_effect = [job("IN_PROGRESS"), job("COMPLETED", LanguageCode="sv-SE")]

    output = main.run(INPUTS)

    transcribe.start_transcription_job.assert_not_called()
    assert output["language_code"] == "sv-SE"


def test_a_failed_job_from_an_earlier_attempt_is_replaced(aws):
    transcribe, _ = aws
    transcribe.get_transcription_job.side_effect = [job("FAILED"), job("COMPLETED", LanguageCode="en-US")]

    main.run(INPUTS)

    transcribe.delete_transcription_job.assert_called_once_with(TranscriptionJobName=JOB)
    transcribe.start_transcription_job.assert_called_once()


def test_a_job_that_fails_fails_the_node_with_the_reason(aws):
    transcribe, _ = aws
    transcribe.get_transcription_job.side_effect = [
        client_error("BadRequestException"),
        job("FAILED", FailureReason="The media format is not supported."),
    ]

    with pytest.raises(patchbae.ComponentFailed, match="media format is not supported"):
        main.run(INPUTS)


def test_fixed_language_speakers_and_vocabulary(aws):
    request = main.start_request(
        {"language_code": "sv-SE", "vocabulary_name": "names", "speaker_labels": True, "max_speakers": 6},
        JOB,
        "s3://media/in/show.mp4",
        "work",
        "",
        [],
    )

    assert request["LanguageCode"] == "sv-SE"
    assert "IdentifyLanguage" not in request
    assert "OutputKey" not in request
    assert "Subtitles" not in request
    assert request["Settings"] == {"VocabularyName": "names", "ShowSpeakerLabels": True, "MaxSpeakerLabels": 6}


@pytest.mark.parametrize(
    "inputs, message",
    [
        ({**INPUTS, "media_uri": "https://example.com/a.mp4"}, "media_uri must be an S3 URI"),
        ({**INPUTS, "output_uri": "s3://work/transcripts"}, "output_uri must be a folder"),
        ({**INPUTS, "subtitle_formats": ["ttml"]}, "Unknown subtitle format"),
        ({**INPUTS, "vocabulary_name": "names"}, "custom vocabulary belongs to one language"),
        ({**INPUTS, "language_options": ["en-US"]}, "two or more languages"),
        ({**INPUTS, "language_code": "en-US", "speaker_labels": True, "max_speakers": 1}, "between 2 and 30"),
    ],
)
def test_bad_inputs_fail_before_anything_is_started(aws, inputs, message):
    transcribe, _ = aws
    transcribe.get_transcription_job.side_effect = client_error("BadRequestException")

    with pytest.raises(patchbae.ComponentFailed, match=message):
        main.run(inputs)

    transcribe.start_transcription_job.assert_not_called()


def test_text_is_left_out_when_not_asked_for(aws):
    transcribe, s3 = aws
    transcribe.get_transcription_job.side_effect = [client_error("BadRequestException"), job("COMPLETED")]

    output = main.run({**INPUTS, "include_text": False})

    assert "text" not in output
    s3.get_object.assert_not_called()
