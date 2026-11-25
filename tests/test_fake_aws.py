"""FakeAws catches the mistakes a test without an AWS account would otherwise let through."""

import datetime

import pytest

from patchbae.testing import FakeAws


@pytest.fixture
def medialive():
    return FakeAws().client("medialive")


def test_a_valid_call_returns_the_mocked_response_and_is_recorded(medialive):
    medialive.respond("describe_channel", {"Id": "1234567", "State": "IDLE"})

    assert medialive.describe_channel(ChannelId="1234567") == {"Id": "1234567", "State": "IDLE"}
    assert medialive.called("describe_channel") == [{"ChannelId": "1234567"}]


def test_responses_are_handed_out_in_order_and_the_last_one_repeats(medialive):
    medialive.respond("describe_channel", {"State": "STARTING"}, {"State": "RUNNING"})

    states = [medialive.describe_channel(ChannelId="1")["State"] for _ in range(3)]

    assert states == ["STARTING", "RUNNING", "RUNNING"]


def test_an_operation_the_api_does_not_have_is_refused(medialive):
    with pytest.raises(AssertionError, match="no operation 'launch_channel'"):
        medialive.launch_channel(ChannelId="1")


@pytest.mark.parametrize(
    "parameters, problem",
    [
        ({"Channel": "1"}, "Unknown parameter"),
        ({}, "Missing required parameter"),
        ({"ChannelId": 1}, "Invalid type"),
    ],
)
def test_parameters_the_api_does_not_accept_are_refused(medialive, parameters, problem):
    medialive.respond("describe_channel", {})

    with pytest.raises(AssertionError, match=problem):
        medialive.describe_channel(**parameters)


def test_a_value_the_api_does_not_list_is_refused():
    ivs = FakeAws().client("ivs")
    ivs.respond("create_channel", {})

    with pytest.raises(AssertionError, match="'ULTRA_LOW' is not one of NORMAL, LOW"):
        ivs.create_channel(name="studio", latencyMode="ULTRA_LOW")


def test_a_value_that_does_not_match_the_api_s_pattern_is_refused():
    deadline = FakeAws().client("deadline")
    deadline.respond("get_job", {})

    with pytest.raises(AssertionError, match="does not match farm-"):
        deadline.get_job(farmId="my-farm", queueId="queue-" + "0" * 32, jobId="job-" + "0" * 32)


def test_a_call_without_a_mocked_response_is_refused(medialive):
    with pytest.raises(AssertionError, match="set up no response"):
        medialive.describe_channel(ChannelId="1")


@pytest.mark.parametrize(
    "response, problem",
    [
        ({"Status": "IDLE"}, "response.Status: no such field"),
        ({"State": "PAUSED"}, "'PAUSED' is not one of"),
        ({"PipelinesRunningCount": "2"}, "expected integer, got str"),
    ],
)
def test_a_mocked_response_the_api_would_not_return_is_refused(medialive, response, problem):
    medialive.respond("describe_channel", response)

    with pytest.raises(AssertionError, match=problem):
        medialive.describe_channel(ChannelId="1")


def test_an_error_is_raised_with_its_code_and_status(medialive):
    medialive.respond("describe_channel", FakeAws.error("NotFoundException", "no such channel", status=404))

    with pytest.raises(Exception) as raised:
        medialive.describe_channel(ChannelId="1")

    assert raised.value.response["Error"] == {"Code": "NotFoundException", "Message": "no such channel"}
    assert raised.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404


def test_an_error_the_operation_cannot_fail_with_is_refused(medialive):
    medialive.respond("describe_channel", FakeAws.error("ResourceNotFoundException"))

    with pytest.raises(AssertionError, match="cannot fail with 'ResourceNotFoundException'"):
        medialive.describe_channel(ChannelId="1")


def test_a_response_can_be_worked_out_from_the_call(medialive):
    medialive.respond("describe_channel", lambda parameters: {"Id": parameters["ChannelId"]})

    assert medialive.describe_channel(ChannelId="42") == {"Id": "42"}


def test_timestamps_and_streams_are_accepted_where_the_api_has_them():
    aws = FakeAws()
    packages = aws.client("mediapackagev2")
    packages.respond("get_harvest_job", {"CreatedAt": datetime.datetime(2026, 11, 25, tzinfo=datetime.timezone.utc)})
    archive = aws.client("kinesis-video-archived-media", endpoint_url="https://example.invalid")

    class Stream:
        def read(self, _size=-1):
            return b""

    archive.respond("get_clip", {"ContentType": "video/mp4", "Payload": Stream()})

    assert packages.get_harvest_job(ChannelGroupName="g", ChannelName="c", OriginEndpointName="e", HarvestJobName="j")
    assert archive.get_clip(
        StreamName="cam",
        ClipFragmentSelector={
            "FragmentSelectorType": "PRODUCER_TIMESTAMP",
            "TimestampRange": {"StartTimestamp": datetime.datetime(2026, 1, 1), "EndTimestamp": datetime.datetime(2026, 1, 1, 0, 1)},
        },
    )
    assert archive.created_with == [{"endpoint_url": "https://example.invalid"}]
