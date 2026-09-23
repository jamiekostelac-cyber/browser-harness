"""Tab guard: an unattended run may act only on tabs it opened (BH_TAB_GUARD=1)."""
import asyncio
import json
import os
import pathlib
import subprocess
import sys

import pytest

from browser_harness import daemon, helpers


FOREIGN = {"targetId": "FOREIGN", "url": "https://mail.example.com/", "title": "Inbox"}
RUN_ID = "123e4567-e89b-42d3-a456-426614174000"
RUN_ID_2 = "123e4567-e89b-42d3-a456-426614174001"
RUN_ID_3 = "123e4567-e89b-42d3-a456-426614174002"


def _fake_send(current=FOREIGN, created="MINE", session="SESSION-MINE", target_type="page"):
    def send(req, response_timeout=None):
        if req.get("meta") == "guard_context":
            return {"target_id": current["targetId"], "session_id": session, "url": current.get("url", "")}
        if req.get("meta") == "current_tab":
            return {**current, "tab_guard": "ok"}
        method = req.get("method")
        if method == "Target.createBrowserContext":
            return {"result": {"browserContextId": "CONTEXT-MINE"}}
        if method == "Target.createTarget":
            return {"result": {"targetId": created}}
        if method == "Target.attachToTarget":
            return {"result": {"sessionId": session}}
        if method == "Target.getTargetInfo":
            return {"result": {"targetInfo": {"type": target_type, "url": current.get("url", "")}}}
        return {"result": {}}
    return send


@pytest.fixture
def guard(tmp_path, monkeypatch):
    """Guard on, ownership isolated to tmp_path, daemon attached to a tab this
    run did not open."""
    monkeypatch.setenv("BH_TAB_GUARD", "1")
    monkeypatch.setenv("BH_TAB_GUARD_RUN", RUN_ID)
    monkeypatch.delenv("BH_TAB_GUARD_LOG", raising=False)
    monkeypatch.setattr(helpers.ipc, "_TMP", tmp_path)
    helpers.tab_guard_reset()
    monkeypatch.setattr(helpers, "_send", _fake_send())


@pytest.fixture
def owning(guard, monkeypatch):
    """As `guard`, but the run has opened a tab and the daemon is attached to it."""
    assert helpers.cdp("Target.createTarget", url="about:blank")["targetId"] == "MINE"
    helpers.cdp("Target.attachToTarget", targetId="MINE", flatten=True)
    monkeypatch.setattr(helpers, "_send", _fake_send(current={"targetId": "MINE", "url": "https://example.com/", "title": "t"}))


# --- refusals -------------------------------------------------------------

def test_refuses_navigating_a_tab_the_run_did_not_open(guard, capsys):
    with pytest.raises(helpers.TabGuardRefused):
        helpers.goto_url("https://example.com/")
    assert "[tab-guard] REFUSED Page.navigate FOREIGN https://example.com/" in capsys.readouterr().err


def test_refuses_closing_and_activating_someone_elses_tab(guard):
    for method in ("Target.closeTarget", "Target.activateTarget", "Target.attachToTarget"):
        with pytest.raises(helpers.TabGuardRefused):
            helpers.cdp(method, targetId="FOREIGN")


@pytest.mark.parametrize("call,label", [
    (lambda: helpers.js("location.href='https://example.com'"), "js navigation"),
    (lambda: helpers.js("document.querySelector('button').click()"), "js click"),
    (lambda: helpers.page_info(), "page_info"),
    (lambda: helpers.capture_screenshot(), "screenshot"),
    (lambda: helpers.type_text("hello"), "type_text"),
    (lambda: helpers.press_key("Enter"), "press_key"),
    (lambda: helpers.click_at_xy(10, 10), "click_at_xy"),
    (lambda: helpers.scroll(0, 0), "scroll"),
    (lambda: helpers.cdp("DOM.setFileInputFiles", files=["/etc/passwd"]), "DOM.setFileInputFiles"),
    (lambda: helpers.cdp("Page.setDocumentContent", html="<h1>x</h1>"), "Page.setDocumentContent"),
])
def test_refuses_every_session_scoped_call_on_a_foreign_tab(guard, call, label):
    """The reason this is an allowlist. A blocklist of "mutating" methods misses
    Runtime.evaluate, and js() is the ordinary fallback for a blocked click —
    so the guard would look present while being trivially bypassable."""
    with pytest.raises(helpers.TabGuardRefused):
        call()


def test_fails_closed_when_the_attached_tab_cannot_be_read(tmp_path, monkeypatch):
    """"Unknown" must not mean "permitted": treating an unreadable current tab
    as nothing-to-refuse turns any daemon hiccup into a bypass."""
    monkeypatch.setenv("BH_TAB_GUARD", "1")
    monkeypatch.setenv("BH_TAB_GUARD_RUN", RUN_ID)
    monkeypatch.setattr(helpers.ipc, "_TMP", tmp_path)
    helpers.tab_guard_reset()

    def flaky(req, response_timeout=None):
        if req.get("meta") == "guard_context":
            raise RuntimeError("daemon unreachable")
        return {"result": {}}

    monkeypatch.setattr(helpers, "_send", flaky)
    with pytest.raises(helpers.TabGuardRefused) as exc:
        helpers.goto_url("https://example.com/")
    assert "failing closed" in str(exc.value)


# --- what must still work -------------------------------------------------

def test_allows_everything_on_a_tab_the_run_opened(owning):
    helpers.goto_url("https://example.com/page")
    helpers.js("document.title")
    helpers.type_text("hi")
    helpers.cdp("Target.closeTarget", targetId="MINE")


