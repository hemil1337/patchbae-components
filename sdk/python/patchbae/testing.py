"""Test helpers for components.

Load a component's entrypoint the way the runner does:

    from patchbae.testing import load_component
    main = load_component(__file__)      # in components/<name>/tests/test_*.py

The runner loads the entrypoint file by path, with its folder on the import
path. Doing the same here keeps tests of different components apart even
though every component calls its entrypoint `src/main.py`.

Stand in for AWS, and have every call checked against the SDK:

    from patchbae.testing import FakeAws
    aws = FakeAws()
    monkeypatch.setattr(main.boto3, "client", aws.client)
    aws.client("medialive").respond("describe_channel", {"Id": "123", "State": "IDLE"})

No test in this library reaches an AWS account, so a misspelled parameter or
a made-up response field would pass unnoticed. FakeAws reads the service
models that ship with the installed SDK and fails the test when a call or a
mocked response does not match them.
"""

import importlib.util
import pathlib
import re
import sys

_ENTRYPOINT = re.compile(r"^\s*entrypoint:\s*['\"]?([^'\"\s#]+)", re.MULTILINE)


def load_component(test_file):
    """The entrypoint module of the component that `test_file` belongs to."""
    root = pathlib.Path(test_file).resolve().parents[1]
    manifest = (root / "component.yaml").read_text(encoding="utf-8")
    match = _ENTRYPOINT.search(manifest)
    if not match:
        raise ValueError(f"{root / 'component.yaml'} has no runtime.entrypoint")
    entrypoint = root / match.group(1)

    folder = str(entrypoint.parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    name = "component_" + re.sub(r"\W", "_", root.name)
    spec = importlib.util.spec_from_file_location(name, entrypoint)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Error codes any AWS operation can answer with, whether or not its model lists them.
_COMMON_ERRORS = {
    "AccessDenied",
    "AccessDeniedException",
    "ExpiredTokenException",
    "InternalFailure",
    "ServiceUnavailable",
    "ThrottlingException",
    "UnrecognizedClientException",
}
_PYTHON_TYPES = {
    "string": (str,),
    "integer": (int,),
    "long": (int,),
    "float": (int, float),
    "double": (int, float),
    "boolean": (bool,),
}


class FakeAws:
    """A stand-in for `boto3.client` that checks every call against the SDK's service models.

        aws = FakeAws()
        monkeypatch.setattr(main.boto3, "client", aws.client)
        ivs = aws.client("ivs")
        ivs.respond("get_stream", {"stream": {"state": "LIVE"}})
        main.run(inputs)
        assert ivs.called("get_stream") == [{"channelArn": ARN}]

    A call fails the test when the service has no such operation, when a
    parameter is unknown, missing, of the wrong type or not one of the values
    the API lists, and when no response was set up for it. A mocked response
    fails the test when it has a field the API does not return, or a value of
    the wrong type. Responses may leave fields out.
    """

    def __init__(self):
        self._clients = {}

    def client(self, service, **options):
        """The fake client of `service`, the same one on every call. Pass this as `boto3.client`."""
        if service not in self._clients:
            self._clients[service] = FakeClient(service)
        self._clients[service].created_with.append(options)
        return self._clients[service]

    @staticmethod
    def error(code, message=None, status=400):
        """The error boto3 raises when AWS answers with `code`. Give it to respond() to have a call raise it."""
        from botocore.exceptions import ClientError

        response = {"Error": {"Code": code, "Message": message or code}, "ResponseMetadata": {"HTTPStatusCode": status}}
        return ClientError(response, "operation")


class FakeClient:
    """One service's fake client. Made by FakeAws.client()."""

    def __init__(self, service):
        import botocore.session
        from botocore import xform_name

        self.service = service
        self.created_with = []
        self.calls = []
        self._model = botocore.session.get_session().get_service_model(service)
        self._operations = {xform_name(name): name for name in self._model.operation_names}
        self._responses = {}

    def respond(self, method, *responses):
        """What `method` returns, call by call. The last response repeats.

        A response is a dict, an error from FakeAws.error() (raised), or a
        function that takes the call's parameters and returns either.
        """
        self._operation(method)
        if not responses:
            raise AssertionError(f"respond({method!r}) needs at least one response")
        self._responses[method] = list(responses)
        return self

    def called(self, method):
        """The parameters of every call made to `method`, in order."""
        self._operation(method)
        return [parameters for name, parameters in self.calls if name == method]

    def __getattr__(self, method):
        if method.startswith("_"):
            raise AttributeError(method)
        operation = self._operation(method)

        def call(*args, **parameters):
            if args:
                raise AssertionError(f"{self.service}.{method} takes keyword arguments only")
            problems = _request_problems(parameters, operation)
            if problems:
                raise AssertionError(f"{self.service}.{method} was called in a way the API does not accept:\n" + "\n".join(problems))
            self.calls.append((method, parameters))
            queue = self._responses.get(method)
            if not queue:
                raise AssertionError(f"{self.service}.{method} was called but the test set up no response for it")
            response = queue.pop(0) if len(queue) > 1 else queue[0]
            if callable(response):
                response = response(parameters)
            if isinstance(response, Exception):
                self._check_error(response, operation, method)
                raise response
            problems = _response_problems(response, operation.output_shape, "response")
            if problems:
                raise AssertionError(f"The mocked response of {self.service}.{method} is not what the API returns:\n" + "\n".join(problems))
            return response

        return call

    def _operation(self, method):
        if method not in self._operations:
            raise AssertionError(f"The {self.service} API has no operation {method!r} in the installed SDK")
        return self._model.operation_model(self._operations[method])

    def _check_error(self, error, operation, method):
        code = getattr(error, "response", {}).get("Error", {}).get("Code")
        modeled = {shape.name for shape in operation.error_shapes}
        if code and modeled and code not in modeled and code not in _COMMON_ERRORS:
            raise AssertionError(f"{self.service}.{method} cannot fail with {code!r}. It can fail with: {', '.join(sorted(modeled))}")


def _request_problems(parameters, operation):
    """Why the API would refuse these parameters: the SDK's own validation, plus listed values and patterns."""
    from botocore.validate import ParamValidator

    shape = operation.input_shape
    if shape is None:
        return [f"takes no parameters, got {sorted(parameters)}"] if parameters else []
    report = ParamValidator().validate(parameters, shape)
    if report.has_errors():
        return report.generate_report().splitlines()
    return _value_problems(parameters, shape, "request", check_patterns=True)


def _response_problems(value, shape, path):
    if shape is None:
        return [] if not value else [f"{path}: the operation returns nothing"]
    return _value_problems(value, shape, path, check_patterns=False)


def _value_problems(value, shape, path, check_patterns):
    """Fields the shape does not have, values of the wrong type, and strings the API does not list."""
    kind = shape.type_name
    if kind == "structure":
        if getattr(shape, "is_document_type", False):
            return []
        if not isinstance(value, dict):
            return [f"{path}: expected a structure, got {type(value).__name__}"]
        problems = []
        for key, item in value.items():
            if key == "ResponseMetadata" and path == "response":
                continue
            if key not in shape.members:
                problems.append(f"{path}.{key}: no such field. Fields: {', '.join(sorted(shape.members))}")
                continue
            problems += _value_problems(item, shape.members[key], f"{path}.{key}", check_patterns)
        return problems
    if kind == "list":
        if not isinstance(value, (list, tuple)):
            return [f"{path}: expected a list, got {type(value).__name__}"]
        return [p for index, item in enumerate(value) for p in _value_problems(item, shape.member, f"{path}[{index}]", check_patterns)]
    if kind == "map":
        if not isinstance(value, dict):
            return [f"{path}: expected a map, got {type(value).__name__}"]
        return [p for key, item in value.items() for p in _value_problems(item, shape.value, f"{path}[{key!r}]", check_patterns)]
    if kind == "timestamp":
        import datetime

        ok = isinstance(value, (datetime.datetime, str, int, float)) and not isinstance(value, bool)
        return [] if ok else [f"{path}: expected a timestamp, got {type(value).__name__}"]
    if kind == "blob":
        ok = isinstance(value, (bytes, bytearray, str)) or hasattr(value, "read")
        return [] if ok else [f"{path}: expected bytes or a stream, got {type(value).__name__}"]
    expected = _PYTHON_TYPES.get(kind)
    if expected and (not isinstance(value, expected) or (kind != "boolean" and isinstance(value, bool))):
        return [f"{path}: expected {kind}, got {type(value).__name__}"]
    if kind == "string":
        if getattr(shape, "enum", None) and value not in shape.enum:
            return [f"{path}: {value!r} is not one of {', '.join(shape.enum)}"]
        pattern = shape.metadata.get("pattern") if check_patterns else None
        if pattern and not _matches(pattern, value):
            return [f"{path}: {value!r} does not match {pattern}"]
    return []


def _matches(pattern, value):
    try:
        return re.fullmatch(pattern, value) is not None
    except re.error:
        # A few patterns in the service models use syntax Python's re does not have.
        return True
