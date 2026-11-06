"""Private profile storage and offline gateway metadata boundary contracts."""

import json
import stat
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from doonex.model_registry import (
    MetadataClient,
    ModelRegistry,
    ProfileConflict,
    RegistryError,
)

KEY = "FAKE_METADATA_KEY_94382"
TOKEN = "FAKE_CONTROL_TOKEN_49283"
DSN = "postgresql://fake:FAKE_DATABASE_SECRET@localhost/fake"


@pytest.fixture
def config(tmp_path, model):
    return SimpleNamespace(
        home=tmp_path / "private",
        model=model,
        token=TOKEN,
        database_url=DSN,
        credential=lambda _: KEY,
    )


@pytest.fixture
def profile(config):
    return ModelRegistry(config).save(
        "complex",
        "复杂任务",
        {**config.model.model_dump(), "base_url": "https://gateway.example/v1"},
        usage_adapter="gateway_v1",
    )


def test_legacy_profile_is_read_only_and_listing_does_not_write_or_read_credentials(
    config,
):
    def forbidden(_):
        pytest.fail("Listing model references must not load credentials")

    config.credential = forbidden
    registry = ModelRegistry(config)
    (legacy,) = registry.list()
    assert legacy.id == "legacy" and legacy.read_only
    assert legacy.model == config.model
    assert registry.get("legacy").revision == legacy.revision
    assert not config.home.exists()
    with pytest.raises(RegistryError, match="只读"):
        registry.save("legacy", "New", config.model)
    with pytest.raises(KeyError):
        registry.get("absent")


def test_profile_roundtrip_revision_and_private_atomic_storage(config):
    registry = ModelRegistry(config)
    created = registry.save("fast", "快速任务", config.model)
    original = created.model.model_dump()
    assert not created.read_only and len(created.revision) == 64
    assert registry.get("fast") == created
    for name in ("profiles.json", ".profiles.lock"):
        assert stat.S_IMODE((config.home / name).stat().st_mode) == 0o600
    raw = (config.home / "profiles.json").read_text()
    assert KEY not in raw and TOKEN not in raw and DSN not in raw
    assert json.loads(raw)["schema_version"] == 1
    assert created.model.credential_env in raw
    with pytest.raises(ProfileConflict):
        registry.save("fast", "Overwrite without revision", config.model)
    changed = registry.save(
        "fast",
        "快速任务更新",
        {**original, "model_id": "different-offline-model"},
        expected_revision=created.revision,
    )
    assert changed.revision != created.revision
    assert registry.get("fast") == changed
    assert created.model.model_dump() == original
    with pytest.raises(ProfileConflict):
        registry.save(
            "fast", "Stale edit", config.model, expected_revision=created.revision
        )
    with pytest.raises(ProfileConflict):
        registry.save(
            "new",
            "Wrong creation revision",
            config.model,
            expected_revision=created.revision,
        )


def test_two_editors_cannot_lose_a_concurrent_profile_edit(config):
    registry = ModelRegistry(config)
    created = registry.save("fast", "Initial", config.model)

    def edit(name):
        try:
            return registry.save(
                "fast", name, config.model, expected_revision=created.revision
            )
        except ProfileConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ("First editor", "Second editor")))
    winners = [profile for profile in results if profile is not None]
    assert len(winners) == 1
    assert registry.get("fast") == winners[0]


@pytest.mark.parametrize("name", ["profiles.json", ".profiles.lock"])
def test_profile_files_do_not_follow_symlinks(config, tmp_path, name):
    config.home.mkdir(mode=0o700)
    target = tmp_path / "sensitive.json"
    target.write_text(KEY)
    (config.home / name).symlink_to(target)
    with pytest.raises(RegistryError) as error:
        ModelRegistry(config).save("fast", "Safe", config.model)
    assert KEY not in str(error.value)
    assert target.read_text() == KEY
    assert (config.home / name).is_symlink()