@pytest.mark.parametrize("method", [
    "Emulation.setEmulatedMedia", "Network.enable", "Page.bringToFront", "Page.reload",
])
def test_owned_page_scope_allowlist_remains_allowed(owning, method):
    helpers.cdp(method)


def test_allows_enumerating_tabs_without_attaching(guard):
    """Refusing reads on a foreign tab costs a run nothing, because
    Target.getTargets still answers "what else is open?"."""
    helpers.cdp("Target.getTargets")
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp("Target.getTargetInfo", targetId="FOREIGN")


@pytest.mark.parametrize("method,params", [
    ("Target.createTarget", {"url": "chrome://settings"}),
    ("Target.createTarget", {"url": "devtools://devtools/bundled/inspector.html"}),
    ("Target.createTarget", {"url": "browser://settings"}),
    ("Target.createTarget", {"url": "javascript:alert(1)"}),
    ("Target.createTarget", {"url": "file:///etc/passwd"}),
    ("Target.createTarget", {"url": "data:text/html,<h1>x</h1>"}),
    ("Target.createTarget", {"url": "ftp://example.com/file"}),
    ("Page.navigate", {"url": "chrome://settings"}),
    ("Page.navigate", {"url": "devtools://devtools/bundled/inspector.html"}),
    ("Page.navigate", {"url": "browser://settings"}),
    ("Page.navigate", {"url": "javascript:alert(1)"}),
    ("Page.navigate", {"url": "file:///etc/passwd"}),
    ("Page.navigate", {"url": "data:text/html,<h1>x</h1>"}),
    ("Page.navigate", {"url": "ftp://example.com/file"}),
])
def test_guard_rejects_privileged_urls_before_transport(owning, monkeypatch, method, params):
    calls = []
    monkeypatch.setattr(helpers, "_send", lambda req, **kwargs: calls.append(req))
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp(method, **params)
    assert calls == []


@pytest.mark.parametrize("url", [
    "chrome://settings", "devtools://devtools/bundled/inspector.html",
    "browser://settings", "javascript:alert(1)", "file:///etc/passwd",
    "data:text/html,<h1>x</h1>", "ftp://example.com/file",
])
def test_nested_navigation_rejects_privileged_urls_before_transport(owning, monkeypatch, url):
    calls = []
    monkeypatch.setattr(helpers, "_send", lambda req, **kwargs: calls.append(req))
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp(
            "Target.sendMessageToTarget",
            sessionId="SESSION-MINE",
            message=json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": url}}),
        )
    assert calls == []


def test_guard_rejects_interaction_with_privileged_current_target(owning, monkeypatch):
    calls = []
    fake = _fake_send(current={"targetId": "MINE", "url": "chrome://settings"})
    def send(req, **kwargs):
        calls.append(req)
        return fake(req, **kwargs)
    monkeypatch.setattr(helpers, "_send", send)
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp("Runtime.evaluate", expression="document.title")
    assert not any(req.get("method") for req in calls)


@pytest.mark.parametrize("nested", [False, True])
def test_guard_rejects_explicit_owned_session_on_privileged_current_target(owning, monkeypatch, nested):
    calls = []
    fake = _fake_send(current={"targetId": "MINE", "url": "chrome://settings"})
    def send(req, **kwargs):
        calls.append(req)
        return fake(req, **kwargs)
    monkeypatch.setattr(helpers, "_send", send)
    with pytest.raises(helpers.TabGuardRefused):
        if nested:
            helpers.cdp(
                "Target.sendMessageToTarget",
                sessionId="SESSION-MINE",
                message=json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": "1"}}),
            )
        else:
            helpers.cdp("Runtime.evaluate", session_id="SESSION-MINE", expression="document.title")
    assert not any(req.get("method") for req in calls)


@pytest.mark.parametrize("nested", [False, True])
def test_guard_rejects_explicit_owned_session_mismatch_before_dispatch(owning, monkeypatch, nested):
    calls = []
    fake = _fake_send(current={"targetId": "MINE", "url": "https://current.example/"}, session="OTHER-SESSION")
    def send(req, **kwargs):
        calls.append(req)
        return fake(req, **kwargs)
    monkeypatch.setattr(helpers, "_send", send)
    with pytest.raises(helpers.TabGuardRefused, match="does not match"):
        if nested:
            helpers.cdp(
                "Target.sendMessageToTarget",
                sessionId="SESSION-MINE",
                message=json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": "https://next.example/"}}),
            )
        else:
            helpers.cdp("Page.navigate", session_id="SESSION-MINE", url="https://next.example/")
    assert not any(req.get("method") for req in calls)


def test_refuses_non_page_targets_without_owned_ancestry(owning, monkeypatch):
    monkeypatch.setattr(helpers, "_send", _fake_send(
        current={"targetId": "MINE"}, target_type="iframe"))
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp("Target.attachToTarget", targetId="FOREIGN-IFRAME", flatten=True)


def test_allows_a_session_this_run_attached_and_refuses_one_it_did_not(owning):
    sid = helpers.cdp("Target.attachToTarget", targetId="MINE", flatten=True)["sessionId"]
    helpers.cdp("Runtime.evaluate", session_id=sid, expression="1")  # ours: fine
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp("Runtime.evaluate", session_id="SOMEONE-ELSES", expression="1")


def test_new_tab_does_not_reuse_a_blank_tab_the_run_does_not_own(guard):
    """Upstream new_tab() navigates the attached tab when it is blank. A blank
    tab is still someone's tab, so under the guard a fresh one is always made."""
    assert helpers._may_reuse_attached_tab() is False


def test_new_tab_may_still_reuse_a_blank_tab_the_run_opened(owning):
    assert helpers._may_reuse_attached_tab() is True


