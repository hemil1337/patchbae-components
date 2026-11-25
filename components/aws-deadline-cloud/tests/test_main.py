import datetime
import io

import pytest

import patchbae
from patchbae.testing import FakeAws, load_component

main = load_component(__file__)

TASK = patchbae.task.id
UTC = datetime.timezone.utc
FARM = "farm-0123456789abcdef0123456789abcdef"
QUEUE = "queue-0123456789abcdef0123456789abcdef"
JOB = "job-0123456789abcdef0123456789abcdef"
WHERE = {"farmId": FARM, "queueId": QUEUE, "jobId": JOB}
DESCRIPTION = f"Submitted by Patchbae. Task {TASK}, run {patchbae.task.run_id}."
TEMPLATE = """specificationVersion: jobtemplate-2023-09
name: Promo endboard
parameterDefinitions:
  - name: Frames
    type: STRING
  - name: Scene
    type: PATH
steps:
  - name: Render
    parameterSpace:
      taskParameterDefinitions:
        - name: Frame
          type: INT
          range: "{{Param.Frames}}"
    script:
      actions:
        onRun:
          command: blender
          args: ["-b", "{{Param.Scene}}", "-f", "{{Task.Param.Frame}}"]
"""
INPUTS = {"farm_id": FARM, "queue_id": QUEUE, "template_uri": "s3://northlight-graphics/templates/promo-endboard.yaml"}


def job(task_run_status, lifecycle="CREATE_COMPLETE", **extra):
    return {
        "jobId": JOB,
        "name": "Promo endboard",
        "description": DESCRIPTION,
        "lifecycleStatus": lifecycle,
        "lifecycleStatusMessage": "",
        "priority": 50,
        "createdAt": datetime.datetime(2026, 11, 25, 16, 0, tzinfo=UTC),
        "createdBy": "arn:aws:sts::123456789012:assumed-role/patchbae-aws-deadline-cloud/patchbae",
        "taskRunStatus": task_run_status,
        **extra,
    }


DONE = job(
    "SUCCEEDED",
    taskRunStatusCounts={"SUCCEEDED": 250, "FAILED": 0, "RUNNING": 0},
    startedAt=datetime.datetime(2026, 11, 25, 16, 2, tzinfo=UTC),
    endedAt=datetime.datetime(2026, 11, 25, 16, 41, 30, tzinfo=UTC),
)


@pytest.fixture
def aws(monkeypatch):
    fake = FakeAws()
    deadline = fake.client("deadline")
    deadline.respond("search_jobs", {"jobs": [], "totalResults": 0})
    deadline.respond("create_job", {"jobId": JOB})
    deadline.respond("get_job", job("PENDING", "CREATE_IN_PROGRESS"), job("RUNNING", taskRunStatusCounts={"RUNNING": 40, "READY": 210}), DONE)
    fake.client("s3").respond("get_object", lambda _request: {"Body": io.BytesIO(TEMPLATE.encode("utf-8"))})
    monkeypatch.setattr(main.boto3, "client", fake.client)
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)
    patchbae.reset()
    return fake