def test_profile_home_symlink_is_rejected(config, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    config.home.symlink_to(target, target_is_directory=True)
    with pytest.raises(RegistryError):
        ModelRegistry(config).save("fast", "Safe", config.model)
    assert not (target / "profiles.json").exists()


def test_broad_permissions_or_corrupt_version_do_not_silently_reset_profiles(
    config, profile
):
    path = config.home / "profiles.json"
    original = path.read_bytes()
    path.chmod(0o644)
    with pytest.raises(RegistryError):
        ModelRegistry(config).list()
    with pytest.raises(RegistryError):
        ModelRegistry(config).save("new", "Safe", config.model)
    assert path.read_bytes() == original
    path.chmod(0o600)
    payload = json.loads(original)
    payload["schema_version"] = 2
    path.write_text(json.dumps(payload))
    with pytest.raises(RegistryError, match="版本"):
        ModelRegistry(config).list()


def test_invalid_model_fields_are_rejected_without_echoing_a_secret(config):
    with pytest.raises(RegistryError) as error:
        ModelRegistry(config).save(
            "fast", "Test", {**config.model.model_dump(), "api_key": KEY}
        )
    assert KEY not in str(error.value)
    assert not config.home.exists()


@pytest.mark.parametrize("profile_id", ["1fast", "../fast", "Fast", "模型"])
def test_profile_id_must_match_stable_api_identifier(config, profile_id):
    with pytest.raises(RegistryError):
        ModelRegistry(config).save(profile_id, "Test", config.model)
    assert not config.home.exists()


def test_atomic_replace_failure_retains_previous_revision_and_cleans_temporary_file(
    config, profile, monkeypatch
):
    import doonex.model_registry as module

    path = config.home / "profiles.json"
    original = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("Simulated disk error")

    monkeypatch.setattr(module.os, "replace", fail)
    with pytest.raises(RegistryError):
        ModelRegistry(config).save(
            "complex", "Changed", config.model, expected_revision=profile.revision
        )
    assert path.read_bytes() == original
    assert list(config.home.glob(".profiles-*")) == []


def test_explicit_model_discovery_keeps_origin_and_disables_environment_proxy(
    config, profile, monkeypatch
):
    import doonex.model_registry as module

    requests, options = [], []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert str(request.url) == "https://gateway.example/v1/models"
        assert request.headers["Authorization"] == "Bearer " + KEY
        assert request.extensions["timeout"]["read"] == 10
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "model-b"},
                    {"id": "model-a", "unknown": KEY},
                    {"id": "model-b"},
                ]
            },
        )

    real_client = httpx.Client

    def client(**kwargs):
        options.append(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr(module.httpx, "Client", client)
    monkeypatch.setenv("HTTPS_PROXY", "http://invalid-proxy.example:9")
    result = MetadataClient(config, transport=httpx.MockTransport(handler)).discover(
        profile
    )
    assert result["status"] == "ok" and result["models"] == ["model-b", "model-a"]
    assert result["profile_id"] == profile.id and result["revision"] == profile.revision
    assert result["source"] == str(requests[0].url) and result["queried_at"]
    assert len(requests) == 1
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False
    assert KEY not in json.dumps(result)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "unauthorized"),
        (403, "unauthorized"),
        (429, "rate_limited"),
        (404, "unsupported"),
        (302, "unavailable"),
        (500, "unavailable"),
    ],
)
def test_metadata_http_errors_and_redirects_return_no_body_or_credentials(
    config, profile, status, expected
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            text="Provider says " + KEY,
            headers={"Location": "https://other.example/" + KEY},
        )

    result = MetadataClient(config, transport=httpx.MockTransport(handler)).usage(
        profile
    )
    assert result["status"] == expected and result["usage"] is None
    assert result["http_status"] == status
    assert len(requests) == 1
    assert KEY not in json.dumps(result) and "Provider says" not in json.dumps(result)


def test_usage_preserves_zero_unrestricted_marker_and_separate_windows(config, profile):
    payload = {
        "isValid": True,
        "mode": "unrestricted",
        "planName": "Plan " + KEY,
        "remaining": 0,
        "unit": "USD",
        "usage": {
            "todayTokens": 0,
            "total_requests": 3,
            "rpm": 0,
            "unknownPrivateField": KEY,
        },
        "windows": [
            {
                "name": "daily",
                "used": 0,
                "limit": 0,
                "remaining": 0,
                "reset_at": "2026-11-07T00:00:00Z",
                "unit": "tokens",
            },
            {"name": "monthly", "used": 4, "remaining": None, "reset_at": None},
        ],
        "daily_usage": [{"raw": KEY}],
        "model_stats": {KEY: "not passed through"},
    }
    result = MetadataClient(
        config,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    ).usage(profile)
    assert result["status"] == "ok", result
    usage = result["usage"]
    assert usage["remaining"] == 0 and usage["unrestricted"] is True
    assert usage["metrics"] == {"today_tokens": 0, "total_requests": 3, "rpm": 0}
    assert usage["windows"][0]["used"] == usage["windows"][0]["remaining"] == 0
    assert usage["windows"][0]["reset_at"] == "2026-11-07T00:00:00+00:00"
    assert usage["windows"][1]["limit"] is None
    assert "[REDACTED]" in usage["plan_name"]
    assert KEY not in json.dumps(result)
    assert "daily_usage" not in usage and "model_stats" not in usage


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"remaining": True},
        {"remaining": -1},
        {"remaining": "0"},
        {"isValid": "true"},
        {"usage": {"tokens": False}},
        {"usage": []},
        {"mode": "x", "windows": [{"name": "bad", "reset_at": "2026-11-07T00:00:00"}]},
        {"mode": "x", "windows": [{}]},
        {"mode": "x", "windows": [None]},
    ],
)
def test_usage_schema_failure_is_unknown_not_zero_or_raw_data(config, profile, payload):
    result = MetadataClient(
        config,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    ).usage(profile)
    assert result["status"] == "invalid_response" and result["usage"] is None