def test_guard_off_by_default_leaves_every_tab_reachable(tmp_path, monkeypatch):
    monkeypatch.delenv("BH_TAB_GUARD", raising=False)
    monkeypatch.setattr(helpers.ipc, "_TMP", tmp_path)
    monkeypatch.setattr(helpers, "_send", _fake_send())
    helpers.goto_url("https://example.com/")
    helpers.js("document.title")
    assert helpers._may_reuse_attached_tab() is True


# --- ownership bookkeeping ------------------------------------------------

def test_ownership_is_scoped_to_the_run_id(guard, monkeypatch):
    monkeypatch.setenv("BH_TAB_GUARD_RUN", RUN_ID)
    helpers.cdp("Target.createTarget", url="about:blank")
    assert helpers._owned_ids() == {"MINE"}
    monkeypatch.setenv("BH_TAB_GUARD_RUN", RUN_ID_2)
    assert helpers._owned_ids() == set()
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp("Target.closeTarget", targetId="MINE")


def test_a_concurrent_run_cannot_consume_this_run_s_ownership(guard, monkeypatch):
    """Two harness users commonly share a daemon name (the default is
    "default"). A single shared state file let one overwrite the other's tabs,
    which refuses a run on its own tab mid-task."""
    helpers.cdp("Target.createTarget", url="about:blank")
    assert helpers._owned_ids() == {"MINE"}
    monkeypatch.setenv("BH_TAB_GUARD_RUN", "")   # a different (unguarded) user
    monkeypatch.delenv("BH_TAB_GUARD")
    helpers._own_tab("THEIRS")
    monkeypatch.setenv("BH_TAB_GUARD", "1")
    monkeypatch.setenv("BH_TAB_GUARD_RUN", RUN_ID)
    assert helpers._owned_ids() == {"MINE"}


def test_ownership_crosses_a_process_boundary(guard, tmp_path):
    """One run is usually several harness invocations, so ownership must survive
    a real process exit — which an in-process assertion cannot demonstrate."""
    helpers.cdp("Target.createTarget", url="about:blank")
    script = (
        "import pathlib, json\n"
        "from browser_harness import helpers\n"
        f"helpers.ipc._TMP = pathlib.Path({str(tmp_path)!r})\n"
        "print(json.dumps(sorted(helpers._owned_ids())))\n"
    )
    env = {**os.environ, "BH_TAB_GUARD": "1", "BH_TAB_GUARD_RUN": RUN_ID}
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == ["MINE"]


def test_a_failed_ownership_write_is_reported_not_swallowed(guard, monkeypatch, capsys):
    """Silence here would present a disk problem as a guard bug: the run would
    be refused on tabs it did open, with nothing saying why."""
    monkeypatch.setattr(helpers, "_owned_path", lambda: pathlib.Path("/nonexistent-dir/owned.json"))
    helpers._own_tab("MINE")
    assert "[tab-guard] WARNING could not record ownership of MINE" in capsys.readouterr().err


def test_corrupt_ownership_state_reads_as_empty_rather_than_crashing(guard):
    helpers._owned_path().write_text("{not json")
    assert helpers._owned_ids() == set()
    helpers._owned_path().write_text('["legacy-list-form"]')
    assert helpers._owned_ids() == set()


# --- refusal reporting ----------------------------------------------------

def test_refusal_is_appended_to_BH_TAB_GUARD_LOG(guard, tmp_path, monkeypatch):
    """A supervisor counting refusals usually cannot see this process's stderr —
    it is a grandchild whose output is captured by whatever spawned it."""
    log = tmp_path / "run.log"
    monkeypatch.setenv("BH_TAB_GUARD_LOG", str(log))
    with pytest.raises(helpers.TabGuardRefused):
        helpers.goto_url("https://example.com/")
    assert "[tab-guard] REFUSED Page.navigate FOREIGN https://example.com/" in log.read_text()


def test_an_unwritable_log_does_not_swallow_the_refusal(guard, monkeypatch):
    monkeypatch.setenv("BH_TAB_GUARD_LOG", "/nonexistent-dir/run.log")
    with pytest.raises(helpers.TabGuardRefused):
        helpers.goto_url("https://example.com/")


@pytest.mark.parametrize("run_id", [None, "", " \t\n", "test-run", "job-42"])
def test_guard_requires_nonempty_run_before_any_dispatch(guard, monkeypatch, run_id):
    if run_id is None:
        monkeypatch.delenv("BH_TAB_GUARD_RUN")
    else:
        monkeypatch.setenv("BH_TAB_GUARD_RUN", run_id)
    calls = []
    monkeypatch.setattr(helpers, "_send", lambda req, **kw: calls.append(req))
    for method in ("Target.createTarget", "Target.getTargets", "Runtime.evaluate"):
        with pytest.raises(helpers.TabGuardRefused, match="BH_TAB_GUARD_RUN"):
            helpers.cdp(method)
    assert calls == []


@pytest.mark.parametrize("first,second", [(RUN_ID, RUN_ID_2), (RUN_ID, RUN_ID_3)])
def test_distinct_valid_run_ids_cannot_share_ownership(guard, monkeypatch, first, second):
    monkeypatch.setenv("BH_TAB_GUARD_RUN", first)
    helpers._own_tab("MINE")
    first_path = helpers._owned_path()
    monkeypatch.setenv("BH_TAB_GUARD_RUN", second)
    assert helpers._owned_path() != first_path
    assert helpers._owned_ids() == set()


