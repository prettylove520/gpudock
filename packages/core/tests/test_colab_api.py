import json

import httpx
import pytest
import respx

from gpudock_core.colab_api import ColabApi, SpecKey, parse_time
from gpudock_core.errors import ColabApiError

ROOT = "https://colaboratory.googleapis.com/"
L4 = SpecKey("VARIANT_GPU", "L4", "SHAPE_STANDARD")
RUNTIME = {
    "name": "runtimes/abc",
    "runtimeSpec": L4.to_json(),
    "connectionInfo": {"url": "https://rt.example/tun/m/xyz/", "token": "secret-proxy-token", "expireTime": "2026-11-25T10:00:00.123456789Z"},
}


def make_api(sleeps=None):
    return ColabApi(lambda: {"authorization": "Bearer t"}, sleep=(sleeps.append if sleeps is not None else lambda s: None))


@respx.mock
def test_list_specs_and_subscription():
    respx.get(ROOT + "v1beta/runtimespecs").respond(
        json={
            "runtimeSpecs": [
                {"key": {"variant": "VARIANT_GPU", "accelerator": "T4", "shape": "SHAPE_STANDARD"}, "eligible": True},
                {"key": {"variant": "VARIANT_GPU", "accelerator": "A100", "shape": "SHAPE_HIGHMEM"}, "eligible": False},
                {"key": {"variant": "VARIANT_CPU", "accelerator": "NONE", "shape": "SHAPE_STANDARD"}},
            ]
        }
    )
    respx.get(ROOT + "v1beta/subscription").respond(json={"name": "subscription", "tier": "SUBSCRIPTION_TIER_FREE"})
    api = make_api()
    specs = api.list_specs()
    assert [s.key.label for s in specs] == ["T4", "A100 (HIGHMEM)", "CPU"]
    assert [s.eligible for s in specs] == [True, False, False]
    assert api.get_subscription() == "SUBSCRIPTION_TIER_FREE"


@respx.mock
def test_create_runtime_waits_and_fetches_connection():
    create = respx.post(ROOT + "v1beta/runtimes").respond(json={"name": "operations/op1", "done": False})
    wait = respx.get(ROOT + "v1/operations/op1:wait").mock(
        side_effect=[
            httpx.Response(200, json={"name": "operations/op1", "done": False}),
            httpx.Response(200, json={"name": "operations/op1", "done": True, "response": {"name": "runtimes/abc", "runtimeSpec": L4.to_json()}}),
        ]
    )
    get = respx.get(ROOT + "v1beta/runtimes/abc").respond(json=RUNTIME)

    rt = make_api().create_runtime(L4, runtime_id="gpudock-m0-1234", request_id="req-1")

    sent = create.calls[0].request
    assert sent.url.params["requestId"] == "req-1"
    assert sent.url.params["runtimeId"] == "gpudock-m0-1234"
    assert json.loads(sent.content) == {"runtimeSpec": L4.to_json()}
    assert wait.call_count == 2 and get.called
    assert rt.id == "abc" and rt.spec == L4
    assert rt.connection.url.startswith("https://rt.example/")
    assert rt.connection.expire_time.year == 2026
    assert "secret-proxy-token" not in repr(rt.connection)


@respx.mock
def test_create_runtime_operation_error_carries_reason():
    respx.post(ROOT + "v1beta/runtimes").respond(
        json={
            "name": "operations/op2",
            "done": True,
            "error": {
                "code": 9,
                "message": "Accelerator unavailable",
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "TOO_MANY_ACTIVE_RUNTIMES", "domain": "colaboratory.googleapis.com"}
                ],
            },
        }
    )
    with pytest.raises(ColabApiError) as info:
        make_api().create_runtime(L4)
    err = info.value
    assert err.status == "FAILED_PRECONDITION"
    assert err.reason == "TOO_MANY_ACTIVE_RUNTIMES"
    assert "活跃运行时" in err.hint


@respx.mock
def test_http_error_body_is_parsed_with_hint():
    respx.get(ROOT + "v1beta/runtimes").respond(
        403,
        json={
            "error": {
                "code": 403,
                "status": "PERMISSION_DENIED",
                "message": "Colab API has not been used in project 123",
                "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED", "metadata": {"service": "colaboratory.googleapis.com"}}],
            }
        },
    )
    with pytest.raises(ColabApiError) as info:
        make_api().list_runtimes()
    assert info.value.http_status == 403
    assert info.value.reason == "SERVICE_DISABLED"
    assert "启用" in str(info.value)


@respx.mock
def test_rate_limit_is_retried_with_backoff():
    sleeps = []
    route = respx.get(ROOT + "v1beta/runtimes").mock(
        side_effect=[
            httpx.Response(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "slow down"}}),
            httpx.Response(503, text="unavailable"),
            httpx.Response(200, json={"runtimes": [RUNTIME]}),
        ]
    )
    runtimes = make_api(sleeps).list_runtimes()
    assert route.call_count == 3 and len(sleeps) == 2 and sleeps[1] > sleeps[0] - 1
    assert runtimes[0].name == "runtimes/abc"


@respx.mock
def test_delete_runtime_uses_allow_missing():
    route = respx.delete(ROOT + "v1beta/runtimes/abc").respond(json={})
    make_api().delete_runtime("abc")
    assert route.calls[0].request.url.params["allowMissing"] == "true"


def test_parse_time_handles_nanos_and_z():
    t = parse_time("2026-11-25T10:00:00.123456789Z")
    assert t.microsecond == 123456 and t.utcoffset().total_seconds() == 0
    assert parse_time(None) is None
