"""AWS Elemental Inference: create a live AI feed, switch its outputs, find a sports fixture.

Elemental Inference works on a live stream that another service, usually a
MediaLive channel, sends to a feed. This pod only makes API calls: it sets
the feed up and changes it. No media passes through it.
"""

import datetime
import random
import re
import time

import boto3
from botocore.exceptions import ClientError

import patchbae

ACTIONS = ("create_feed", "set_output_status", "find_fixture")
# The name each feature's output gets in a feed this component creates.
FEATURES = {"smart_crop": "crop", "clips": "clips", "subtitles": "subtitles", "contextual_metadata": "metadata"}
LANGUAGES = ("eng", "eng-au", "eng-gb", "eng-us", "fra", "ita", "deu", "spa", "por", "ara")
SPORTS = ("basketball", "american-football")
RESOURCE_NAME = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9_-]{0,126}[a-zA-Z0-9])?$")
FEED_ID = re.compile(r"^[a-z0-9]{19}$")
ROLE_ARN = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/.+$")
TEMPLATE_URI = re.compile(r"^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/.+$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
GONE = ("DELETING", "DELETED", "ARCHIVED")
REFUSED = ("ValidationException", "ConflictException", "ServiceQuotaExceededException")
TASK_TAG = "patchbae_task_id"
PAGES = 10
POLL_START_SECONDS = 5
POLL_MAX_SECONDS = 30
TEXT_OUTPUTS = ("feed_id", "feed_arn", "feed_status", "output_name", "output_status", "action_name", "scheduled_at", "fixture_id", "fixture_name")


def run(inputs):
    action = text(inputs, "action")
    if action not in ACTIONS:
        patchbae.fail(f"Unknown action {action!r}. Use create_feed, set_output_status or find_fixture.")
    if action == "create_feed":
        return create_feed(inputs)
    if action == "set_output_status":
        return set_output_status(inputs)
    return find_fixture(inputs)


def create_feed(inputs):
    name = text(inputs, "feed_name")
    if not RESOURCE_NAME.match(name):
        patchbae.fail("action is create_feed, so feed_name must be letters, digits, dashes and underscores, starting and ending with a letter or digit.")
    features = [str(item).strip() for item in (inputs.get("features") or []) if str(item).strip()]
    unknown = [item for item in features if item not in FEATURES]
    if unknown:
        patchbae.fail(f"Unknown feature {unknown[0]!r}. Use smart_crop, clips, subtitles or contextual_metadata.")
    if not features:
        patchbae.fail("features must list what the feed produces: any of smart_crop, clips, subtitles, contextual_metadata.")
    role_arn = text(inputs, "access_role_arn")
    if role_arn and not ROLE_ARN.match(role_arn):
        patchbae.fail("access_role_arn must be the ARN of an IAM role, such as arn:aws:iam::123456789012:role/ElementalInferenceAccessRole.")
    outputs = [feed_output(feature, inputs, role_arn) for feature in dict.fromkeys(features)]

    inference = boto3.client("elementalinference")
    feed = find_feed(inference, name)
    if feed:
        patchbae.log("found the feed with this name", feed=name, status=feed.get("status", ""))
    else:
        request = {"name": name, "outputs": outputs, "tags": {TASK_TAG: patchbae.task.id}}
        if role_arn:
            request["accessRoleArn"] = role_arn
        try:
            feed = inference.create_feed(**request)
        except ClientError as error:
            if error_code(error) in REFUSED:
                patchbae.fail(f"Elemental Inference would not create the feed: {error_message(error)}")
            raise
        patchbae.log("feed created", feed=name, outputs=[item["name"] for item in outputs])

    delay = POLL_START_SECONDS
    while feed.get("status") in ("CREATING", "UPDATING"):
        time.sleep(delay + random.uniform(0, 1))
        delay = min(delay * 1.5, POLL_MAX_SECONDS)
        feed = inference.get_feed(id=feed["id"])
    if feed.get("status") in GONE:
        patchbae.fail(f"Feed {name} is {feed['status']}. Use another feed_name.")
    return result(
        feed_id=feed.get("id", ""),
        feed_arn=feed.get("arn", ""),
        feed_status=feed.get("status", ""),
        data_endpoints=list(feed.get("dataEndpoints", [])),
        output_names=[item.get("name", "") for item in feed.get("outputs", [])],
    )


def feed_output(feature, inputs, role_arn):
    """The feed output of one feature. Input problems fail here, before anything is created."""
    if feature == "smart_crop":
        config = {}
        templates = [str(uri).strip() for uri in (inputs.get("crop_template_uris") or []) if str(uri).strip()]
        for uri in templates:
            if not TEMPLATE_URI.match(uri):
                patchbae.fail(f"crop_template_uris holds S3 URIs of template files, such as s3://bucket/templates/score.json. Got {uri!r}.")
        if templates and not role_arn:
            patchbae.fail("crop_template_uris is set, so access_role_arn must name the role Elemental Inference reads the templates with.")
        if templates:
            config["templateGroups"] = [{"name": "default", "templateUris": templates}]
        output_config = {"cropping": config}
    elif feature == "clips":
        config = {}
        fixture = text(inputs, "fixture_id")
        if fixture:
            config["dataSourceConfiguration"] = {"fixtureId": fixture}
        output_config = {"clipping": config}
    elif feature == "subtitles":
        language = text(inputs, "subtitle_language") or "eng"
        if language not in LANGUAGES:
            patchbae.fail(f"Unknown subtitle_language {language!r}. Use one of {', '.join(LANGUAGES)}.")
        output_config = {"subtitling": {"language": language}}
    else:
        output_config = {"contextualMetadata": {}}
    return {"name": FEATURES[feature], "outputConfig": output_config, "status": "ENABLED"}


def find_feed(inference, name):
    """The feed with this name that is still in use, or None."""
    request = {"maxResults": 100}
    for _page in range(PAGES):
        page = inference.list_feeds(**request)
        for summary in page.get("feeds", []):
            if summary.get("name") == name and summary.get("status") not in GONE:
                return inference.get_feed(id=summary["id"])
        if not page.get("nextToken"):
            break
        request["nextToken"] = page["nextToken"]
    return None


def set_output_status(inputs):
    feed_id = text(inputs, "feed_id")
    if not FEED_ID.match(feed_id):
        patchbae.fail("action is set_output_status, so feed_id must be the feed's ID: 19 lowercase letters and digits.")
    output_name = text(inputs, "output_name")
    if not output_name:
        patchbae.fail("action is set_output_status, so output_name must name the feed output to switch.")
    status = "ENABLED" if inputs.get("enabled", True) is not False else "DISABLED"
    at = text(inputs, "at")
    when = moment(at) if at else None

    inference = boto3.client("elementalinference")
    try:
        feed = inference.get_feed(id=feed_id)
    except ClientError as error:
        if error_code(error) == "ResourceNotFoundException":
            patchbae.fail(f"Feed {feed_id} was not found. Check the ID and the region the component is set up for.")
        raise
    names = [item.get("name", "") for item in feed.get("outputs", [])]
    if output_name not in names:
        patchbae.fail(f"Feed {feed_id} has no output named {output_name!r}. Its outputs: {', '.join(names) or 'none'}.")

    if when is None:
        current = next(item for item in feed["outputs"] if item.get("name") == output_name)
        if current.get("status") == status:
            patchbae.log("output already has this status", feed=feed_id, output=output_name, status=status)
        else:
            request = {"id": feed_id, "name": feed["name"], "outputs": [{**item, "status": status} if item.get("name") == output_name else item for item in feed["outputs"]]}
            if feed.get("accessRoleArn"):
                request["accessRoleArn"] = feed["accessRoleArn"]
            change(inference.update_feed, request, "change the feed")
            patchbae.log("output switched", feed=feed_id, output=output_name, status=status)
        return result(feed_id=feed_id, output_name=output_name, output_status=status, scheduled_at="now")

    # One schedule action per task: the name is how a second delivery finds the first.
    action_name = f"patchbae-{patchbae.task.id}"
    stamp = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    if find_schedule_action(inference, feed_id, action_name):
        patchbae.log("found the schedule action from an earlier delivery", feed=feed_id, action=action_name)
    else:
        request = {
            "id": feed_id,
            "actionName": action_name,
            "startConfiguration": {"fixedModeScheduleActionStartSettings": {"scheduledAt": when}},
            "operations": [{"updateOutputStatus": {"outputName": output_name, "status": status}}],
        }
        change(inference.create_schedule_action, request, "schedule the change")
        patchbae.log("output switch scheduled", feed=feed_id, output=output_name, status=status, at=stamp)
    return result(feed_id=feed_id, output_name=output_name, output_status=status, action_name=action_name, scheduled_at=stamp)


def find_schedule_action(inference, feed_id, action_name):
    request = {"id": feed_id, "maxResults": 100}
    for _page in range(PAGES):
        page = inference.list_schedule_actions(**request)
        for item in page.get("scheduleActions", []):
            if item.get("actionName") == action_name:
                return item
        if not page.get("nextToken"):
            break
        request["nextToken"] = page["nextToken"]
    return None


def find_fixture(inputs):
    sport = text(inputs, "sport")
    if sport not in SPORTS:
        patchbae.fail(f"action is find_fixture, so sport must be basketball or american-football. Got {sport!r}.")
    date = text(inputs, "date")
    end_date = text(inputs, "end_date")
    if not DATE.match(date) or (end_date and not DATE.match(end_date)):
        patchbae.fail("date and end_date are days in UTC, written as YYYY-MM-DD, for example 2026-11-25.")
    if end_date and end_date < date:
        patchbae.fail("end_date must be on or after date.")
    competitors = [str(item).strip() for item in (inputs.get("competitors") or []) if str(item).strip()]

    request = {"sport": sport, "startDate": date, "maxResults": 100}
    if end_date:
        request["endDate"] = end_date
    if competitors:
        request["filters"] = [{"name": "COMPETITOR", "values": competitors}]
    inference = boto3.client("elementalinference")
    found = []
    for _page in range(PAGES):
        try:
            page = inference.search_fixtures(**request)
        except ClientError as error:
            if error_code(error) == "ValidationException":
                patchbae.fail(f"Elemental Inference would not run the search: {error_message(error)}")
            raise
        found += page.get("fixtures", [])
        if not page.get("nextToken"):
            break
        request["nextToken"] = page["nextToken"]

    fixtures = sorted((fixture_summary(item) for item in found), key=lambda item: (item["scheduled_start"] or "9", item["name"]))
    if not fixtures:
        window = date if not end_date else f"{date} to {end_date}"
        teams = f" with {' or '.join(competitors)}" if competitors else ""
        patchbae.fail(f"No {sport} fixture{teams} was found on {window}. Check the date (it is in UTC) and the team names.")
    if len(fixtures) > 1:
        patchbae.warn("more than one fixture matched; fixture_id is the earliest", matches=len(fixtures))
    patchbae.log("fixture found", fixture=fixtures[0]["fixture_id"], name=fixtures[0]["name"])
    return result(fixture_id=fixtures[0]["fixture_id"], fixture_name=fixtures[0]["name"], fixture_count=len(fixtures), fixtures=fixtures)


def fixture_summary(fixture):
    start = fixture.get("scheduledStart")
    if isinstance(start, datetime.datetime):
        start = start.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "fixture_id": fixture.get("fixtureId", ""),
        "name": fixture.get("name", ""),
        "group": fixture.get("fixtureGroup", ""),
        "scheduled_start": str(start or ""),
        "status": fixture.get("status", ""),
        "competitors": [item.get("name", "") for item in fixture.get("competitors", [])],
    }


def change(operation, request, what):
    """Makes the change. A refusal fails the node with Elemental Inference's reason."""
    try:
        return operation(**request)
    except ClientError as error:
        if error_code(error) in REFUSED:
            patchbae.fail(f"Elemental Inference would not {what}: {error_message(error)}")
        raise


def moment(value):
    """The time as UTC in whole seconds. Anything that is not a time with a time zone fails the node."""
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        patchbae.fail(f"at must be a UTC time such as 2026-11-25T19:00:00Z, got {value!r}.")
    return parsed.astimezone(datetime.timezone.utc).replace(microsecond=0)


def result(**values):
    """Every output, with empty values for the ones this action does not produce."""
    empty = {**{name: "" for name in TEXT_OUTPUTS}, "data_endpoints": [], "output_names": [], "fixture_count": 0, "fixtures": []}
    return {**empty, **values}


def text(inputs, name):
    return str(inputs.get(name) or "").strip()


def error_code(error):
    return error.response.get("Error", {}).get("Code", "")


def error_message(error):
    return error.response.get("Error", {}).get("Message") or "no reason given"