@pytest.mark.parametrize("run_id", [
    "test-run", "job-42", "12345678-1234-1234-1234-123456789abc",
    "123e4567-e89b-42d3-0456-426614174000",
])
def test_low_entropy_run_ids_are_rejected_before_ownership_path_access(guard, monkeypatch, run_id):
    monkeypatch.setenv("BH_TAB_GUARD_RUN", run_id)
    monkeypatch.setattr(helpers, "_owned_path", lambda: pytest.fail("invalid run IDs must not resolve a path"))
    with pytest.raises(helpers.TabGuardRefused, match="canonical UUID4"):
        helpers.cdp("Target.getTargets")


def test_daemon_names_are_part_of_ownership_key_even_in_custom_tmp(guard, monkeypatch):
    monkeypatch.setattr(helpers.ipc, "BH_TMP_DIR", "custom")
    monkeypatch.setattr(helpers.ipc, "BH_TMP_DIR_SHARED", False)
    helpers._own_tab("MINE")
    monkeypatch.setattr(helpers, "NAME", "another-daemon")
    assert helpers._owned_ids() == set()


def test_detach_checks_parameter_session_and_forgets_it(owning):
    helpers.cdp("Target.detachFromTarget", sessionId="SESSION-MINE")
    assert "SESSION-MINE" not in helpers._owned_sessions()
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp("Runtime.evaluate", session_id="SESSION-MINE", expression="1")


def test_create_and_attach_record_new_ownership(guard):
    assert helpers._owned_ids() == set()
    assert helpers._owned_sessions() == set()
    assert helpers.cdp("Target.createTarget")["targetId"] == "MINE"
    assert helpers._owned_ids() == {"MINE"}
    assert helpers.cdp("Target.attachToTarget", targetId="MINE")["sessionId"] == "SESSION-MINE"
    assert helpers._owned_sessions() == {"SESSION-MINE"}


def test_create_target_is_pinned_to_a_run_owned_browser_context(guard, monkeypatch):
    requests = []
    original = helpers._send

    def send(req, **kwargs):
        requests.append(req)
        return original(req, **kwargs)

    monkeypatch.setattr(helpers, "_send", send)
    result = helpers.cdp("Target.createTarget", url="about:blank")
    assert result["targetId"] == "MINE"
    assert helpers._owned_contexts() == {"CONTEXT-MINE"}
    create = next(req for req in requests if req.get("method") == "Target.createTarget")
    assert create["params"]["browserContextId"] == "CONTEXT-MINE"


@pytest.mark.parametrize("context_id", ["FOREIGN-CONTEXT", None])
def test_create_target_rejects_a_foreign_or_default_context_before_dispatch(
    owning, monkeypatch, context_id
):
    calls = []
    monkeypatch.setattr(helpers, "_send", lambda req, **kwargs: calls.append(req) or {"result": {}})
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp("Target.createTarget", url="about:blank", browserContextId=context_id)
    assert calls == []


def test_nested_create_target_cannot_bypass_context_ownership(owning, monkeypatch):
    calls = []
    monkeypatch.setattr(helpers, "_send", lambda req, **kwargs: calls.append(req) or {"result": {}})
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp(
            "Target.sendMessageToTarget",
            sessionId="SESSION-MINE",
            message=json.dumps({
                "id": 1,
                "method": "Target.createTarget",
                "params": {"url": "about:blank", "browserContextId": "FOREIGN-CONTEXT"},
            }),
        )
    assert calls == []


def test_removal_is_noop_only_when_value_is_absent(guard):
    helpers._own_tab("MINE")
    helpers._remember("tabs", "MINE", remove=True)
    assert helpers._owned_ids() == set()
    helpers._remember("tabs", "MINE", remove=True)
    assert helpers._owned_ids() == set()


def test_addition_is_noop_only_when_value_is_present(guard):
    helpers._own_tab("MINE")
    helpers._own_tab("MINE")
    assert helpers._owned_ids() == {"MINE"}


@pytest.mark.parametrize("params", [
    {}, {"sessionId": "FOREIGN"}, {"targetId": "MINE"},
    {"sessionId": "FOREIGN", "targetId": "MINE"},
])
@pytest.mark.parametrize("method", ["Target.detachFromTarget", "Target.sendMessageToTarget"])
def test_routing_params_cannot_borrow_current_or_explicit_session(owning, params, method):
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp(method, session_id="SESSION-MINE", **params)


