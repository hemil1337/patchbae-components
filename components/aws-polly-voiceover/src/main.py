"""Amazon Polly Voice-over: text to a spoken audio file on S3.

The pod hands the text to Polly's asynchronous API, which works for long
texts and writes the audio file to S3 itself. The pod starts the task, waits
for it and returns a reference to the file.
"""

import posixpath
import random
import time
from urllib.parse import unquote, urlparse

import boto3
from botocore.exceptions import ClientError

import patchbae

MAX_CHARACTERS = 200_000
ENGINES = ("neural", "standard", "long-form", "generative")
TEXT_TYPES = ("text", "ssml")
# output_format: (file extension Polly uses, media type)
AUDIO = {
    "mp3": ("mp3", "audio/mpeg"),
    "ogg_vorbis": ("ogg", "audio/ogg"),
    "pcm": ("pcm", "audio/pcm"),
}
POLL_START_SECONDS = 5
POLL_MAX_SECONDS = 30
# Problems with the request itself, which the workflow builder can fix.
REQUEST_ERRORS = {
    "EngineNotSupportedException",
    "InvalidS3BucketException",
    "InvalidS3KeyException",
    "InvalidSsmlException",
    "LanguageNotSupportedException",
    "TextLengthExceededException",
    "ValidationException",
}
TASK_GONE_CODES = {"SynthesisTaskNotFoundException", "InvalidTaskIdException"}


def run(inputs):
    out_bucket, out_prefix = split_s3_uri(inputs.get("output_uri"), "output_uri")
    if out_prefix and not out_prefix.endswith("/"):
        patchbae.fail("output_uri must be a folder: end it with a slash, for example s3://bucket/voice-over/")
    engine = one_of(inputs.get("engine") or "neural", ENGINES, "engine")
    output_format = one_of(inputs.get("output_format") or "mp3", tuple(AUDIO), "output_format")
    text_type = one_of(inputs.get("text_type") or "text", TEXT_TYPES, "text_type")
    voice_id = str(inputs.get("voice_id") or "Joanna").strip()
    language_code = str(inputs.get("language_code") or "").strip()

    s3 = boto3.client("s3")
    text, text_type = spoken_text(inputs, s3, text_type)
    extension, media_type = AUDIO[output_format]

    polly = boto3.client("polly")
    # Polly names the file <this prefix>.<its task id>.<extension>. The prefix holds the Patchbae
    # task, so a redelivered task finds the file it already has instead of paying for another.
    key_prefix = f"{out_prefix}patchbae-{patchbae.task.id}"

    audio_key = find_audio(s3, out_bucket, key_prefix)
    if audio_key:
        polly_task_id = audio_key[len(key_prefix) + 1 :].rsplit(".", 1)[0]
        task = find_task(polly, polly_task_id) or {}
        characters = task.get("RequestCharacters", len(text))
        patchbae.log("found the audio from an earlier delivery", audio=f"s3://{out_bucket}/{audio_key}")
    else:
        request = start_request(text, text_type, voice_id, engine, output_format, language_code, out_bucket, key_prefix)
        task = start(polly, request)
        polly_task_id = task["TaskId"]
        patchbae.log("speech synthesis started", task=polly_task_id, voice=voice_id, engine=engine)
        task = wait_for(polly, polly_task_id)
        if task["TaskStatus"] == "failed":
            patchbae.fail(f"Polly could not synthesize the text: {task.get('TaskStatusReason') or 'no reason given'}")
        audio_key = written_key(task, key_prefix, extension)
        characters = task.get("RequestCharacters", len(text))

    audio_uri = f"s3://{out_bucket}/{audio_key}"
    patchbae.emit_asset("Voice-over", audio_uri, media_type)
    patchbae.log("speech synthesis finished", task=polly_task_id, characters=characters)
    return {
        "audio_uri": audio_uri,
        "characters": characters,
        "voice_id": voice_id,
        "task_id": polly_task_id,
    }