def test_submits_a_job_from_a_template_on_s3_and_waits_for_it(aws):
    deadline = aws.client("deadline")

    output = main.run({**INPUTS, "parameters": {"Frames": "1-250", "Scene": {"path": "/mnt/projects/promo.blend"}, "Samples": 128, "Gamma": 2.2, "Denoise": True}})

    assert aws.client("s3").called("get_object") == [{"Bucket": "northlight-graphics", "Key": "templates/promo-endboard.yaml"}]
    assert deadline.called("search_jobs") == [
        {
            "farmId": FARM,
            "queueIds": [QUEUE],
            "itemOffset": 0,
            "pageSize": 25,
            "filterExpressions": {"filters": [{"searchTermFilter": {"searchTerm": TASK}}], "operator": "AND"},
        }
    ]
    assert deadline.called("create_job") == [
        {
            "farmId": FARM,
            "queueId": QUEUE,
            "priority": 50,
            "template": TEMPLATE.strip(),
            "templateType": "YAML",
            "clientToken": f"patchbae-{TASK}",
            "descriptionOverride": DESCRIPTION,
            "parameters": {
                "Frames": {"string": "1-250"},
                "Scene": {"path": "/mnt/projects/promo.blend"},
                "Samples": {"int": "128"},
                "Gamma": {"float": "2.2"},
                "Denoise": {"string": "true"},
            },
        }
    ]
    assert deadline.called("get_job") == [WHERE] * 3
    assert output == {
        "job_id": JOB,
        "job_name": "Promo endboard",
        "lifecycle_status": "CREATE_COMPLETE",
        "task_run_status": "SUCCEEDED",
        "task_counts": {"SUCCEEDED": 250},
        "started_at": "2026-11-25T16:02:00Z",
        "ended_at": "2026-11-25T16:41:30Z",
    }


def test_an_inline_json_template_with_every_option(aws):
    main.run(
        {
            "farm_id": FARM,
            "queue_id": QUEUE,
            "template": '{"specificationVersion": "jobtemplate-2023-09", "name": "Still", "steps": []}',
            "parameters": '{"Frames": "1"}',
            "priority": 90,
            "name": "Winter promo endboard",
            "max_failed_tasks": 5,
            "max_retries_per_task": 2,
            "max_workers": 20,
            "storage_profile_id": "sp-0123456789abcdef0123456789abcdef",
        }
    )

    (request,) = aws.client("deadline").called("create_job")
    assert request["templateType"] == "JSON"
    assert request["parameters"] == {"Frames": {"string": "1"}}
    assert {key: request[key] for key in ("priority", "nameOverride", "maxFailedTasksCount", "maxRetriesPerTask", "maxWorkerCount", "storageProfileId")} == {
        "priority": 90,
        "nameOverride": "Winter promo endboard",
        "maxFailedTasksCount": 5,
        "maxRetriesPerTask": 2,
        "maxWorkerCount": 20,
        "storageProfileId": "sp-0123456789abcdef0123456789abcdef",
    }
    assert aws.client("s3").calls == []


def test_a_redelivered_task_waits_for_its_job_instead_of_rendering_again(aws):
    deadline = aws.client("deadline")
    someone_elses = {"jobId": "job-ffffffffffffffffffffffffffffffff", "name": f"notes mention {TASK}"}
    deadline.respond("search_jobs", {"jobs": [someone_elses, {"jobId": JOB, "name": "Promo endboard"}], "totalResults": 2})
    deadline.respond("get_job", lambda request: DONE if request["jobId"] == JOB else {**job("RUNNING"), "jobId": request["jobId"], "description": "Artist submission"})

    output = main.run(INPUTS)

    assert deadline.called("create_job") == []
    assert aws.client("s3").calls == []
    assert output["job_id"] == JOB


def test_without_waiting_the_node_returns_as_soon_as_the_job_is_accepted(aws):
    output = main.run({**INPUTS, "wait": False})

    assert len(aws.client("deadline").called("get_job")) == 1
    assert (output["task_run_status"], output["lifecycle_status"], output["ended_at"]) == ("PENDING", "CREATE_IN_PROGRESS", "")


@pytest.mark.parametrize(
    "finished, message",
    [
        (job("FAILED", taskRunStatusCounts={"SUCCEEDED": 247, "FAILED": 3}), "failed: 3 of 250 tasks failed"),
        (job("CANCELED"), "was canceled before it finished"),
        (job("SUSPENDED"), "was suspended"),
        (job("NOT_COMPATIBLE"), "no fleet of the queue has workers"),
    ],
)
def test_a_job_that_does_not_succeed_fails_the_node(aws, finished, message):
    aws.client("deadline").respond("get_job", finished)

    with pytest.raises(patchbae.ComponentFailed, match=message):
        main.run(INPUTS)