def test_send_message_to_owned_session_works(owning):
    helpers.cdp("Target.sendMessageToTarget", sessionId="SESSION-MINE",
                message=json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": "1"}}))


def test_nested_message_cannot_route_to_foreign_target(owning):
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp("Target.sendMessageToTarget", sessionId="SESSION-MINE",
                    message=json.dumps({"id": 1, "method": "Target.attachToTarget", "params": {"targetId": "FOREIGN"}}))


@pytest.mark.parametrize("method", [
    "Target.exposeDevToolsProtocol",
    "ServiceWorker.enable",
    "Storage.trackIndexedDBForOrigin",
    "Network.getAllCookies",
    "Network.clearBrowserCookies",
    "Network.clearBrowserCache",
    "Network.getCookies",
    "Network.setCookies",
    "Storage.getCookies",
    "Storage.clearDataForOrigin",
    "Browser.getVersion",
    "SystemInfo.getProcessInfo",
])
def test_guard_refuses_browser_and_context_wide_methods_before_dispatch(owning, monkeypatch, method):
    calls = []
    original = helpers._send
    def send(req, **kwargs):
        calls.append(req)
        return original(req, **kwargs)
    monkeypatch.setattr(helpers, "_send", send)
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp(method, targetId="MINE")
    assert calls == []


@pytest.mark.parametrize("method", [
    "Network.getAllCookies", "Network.clearBrowserCookies",
    "ServiceWorker.enable", "Storage.trackIndexedDBForOrigin",
    "Target.exposeDevToolsProtocol",
])
def test_nested_message_applies_browser_scope_policy_before_dispatch(owning, monkeypatch, method):
    calls = []
    monkeypatch.setattr(helpers, "_send", lambda req, **kwargs: calls.append(req) or {"result": {}})
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp(
            "Target.sendMessageToTarget",
            sessionId="SESSION-MINE",
            message=json.dumps({"id": 1, "method": method, "params": {}}),
        )
    assert calls == []


def test_create_isolated_world_is_not_guarded_page_scope(owning, monkeypatch):
    calls = []
    monkeypatch.setattr(helpers, "_send", lambda req, **kwargs: calls.append(req) or {"result": {}})
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp("Page.createIsolatedWorld", frameId="frame", grantUniveralAccess=True)
    assert calls == []


def test_nested_create_isolated_world_is_not_guarded_page_scope(owning, monkeypatch):
    calls = []
    monkeypatch.setattr(helpers, "_send", lambda req, **kwargs: calls.append(req) or {"result": {}})
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp(
            "Target.sendMessageToTarget",
            sessionId="SESSION-MINE",
            message=json.dumps({
                "id": 1,
                "method": "Page.createIsolatedWorld",
                "params": {"frameId": "frame", "grantUniveralAccess": True},
            }),
        )
    assert calls == []


def test_nested_owned_page_method_remains_allowed(owning):
    helpers.cdp(
        "Target.sendMessageToTarget",
        sessionId="SESSION-MINE",
        message=json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": "1"}}),
    )


def test_owned_iframe_session_fails_closed_without_target_mapping(owning, monkeypatch):
    calls = []
    base = _fake_send(current={"targetId": "MINE", "url": "https://example.com/"}, target_type="iframe")
    def send(req, **kw):
        calls.append(req)
        if req.get("method") == "Page.getFrameTree":
            assert req["session_id"] == "SESSION-MINE"
            return {"result": {"frameTree": {"frame": {"id": "MINE"}, "childFrames": [
                {"frame": {"id": "CHILD"}, "childFrames": [{"frame": {"id": "IFRAME"}}]}]}}}
        if req.get("method") == "Target.attachToTarget":
            return {"result": {"sessionId": "IFRAME-SESSION"}}
        return base(req, **kw)
    monkeypatch.setattr(helpers, "_send", send)
    with pytest.raises(helpers.TabGuardRefused, match="does not match"):
        helpers.js("42", target_id="IFRAME")
    assert not any(req.get("method") == "Runtime.evaluate" for req in calls)


@pytest.mark.parametrize("target_type", ["iframe", "worker", "service_worker", "browser", None])
def test_enumerating_non_page_target_does_not_grant_ownership(guard, monkeypatch, target_type):
    calls = []
    base = _fake_send(target_type=target_type)
    def send(req, **kw):
        calls.append(req)
        return base(req, **kw)
    monkeypatch.setattr(helpers, "_send", send)
    helpers.cdp("Target.getTargets")
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp("Target.attachToTarget", targetId="FOREIGN-CHILD")
    assert not any(r.get("method") == "Target.attachToTarget" for r in calls)
    assert helpers._owned_sessions() == set()


def test_implicit_dispatch_is_pinned_to_validated_session(owning, monkeypatch):
    calls = []
    def send(req, **kw):
        calls.append(req)
        if req.get("meta") == "guard_context":
            # Another run switches the daemon immediately after this snapshot.
            return {"target_id": "MINE", "session_id": "SESSION-MINE", "url": "https://example.com/"}
        assert req["session_id"] == "SESSION-MINE"
        return {"result": {}}
    monkeypatch.setattr(helpers, "_send", send)
    helpers.cdp("Page.navigate", url="https://example.com")
    assert calls[-1]["session_id"] == "SESSION-MINE"


@pytest.mark.parametrize("context", [
    None, [], {}, {"target_id": []}, {"target_id": "MINE"},
    {"target_id": "MINE", "session_id": []}, {"target_id": "MINE", "session_id": ""},
    {"target_id": "MINE", "session_id": "FOREIGN"},
    {"target_id": "FOREIGN", "session_id": "SESSION-MINE"},
])
def test_implicit_dispatch_fails_closed_on_unowned_or_incomplete_context(owning, monkeypatch, context):
    calls = []
    monkeypatch.setattr(helpers, "_send", lambda req, **kw: calls.append(req) or context)
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp("Runtime.evaluate", expression="1")
    assert all("method" not in req for req in calls)


def test_guard_off_performs_no_ownership_io(guard, monkeypatch, capsys):
    monkeypatch.delenv("BH_TAB_GUARD")
    def unexpected():
        pytest.fail("unguarded requests must not touch ownership files")
    monkeypatch.setattr(helpers, "_owned_path", unexpected)
    for _ in range(3):
        helpers.cdp("Target.createTarget")
        helpers.cdp("Target.attachToTarget", targetId="FOREIGN")
        helpers.cdp("Target.detachFromTarget", sessionId="SESSION-MINE")
    assert capsys.readouterr().err == ""


def test_removal_guard_off_does_not_resolve_ownership_path(guard, monkeypatch):
    monkeypatch.delenv("BH_TAB_GUARD")
    monkeypatch.setattr(helpers, "_owned_path", lambda: pytest.fail("path must not be resolved"))
    helpers._remember("tabs", "MINE", remove=True)


def test_ownership_update_lock_covers_cross_process_read_modify_replace(guard, tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(helpers.ipc, "_TMP", tmp_path)
    monkeypatch.setattr(helpers.ipc, "BH_TMP_DIR", str(tmp_path))
    monkeypatch.setattr(helpers.ipc, "BH_TMP_DIR_SHARED", False)
    monkeypatch.setattr(helpers.ipc, "_RUNTIME", runtime)
    helpers.tab_guard_reset()
    helpers._own_tab("BASE")
    path = helpers._owned_path()
    script = (
        "from browser_harness import helpers\n"
        "print('started', flush=True)\n"
        "helpers._own_tab('CHILD')\n"
        "print('done', flush=True)\n"
    )
    env = {
        **os.environ,
        "BH_TAB_GUARD": "1",
        "BH_TAB_GUARD_RUN": RUN_ID,
        "BH_TMP_DIR": str(tmp_path),
        "BH_RUNTIME_DIR": str(runtime),
    }
    with helpers._ownership_lock(path):
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=str(pathlib.Path.cwd()),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdout.readline().strip() == "started"
        assert process.poll() is None
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, stderr
    assert "done" in stdout
    assert helpers._owned_ids() == {"BASE", "CHILD"}


def test_reset_uses_the_same_ownership_lock(guard, tmp_path, monkeypatch):
    monkeypatch.setattr(helpers.ipc, "_TMP", tmp_path)
    monkeypatch.setattr(helpers.ipc, "BH_TMP_DIR", str(tmp_path))
    monkeypatch.setattr(helpers.ipc, "BH_TMP_DIR_SHARED", False)
    helpers._own_tab("MINE")
    path = helpers._owned_path()
    script = (
        "from browser_harness import helpers\n"
        "helpers.tab_guard_reset()\n"
        "print('done', flush=True)\n"
    )
    env = {
        **os.environ,
        "BH_TAB_GUARD": "1",
        "BH_TAB_GUARD_RUN": RUN_ID,
        "BH_TMP_DIR": str(tmp_path),
        "BH_RUNTIME_DIR": str(helpers.ipc._RUNTIME),
    }
    with helpers._ownership_lock(path):
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=str(pathlib.Path.cwd()),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.poll() is None
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, stderr
    assert stdout.strip() == "done"
    assert not path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_guarded_ownership_is_private_on_creation_and_update(guard):
    helpers._own_tab("MINE")
    assert helpers._owned_path().stat().st_mode & 0o777 == 0o600
    helpers._own_tab("SECOND")
    assert helpers._owned_path().stat().st_mode & 0o777 == 0o600


def test_enabling_guard_does_not_inherit_unguarded_tabs(guard, monkeypatch):
    monkeypatch.delenv("BH_TAB_GUARD")
    helpers.cdp("Target.createTarget")
    monkeypatch.setenv("BH_TAB_GUARD", "1")
    assert helpers._owned_ids() == set()


@pytest.fixture
def daemon_bridge(owning, monkeypatch):
    """Exercise helpers against the actual daemon handler, without a browser."""
    d = daemon.Daemon()
    d.target_id, d.session = "MINE", "SESSION-MINE"
    calls = []
    class CDP:
        async def send_raw(self, method, params=None, session_id=None):
            calls.append((method, params, session_id))
            if method == "Target.getTargetInfo":
                return {"targetInfo": {"type": "page", "targetId": params["targetId"],
                                       "url": "https://owned.example/", "title": "Owned"}}
            if method == "Runtime.evaluate":
                return {"result": {"value": '{"url":"https://owned.example/"}'}}
            return {}
    d.cdp = CDP()
    def send(req, **kwargs):
        result = asyncio.run(d.handle(req))
        if "error" in result:
            raise RuntimeError(result["error"])
        return result
    monkeypatch.setattr(helpers, "_send", send)
    return d, calls


@pytest.mark.parametrize("call", [
    helpers.current_tab, helpers.page_info, helpers.wait_for_network_idle,
    lambda: helpers._read_meta("connection_status"),
])
def test_metadata_helpers_refuse_foreign_current_tab(daemon_bridge, call):
    d, calls = daemon_bridge
    d.target_id, d.session = "FOREIGN", "FOREIGN-SESSION"
    d._record_event("Page.javascriptDialogOpening", {"message": "private dialog"}, "FOREIGN-SESSION")
    with pytest.raises(helpers.TabGuardRefused):
        call()
    assert calls == []


def test_metadata_helpers_still_read_owned_page_and_dialog(daemon_bridge):
    d, _ = daemon_bridge
    assert helpers.current_tab()["targetId"] == "MINE"
    assert helpers.page_info()["url"] == "https://owned.example/"
    d._record_event("Page.javascriptDialogOpening", {"message": "owned"}, "SESSION-MINE")
    assert helpers.page_info() == {"dialog": {"message": "owned"}}


def test_guarded_metadata_and_switch_reject_privileged_current_url(daemon_bridge, monkeypatch):
    d, calls = daemon_bridge
    original = d.cdp.send_raw

    async def privileged(method, params=None, session_id=None):
        if method == "Target.getTargetInfo":
            calls.append((method, params, session_id))
            return {"targetInfo": {"type": "page", "targetId": "MINE", "url": "chrome://settings"}}
        return await original(method, params, session_id)

    d.cdp.send_raw = privileged
    with pytest.raises(helpers.TabGuardRefused):
        helpers.current_tab()
    calls.clear()
    with pytest.raises(helpers.TabGuardRefused):
        helpers._read_meta("set_session", target_id="MINE", session_id="SESSION-MINE")
    assert not any(method.endswith(".enable") for method, _params, _sid in calls)


def test_foreign_dialog_does_not_leak_when_current_tab_is_owned(daemon_bridge):
    d, _ = daemon_bridge
    d._record_event("Page.javascriptDialogOpening", {"message": "private"}, "FOREIGN-SESSION")
    assert helpers.page_info() == {"url": "https://owned.example/"}


def test_foreign_dialog_close_cannot_clear_owned_dialog(daemon_bridge):
    d, _ = daemon_bridge
    d._record_event("Page.javascriptDialogOpening", {"message": "owned"}, "SESSION-MINE")
    d._record_event("Page.javascriptDialogClosed", {}, "FOREIGN-SESSION")
    assert helpers.page_info() == {"dialog": {"message": "owned"}}


def test_event_drain_filters_owned_sessions_and_preserves_foreign_events(daemon_bridge):
    d, _ = daemon_bridge
    for sid in ("SESSION-MINE", "FOREIGN-SESSION", None):
        d._record_event("Network.requestWillBeSent", {"secret": sid}, sid)
    # Even with a foreign current page, reading this run's own events is safe.
    d.target_id, d.session = "FOREIGN", "FOREIGN-SESSION"
    assert [e["session_id"] for e in helpers.drain_events()] == ["SESSION-MINE"]
    assert [e["session_id"] for e in d.events] == ["FOREIGN-SESSION", None]
    assert helpers.drain_events() == []


def test_legacy_target_reply_uses_params_session_id_for_guarded_event_filter(daemon_bridge):
    d, _ = daemon_bridge
    owned = {"method": "Target.receivedMessageFromTarget",
             "params": {"sessionId": "SESSION-MINE", "message": json.dumps({"id": 1, "result": {}})},
             "session_id": None}
    foreign = {"method": "Target.receivedMessageFromTarget",
               "params": {"sessionId": "FOREIGN-SESSION", "message": json.dumps({"id": 2, "result": {}})},
               "session_id": None}
    d._record_event(owned["method"], owned["params"], owned["session_id"])
    d._record_event(foreign["method"], foreign["params"], foreign["session_id"])
    assert helpers.drain_events() == [owned]
    assert list(d.events) == [foreign]


def test_legacy_target_reply_ignores_outer_owned_carrier_and_malformed_nested_messages(daemon_bridge):
    d, _ = daemon_bridge
    events = [
        {"method": "Target.receivedMessageFromTarget",
         "params": {"sessionId": "FOREIGN-SESSION", "message": json.dumps({"id": 3})},
         "session_id": "SESSION-MINE"},
        {"method": "Target.receivedMessageFromTarget",
         "params": {"sessionId": "SESSION-MINE", "message": "not-json"},
         "session_id": "SESSION-MINE"},
        {"method": "Target.receivedMessageFromTarget",
         "params": {"message": json.dumps({"id": 4})},
         "session_id": "SESSION-MINE"},
    ]
    for event in events:
        d._record_event(event["method"], event["params"], event["session_id"])
    assert helpers.drain_events() == []
    assert list(d.events) == events


def test_context_wide_event_subscription_is_refused_and_foreign_events_stay_hidden(daemon_bridge):
    d, calls = daemon_bridge
    helpers.cdp("Network.enable")
    d._record_event("ServiceWorker.workerVersionUpdated", {"secret": "foreign"}, "FOREIGN-SESSION")
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp("ServiceWorker.enable")
    assert helpers.drain_events() == []
    assert len(d.events) == 1
    assert d.events[0]["session_id"] == "FOREIGN-SESSION"
    assert calls[-1] == ("Network.enable", {}, "SESSION-MINE")


def test_older_daemon_cannot_silently_return_unguarded_metadata(owning, monkeypatch):
    monkeypatch.setattr(helpers, "_send", lambda req: {"dialog": {"message": "private"}})
    with pytest.raises(helpers.TabGuardRefused):
        helpers.page_info()


def test_metadata_snapshot_stays_owned_during_concurrent_switch(daemon_bridge):
    d, _ = daemon_bridge
    async def send_raw(method, params=None, session_id=None):
        assert params == {"targetId": "MINE"}
        d.target_id, d.session = "FOREIGN", "FOREIGN-SESSION"
        await asyncio.sleep(0)
        return {"targetInfo": {"targetId": "MINE", "type": "page", "url": "https://owned.example/"}}
    d.cdp.send_raw = send_raw
    response = helpers._read_meta("connection_status")
    assert response["target_id"] == "MINE"
    assert response["session_id"] == "SESSION-MINE"


def test_pinned_request_never_uses_new_current_session_or_recovers_there(daemon_bridge, monkeypatch):
    d, calls = daemon_bridge
    original_send = helpers._send
    def switch_after_snapshot(req, **kwargs):
        response = original_send(req, **kwargs)
        if req.get("meta") == "guard_context":
            d.target_id, d.session = "FOREIGN", "FOREIGN-SESSION"
        return response
    monkeypatch.setattr(helpers, "_send", switch_after_snapshot)
    helpers.cdp("Page.navigate", url="https://owned.example/")
    assert calls[-1][2] == "SESSION-MINE"
    # A dropped pinned session must error, never replay against the new tab.
    d.target_id, d.session = "MINE", "SESSION-MINE"
    async def stale(method, params=None, session_id=None):
        calls.append((method, params, session_id))
        raise RuntimeError("Session with given id not found")
    d.cdp.send_raw = stale
    count = len(calls)
    with pytest.raises(helpers.TabGuardRefused, match="URL"):
        helpers.cdp("Page.navigate", url="https://owned.example/")
    assert len(calls) == count + 1
    assert calls[-1][0] == "Target.getTargetInfo"


def test_guarded_switch_does_not_disable_foreign_session(daemon_bridge, monkeypatch):
    d, calls = daemon_bridge
    monkeypatch.setenv("BH_TAB_MARKER", "0")
    d.target_id, d.session = "FOREIGN", "FOREIGN-SESSION"
    helpers._read_meta("set_session", target_id="MINE", session_id="SESSION-MINE")
    assert d.target_id == "MINE"
    assert all(sid in {None, "SESSION-MINE"} for _, _, sid in calls)
    assert not any(sid == "FOREIGN-SESSION" for _, _, sid in calls)


def test_guarded_switch_rejects_foreign_session(daemon_bridge):
    d, calls = daemon_bridge
    with pytest.raises(helpers.TabGuardRefused):
        helpers._read_meta("set_session", target_id="MINE", session_id="FOREIGN-SESSION")
    assert d.session == "SESSION-MINE"
    assert not any(sid == "FOREIGN-SESSION" for _, _, sid in calls)


def test_old_daemon_refused_before_session_switch(owning, monkeypatch):
    requests = []
    def old_daemon(req):
        requests.append(req)
        return {"session_id": "SESSION-MINE"}
    monkeypatch.setattr(helpers, "_send", old_daemon)
    with pytest.raises(helpers.TabGuardRefused, match="reloaded"):
        helpers._read_meta("set_session", target_id="MINE", session_id="SESSION-MINE")
    assert [req["meta"] for req in requests] == ["guard_context"]


@pytest.mark.parametrize("meta", ["current_tab", "pending_dialog", "session", "drain_events"])
def test_metadata_connection_failure_is_a_guard_refusal(owning, monkeypatch, meta):
    def disconnected(req):
        raise RuntimeError("cdp_disconnected")
    monkeypatch.setattr(helpers, "_send", disconnected)
    with pytest.raises(helpers.TabGuardRefused, match="failing closed"):
        helpers._read_meta(meta)


@pytest.mark.parametrize("failure", ["disconnected", "missing-tree", "foreign-frame"])
def test_iframe_proof_failure_never_dispatches_attach(owning, monkeypatch, failure):
    calls = []
    base = _fake_send(current={"targetId": "MINE"}, target_type="iframe")
    def send(req, **kwargs):
        calls.append(req)
        if req.get("method") == "Page.getFrameTree":
            if failure == "disconnected":
                raise RuntimeError("cdp_disconnected")
            if failure == "missing-tree":
                return {"result": {}}
            return {"result": {"frameTree": {"frame": {"id": "MINE"},
                "childFrames": [{"frame": {"id": "OTHER-IFRAME"}}]}}}
        return base(req, **kwargs)
    monkeypatch.setattr(helpers, "_send", send)
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp("Target.attachToTarget", targetId="FOREIGN-IFRAME")
    assert not any(r.get("method") == "Target.attachToTarget" for r in calls)


@pytest.mark.parametrize("message", [
    "{bad json", "null", "[]", "{}", '{"method": 3}',
    '{"method":"Runtime.evaluate","sessionId":"FOREIGN"}',
    '{"method":"Target.sendMessageToTarget","params":{"sessionId":"FOREIGN"}}',
])
def test_message_routing_envelope_cannot_escape_owned_session(owning, monkeypatch, message):
    calls = []
    monkeypatch.setattr(helpers, "_send", lambda req, **kwargs: calls.append(req))
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp("Target.sendMessageToTarget", sessionId="SESSION-MINE", message=message)
    assert calls == []


def test_unknown_target_routing_method_fails_closed(owning):
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp("Target.autoAttachRelated", targetId="FOREIGN")


def test_failed_atomic_write_preserves_record_and_removes_temporary(owning, monkeypatch, capsys):
    path = helpers._owned_path()
    before = path.read_bytes()
    def fail_replace(src, dst):
        raise OSError("replace failed")
    monkeypatch.setattr(helpers.os, "replace", fail_replace)
    helpers._own_tab("SECOND")
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == sorted(
        [path.name, path.name + ".lock"]
    )
    assert "WARNING" in capsys.readouterr().err


def test_run_reset_removes_its_record(owning):
    path = helpers._owned_path()
    helpers.tab_guard_reset()
    assert not path.exists()
    assert helpers._owned_ids() == set()
    assert helpers._owned_sessions() == set()


def test_run_reset_propagates_lock_failure(guard, monkeypatch):
    def fail(_path):
        raise OSError("lock failed")
    monkeypatch.setattr(helpers, "_ownership_lock", fail)
    with pytest.raises(OSError, match="lock failed"):
        helpers.tab_guard_reset()


def test_run_reset_propagates_unlink_failure(guard, monkeypatch):
    class FailingPath:
        def unlink(self):
            raise PermissionError("unlink failed")

    class NoopLock:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(helpers, "_owned_path", lambda: FailingPath())
    monkeypatch.setattr(helpers, "_ownership_lock", lambda _path: NoopLock())
    with pytest.raises(PermissionError, match="unlink failed"):
        helpers.tab_guard_reset()


@pytest.mark.parametrize("tool", ["browser_page_info", "browser_current_tab"])
def test_mcp_metadata_tools_cannot_return_foreign_dialog_or_page(daemon_bridge, monkeypatch, tool):
    pytest.importorskip("mcp")
    from mcp_types import CallToolRequestParams
    import mcp_server

    d, calls = daemon_bridge
    d.target_id, d.session = "FOREIGN", "FOREIGN-SESSION"
    d._record_event("Page.javascriptDialogOpening", {"message": "private dialog"}, "FOREIGN-SESSION")
    monkeypatch.setattr(mcp_server, "ensure_daemon", lambda: None)
    result = asyncio.run(mcp_server.SERVER._handle_call_tool(
        None, CallToolRequestParams(name=tool, arguments={})))
    assert result.is_error is True
    assert "[tab-guard] REFUSED" in result.content[0].text
    assert "private dialog" not in result.content[0].text
    assert calls == []
