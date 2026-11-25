import pytest

import patchbae
from patchbae.testing import FakeAws, load_component

main = load_component(__file__)

FLOW_ARN = "arn:aws:mediaconnect:eu-north-1:123456789012:flow:1-23aBC45dEF67hiJ8-12AbC34DE5fG:stadium-feed"
FLOW = {
    "FlowArn": FLOW_ARN,
    "Name": "stadium-feed",
    "AvailabilityZone": "eu-north-1a",
    "EgressIp": "13.48.20.7",
    "Entitlements": [],
    "Outputs": [],
    "Source": {
        "Name": "stadium-encoder",
        "SourceArn": "arn:aws:mediaconnect:eu-north-1:123456789012:source:2-3aBC45dEF67hiJ89-c34de5fG678h:stadium-encoder",
        "IngestIp": "13.48.20.7",
        "IngestPort": 5000,
        "Transport": {"Protocol": "srt-listener"},
        "WhitelistCidr": "203.0.113.10/32",
    },
}


def flow(status, errors=()):
    return {"Flow": {**FLOW, "Status": status}, **({"Messages": {"Errors": list(errors)}} if errors else {})}


@pytest.fixture
def mediaconnect(monkeypatch):
    aws = FakeAws()
    client = aws.client("mediaconnect")
    client.respond("start_flow", {"FlowArn": FLOW_ARN, "Status": "STARTING"})
    client.respond("stop_flow", {"FlowArn": FLOW_ARN, "Status": "STOPPING"})
    monkeypatch.setattr(main.boto3, "client", aws.client)
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)
    patchbae.reset()
    return client


def test_starts_a_flow_on_standby_and_returns_where_to_send_the_feed(mediaconnect):
    mediaconnect.respond("describe_flow", flow("STANDBY"), flow("STARTING"), flow("ACTIVE"))

    output = main.run({"flow_arn": FLOW_ARN, "action": "start_flow"})

    assert mediaconnect.called("start_flow") == [{"FlowArn": FLOW_ARN}]
    assert output == {
        "flow_arn": FLOW_ARN,
        "flow_name": "stadium-feed",
        "status": "ACTIVE",
        "ingest_ip": "13.48.20.7",
        "ingest_port": 5000,
        "ingest_protocol": "srt-listener",
        "egress_ip": "13.48.20.7",
    }


def test_an_active_flow_is_not_started_again(mediaconnect):
    mediaconnect.respond("describe_flow", flow("ACTIVE"))

    output = main.run({"flow_arn": FLOW_ARN, "action": "start_flow"})

    assert mediaconnect.called("start_flow") == []
    assert output["status"] == "ACTIVE"


def test_a_redelivered_start_waits_for_the_flow_that_is_already_starting(mediaconnect):
    mediaconnect.respond("describe_flow", flow("STARTING"), flow("ACTIVE"))

    main.run({"flow_arn": FLOW_ARN, "action": "start_flow"})

    assert mediaconnect.called("start_flow") == []


def test_stops_an_active_flow_and_waits_for_standby(mediaconnect):
    mediaconnect.respond("describe_flow", flow("ACTIVE"), flow("STOPPING"), flow("STANDBY"))

    output = main.run({"flow_arn": FLOW_ARN, "action": "stop_flow"})

    assert mediaconnect.called("stop_flow") == [{"FlowArn": FLOW_ARN}]
    assert output["status"] == "STANDBY"


def test_a_flow_on_standby_is_not_stopped_again(mediaconnect):
    mediaconnect.respond("describe_flow", flow("STANDBY"))

    main.run({"flow_arn": FLOW_ARN, "action": "stop_flow"})

    assert mediaconnect.called("stop_flow") == []


def test_without_waiting_the_node_returns_once_the_flow_is_starting(mediaconnect):
    mediaconnect.respond("describe_flow", flow("STANDBY"))

    output = main.run({"flow_arn": FLOW_ARN, "action": "start_flow", "wait": False})

    assert len(mediaconnect.called("describe_flow")) == 1
    assert output["status"] == "STARTING"


def test_a_source_without_an_ingest_address_returns_empty_values(mediaconnect):
    entitled = flow("ACTIVE")
    entitled["Flow"]["Source"] = {"Name": "partner", "SourceArn": "arn:aws:mediaconnect:eu-north-1:123456789012:source:1:partner"}
    mediaconnect.respond("describe_flow", entitled)

    output = main.run({"flow_arn": FLOW_ARN, "action": "start_flow"})

    assert (output["ingest_ip"], output["ingest_port"], output["ingest_protocol"]) == ("", 0, "")


def test_a_flow_in_error_fails_the_node_with_the_errors_mediaconnect_reports(mediaconnect):
    mediaconnect.respond("describe_flow", flow("ERROR", ["Source is not reachable", "No bitrate for 60 seconds"]))

    with pytest.raises(patchbae.ComponentFailed, match="stadium-feed is in ERROR: Source is not reachable; No bitrate"):
        main.run({"flow_arn": FLOW_ARN, "action": "start_flow"})


def test_a_flow_that_does_not_exist_fails_the_node(mediaconnect):
    mediaconnect.respond("describe_flow", FakeAws.error("NotFoundException", status=404))

    with pytest.raises(patchbae.ComponentFailed, match="The flow was not found"):
        main.run({"flow_arn": FLOW_ARN, "action": "start_flow"})


def test_a_start_mediaconnect_refuses_fails_the_node_with_its_reason(mediaconnect):
    mediaconnect.respond("describe_flow", flow("STANDBY"))
    mediaconnect.respond("start_flow", FakeAws.error("BadRequestException", "The flow has no outputs"))

    with pytest.raises(patchbae.ComponentFailed, match="MediaConnect would not start the flow: The flow has no outputs"):
        main.run({"flow_arn": FLOW_ARN, "action": "start_flow"})


def test_a_service_outage_is_not_swallowed(mediaconnect):
    mediaconnect.respond("describe_flow", FakeAws.error("ServiceUnavailableException", status=503))

    with pytest.raises(main.ClientError):
        main.run({"flow_arn": FLOW_ARN, "action": "stop_flow"})


@pytest.mark.parametrize(
    "inputs, message",
    [
        ({"flow_arn": "stadium-feed", "action": "start_flow"}, "must be the ARN of a MediaConnect flow"),
        ({"flow_arn": FLOW_ARN, "action": "restart_flow"}, "Unknown action 'restart_flow'"),
        ({"flow_arn": FLOW_ARN}, "Unknown action ''"),
    ],
)
def test_bad_inputs_fail_before_anything_is_changed(mediaconnect, inputs, message):
    with pytest.raises(patchbae.ComponentFailed, match=message):
        main.run(inputs)

    assert mediaconnect.calls == []