def spoken_text(inputs, s3, text_type):
    """The words to speak and whether they are SSML, from the text input or the file at text_uri."""
    text = inputs.get("text")
    text_uri = str(inputs.get("text_uri") or "").strip()
    has_text = text not in (None, "")
    if has_text and text_uri:
        patchbae.fail("Set either text or text_uri, not both.")
    if not has_text and not text_uri:
        patchbae.fail("Set text (the words to speak) or text_uri (a .txt or .ssml file on S3).")

    if text_uri:
        bucket, key = split_s3_uri(text_uri, "text_uri")
        extension = posixpath.splitext(key)[1].lower()
        if extension not in (".txt", ".ssml"):
            patchbae.fail("text_uri must point to a .txt or .ssml file.")
        text = read_text(s3, bucket, key)
        if extension == ".ssml":
            text_type = "ssml"

    text = str(text)
    if not text.strip():
        patchbae.fail("The text is empty: there is nothing to speak.")
    if len(text) > MAX_CHARACTERS:
        patchbae.fail(
            f"The text has {len(text):,} characters. Polly takes at most {MAX_CHARACTERS:,} in one task: "
            "split it over several nodes."
        )
    return text, text_type


def read_text(s3, bucket, key):
    """The text file at text_uri."""
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error_code(error) in ("NoSuchKey", "NoSuchBucket"):
            patchbae.fail(f"There is no text file at s3://{bucket}/{key}.")
        raise
    # A character is at most 4 bytes in UTF-8: anything larger is over the limit without reading it.
    if response.get("ContentLength", 0) > MAX_CHARACTERS * 4:
        patchbae.fail(f"The file at text_uri holds more than the {MAX_CHARACTERS:,} characters Polly takes in one task.")
    try:
        return response["Body"].read().decode("utf-8-sig")
    except UnicodeDecodeError:
        patchbae.fail("The file at text_uri is not UTF-8 text. Save it as UTF-8 and run the node again.")


def start_request(text, text_type, voice_id, engine, output_format, language_code, out_bucket, key_prefix):
    """The StartSpeechSynthesisTask request for these inputs."""
    request = {
        "OutputS3BucketName": out_bucket,
        "OutputS3KeyPrefix": key_prefix,
        "Text": text,
        "TextType": text_type,
        "VoiceId": voice_id,
        "Engine": engine,
        "OutputFormat": output_format,
    }
    if language_code:
        request["LanguageCode"] = language_code
    return request


def start(polly, request):
    """Starts the synthesis task. Fails the node when Polly turns the request down."""
    try:
        return polly.start_speech_synthesis_task(**request)["SynthesisTask"]
    except ClientError as error:
        if error_code(error) in REQUEST_ERRORS:
            patchbae.fail(f"Polly did not accept the request: {error_message(error)}")
        raise


def wait_for(polly, polly_task_id):
    """Polls until the task has finished, backing off to one request every 30 seconds."""
    delay = POLL_START_SECONDS
    while True:
        task = polly.get_speech_synthesis_task(TaskId=polly_task_id)["SynthesisTask"]
        if task["TaskStatus"] in ("completed", "failed"):
            return task
        time.sleep(delay + random.uniform(0, 1))
        delay = min(delay * 1.5, POLL_MAX_SECONDS)


def find_audio(s3, bucket, key_prefix):
    """The key of the audio file Polly already wrote for this task, or None."""
    listing = s3.list_objects_v2(Bucket=bucket, Prefix=f"{key_prefix}.", MaxKeys=10)
    keys = sorted(item["Key"] for item in listing.get("Contents", []))
    return keys[0] if keys else None


def find_task(polly, polly_task_id):
    """The synthesis task, or None when Polly no longer keeps it."""
    try:
        return polly.get_speech_synthesis_task(TaskId=polly_task_id)["SynthesisTask"]
    except ClientError as error:
        if error_code(error) in TASK_GONE_CODES:
            return None
        raise


def written_key(task, key_prefix, extension):
    """The S3 key of the file Polly wrote: <key prefix>.<Polly task id>.<extension>.

    The bucket and the prefix are known, so the key is the end of the URL Polly returns,
    from the prefix on. The host name, which differs per region, is never read.
    """
    path = unquote(urlparse(task.get("OutputUri") or "").path)
    at = path.find(key_prefix)
    if at >= 0:
        return path[at:]
    return f"{key_prefix}.{task['TaskId']}.{extension}"


def one_of(value, options, name):
    choice = str(value).strip().lower()
    if choice not in options:
        patchbae.fail(f"{name} must be one of {', '.join(options)}, got {value!r}.")
    return choice


def split_s3_uri(uri, name):
    parsed = urlparse(str(uri or ""))
    if parsed.scheme != "s3" or not parsed.netloc:
        patchbae.fail(f"{name} must be an S3 URI such as s3://bucket/path, got {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def error_code(error):
    return error.response.get("Error", {}).get("Code", "")


def error_message(error):
    return error.response.get("Error", {}).get("Message", "") or error_code(error)