def test_a_job_deadline_cannot_set_up_fails_the_node_with_the_reason(aws):
    aws.client("deadline").respond("get_job", job("PENDING", "CREATE_FAILED", lifecycleStatusMessage="Step Render: unknown parameter Param.Scenes"))

    with pytest.raises(patchbae.ComponentFailed, match=r"could not set the job up \(CREATE_FAILED\): Step Render: unknown parameter"):
        main.run(INPUTS)


def test_a_job_deadline_refuses_fails_the_node_with_its_reason(aws):
    aws.client("deadline").respond("create_job", FakeAws.error("ValidationException", "Template is missing specificationVersion"))

    with pytest.raises(patchbae.ComponentFailed, match="would not accept the job: Template is missing specificationVersion"):
        main.run(INPUTS)


def test_a_template_that_is_not_on_s3_fails_the_node_before_anything_is_submitted(aws):
    aws.client("s3").respond("get_object", FakeAws.error("NoSuchKey", status=404))

    with pytest.raises(patchbae.ComponentFailed, match="s3://northlight-graphics/templates/promo-endboard.yaml was not found"):
        main.run(INPUTS)

    assert aws.client("deadline").called("create_job") == []


def test_a_template_that_is_too_large_or_empty_is_refused(aws):
    s3 = aws.client("s3")
    s3.respond("get_object", lambda _request: {"Body": io.BytesIO(b"x" * 1_000_001)})
    with pytest.raises(patchbae.ComponentFailed, match="larger than 1 MB"):
        main.run(INPUTS)

    s3.respond("get_object", lambda _request: {"Body": io.BytesIO(b"  \n")})
    with pytest.raises(patchbae.ComponentFailed, match="is empty"):
        main.run(INPUTS)


def test_a_missing_permission_is_not_swallowed(aws):
    aws.client("deadline").respond("search_jobs", FakeAws.error("AccessDeniedException", status=403))

    with pytest.raises(main.ClientError):
        main.run(INPUTS)


@pytest.mark.parametrize(
    "inputs, message",
    [
        ({**INPUTS, "farm_id": "northlight-farm"}, "farm_id must be the farm's ID"),
        ({**INPUTS, "queue_id": "promos"}, "queue_id must be the queue's ID"),
        ({"farm_id": FARM, "queue_id": QUEUE}, "either as a file"),
        ({**INPUTS, "template": TEMPLATE}, "either as a file"),
        ({**INPUTS, "template_uri": "s3://northlight-graphics/templates/promo.blend"}, "ending in .json, .yaml or .yml"),
        ({**INPUTS, "template_uri": "templates/promo.yaml"}, "template_uri must be an S3 URI"),
        ({**INPUTS, "priority": 101}, "priority must be a whole number from 0 to 100"),
        ({**INPUTS, "max_workers": 0}, "max_workers must be a whole number from 1"),
        ({**INPUTS, "max_retries_per_task": 1.5}, "max_retries_per_task must be a whole number"),
        ({**INPUTS, "storage_profile_id": "studio"}, "storage_profile_id must be"),
        ({**INPUTS, "parameters": ["Frames", "1-250"]}, "parameters must be a JSON object"),
        ({**INPUTS, "parameters": "Frames=1-250"}, "parameters must be a JSON object"),
        ({**INPUTS, "parameters": {"Frames": [1, 250]}}, "Parameter 'Frames' must be text"),
        ({**INPUTS, "parameters": {"Scene": {"file": "promo.blend"}}}, "Parameter 'Scene' must be text"),
    ],
)
def test_bad_inputs_fail_before_anything_is_submitted(aws, inputs, message):
    with pytest.raises(patchbae.ComponentFailed, match=message):
        main.run(inputs)

    assert aws.client("deadline").calls == [] and aws.client("s3").calls == []