@pytest.mark.parametrize("payload", [b"not json", b'{"remaining":NaN}', b'{"data":{}}'])
def test_bad_json_or_model_schema_is_safely_rejected(config, profile, payload):
    result = MetadataClient(
        config,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload)),
    ).discover(profile)
    assert result["status"] == "invalid_response" and result["models"] == []


def test_empty_model_list_is_valid_and_missing_usage_remains_unknown(config, profile):
    empty = MetadataClient(
        config,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": []})),
    ).discover(profile)
    assert empty["status"] == "ok" and empty["models"] == []
    unknown = MetadataClient(
        config,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"mode": "limited"})
        ),
    ).usage(profile)
    assert unknown["status"] == "ok"
    assert unknown["usage"]["remaining"] is None
    assert (
        unknown["usage"]["metrics"] == {} and unknown["usage"]["unrestricted"] is False
    )


def test_response_stream_size_and_encoding_are_bounded(config, profile):
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield b"{" * 64
            yield b"}" * 64

    oversized = MetadataClient(
        config,
        max_bytes=64,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Chunks())),
    ).usage(profile)
    assert oversized["status"] == "invalid_response"
    encoded = MetadataClient(
        config,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"Content-Encoding": "gzip"}, stream=Chunks()
            )
        ),
    ).usage(profile)
    assert encoded["status"] == "invalid_response"


def test_timeout_and_echoed_control_secrets_do_not_escape(config, profile):
    def timeout(_):
        raise httpx.ReadTimeout("Cannot connect with " + KEY)

    failed = MetadataClient(config, transport=httpx.MockTransport(timeout)).usage(
        profile
    )
    assert failed["status"] == "unavailable"
    assert KEY not in json.dumps(failed)
    echoed = MetadataClient(
        config,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"mode": "limited", "planName": f"{KEY} {TOKEN} {DSN}"}
            )
        ),
    ).usage(profile)
    text = json.dumps(echoed)
    assert echoed["status"] == "ok" and "[REDACTED]" in text
    assert KEY not in text and TOKEN not in text and DSN not in text


def test_queries_require_explicit_adapter_origin_and_credentials(config, profile):
    def forbidden(_):
        pytest.fail("Unconfigured metadata must not dispatch a request")

    client = MetadataClient(config, transport=httpx.MockTransport(forbidden))
    assert (
        client.usage(profile.model_copy(update={"usage_adapter": "none"}))["status"]
        == "not_configured"
    )
    for base_url in (
        None,
        "https://gateway.example/custom/v1",
        "http://remote.example/v1",
    ):
        candidate = profile.model_copy(
            update={"model": profile.model.model_copy(update={"base_url": base_url})}
        )
        assert client.discover(candidate)["status"] == "unsupported"
    config.credential = lambda _: None
    assert client.discover(profile)["status"] == "not_configured"


def test_model_ids_cannot_echo_credentials_and_numeric_aliases_cannot_conflict(
    config, profile
):
    echoed = MetadataClient(
        config,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"data": [{"id": KEY}]})
        ),
    ).discover(profile)
    assert echoed["status"] == "invalid_response" and echoed["models"] == []
    assert KEY not in json.dumps(echoed)
    aliases = MetadataClient(
        config,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"usage": {"today_tokens": 1, "todayTokens": 2}}
            )
        ),
    ).usage(profile)
    assert aliases["status"] == "invalid_response" and aliases["usage"] is None


def test_slow_dripping_body_hits_cumulative_deadline(config, profile, monkeypatch):
    import doonex.model_registry as module

    current = 0.0

    class SlowChunks(httpx.SyncByteStream):
        def __iter__(self):
            nonlocal current
            current = 4.0
            yield b'{"mode":'
            current = 11.0
            yield b'"unrestricted"}'

    monkeypatch.setattr(module.time, "monotonic", lambda: current)
    result = MetadataClient(
        config,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=SlowChunks())
        ),
    ).usage(profile)
    assert result["status"] == "unavailable" and result["usage"] is None
