"""A short warning next to the sponsored Kimi engines while their provider struggles.

The broker's /healthz reports passive upstream health (it never calls Kimi);
the console turns a degraded report into a notice on every engine the broker
serves (for a viewer without its own Kimi key), and the composer shows it
while the chosen engine runs a Kimi model.  No provider is contacted.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_monitor import auth, sponsored_kimi_client
from test_console_guest_access import (  # noqa: F401  (fixture import)
    APP_MOUNT,
    _issue_guest,
    guest_console,
    request,
    running_server,
)

HTML = (
    Path(__file__).parents[1] / "agent_monitor" / "web" / "console.html"
).read_text(encoding="utf-8")
SCRIPT = HTML.rsplit("<script>", 1)[1].split("</script>", 1)[0]
NODE = shutil.which("node")


def _engines(port: int, cookie: str) -> dict[str, dict]:
    status, _headers, raw = request(
        port, "GET", f"{APP_MOUNT}/api/engines", headers={"Cookie": cookie}
    )
    assert status == 200, raw
    return {engine["id"]: engine for engine in json.loads(raw)["engines"]}


def test_degraded_provider_adds_a_notice_to_the_engines_the_broker_serves(
    guest_console, monkeypatch
):
    monkeypatch.setattr(sponsored_kimi_client, "upstream_degraded", lambda: True)
    with running_server() as port:
        _guest, cookie, _token = _issue_guest(port)
        engines = _engines(port, cookie)
    assert engines["kimi"]["notice"] == sponsored_kimi_client.DEGRADED_NOTICE
    assert "try again later" in engines["kimi"]["notice"].lower()
    noticed = sorted(engine_id for engine_id, engine in engines.items() if "notice" in engine)
    assert noticed == sorted(
        engine_id for engine_id in engines if sponsored_kimi_client.supports_engine(engine_id)
    )
    assert {"kimi", "plain"} <= set(noticed)
    assert "hermes" not in noticed
    assert engines["kimi"]["requires_login"] is False


def test_no_notice_for_a_viewer_whose_kimi_runs_use_its_own_key(guest_console, monkeypatch):
    monkeypatch.setattr(sponsored_kimi_client, "upstream_degraded", lambda: True)
    with running_server() as port:
        guest, cookie, _token = _issue_guest(port)
        auth.set_user_env(int(guest["id"]), {"KIMI_API_KEY": "sk-guest-own-kimi-key-1234"})
        engines = _engines(port, cookie)
    assert not [engine for engine in engines.values() if "notice" in engine]


def test_healthy_provider_adds_no_notice(guest_console, monkeypatch):
    monkeypatch.setattr(sponsored_kimi_client, "upstream_degraded", lambda: False)
    with running_server() as port:
        _guest, cookie, _token = _issue_guest(port)
        engines = _engines(port, cookie)
    assert not [engine for engine in engines.values() if "notice" in engine]


def _js_function(name: str) -> str:
    start = SCRIPT.index(f"function {name}(")
    opening = SCRIPT.index("{", start)
    depth = 0
    for index in range(opening, len(SCRIPT)):
        if SCRIPT[index] == "{":
            depth += 1
        elif SCRIPT[index] == "}":
            depth -= 1
            if depth == 0:
                return SCRIPT[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


def test_the_notice_follows_the_chosen_engine_and_model():
    render = _js_function("renderEngines")
    call = "renderEngineNotice(selected,currentModel());"
    assert call in render
    # Placed before renderModels, which may switch the engine (or model) and
    # then renders the notice again with the ones it settles on.
    assert render.index(call) < render.index("if(SETTINGS)renderModels();")
    # renderModels ends with renderRunRuntime, the box the notice sits under.
    assert "renderRunRuntime();" in _js_function("renderModels")
    assert "renderEngineNotice(ENGINES.find(x=>x.id===engine),currentModel());" in _js_function(
        "renderRunRuntime"
    )


@pytest.mark.skipif(not NODE, reason="node is unavailable")
def test_notice_is_plain_text_under_the_composer_and_hides_again():
    program = f"""
const els={{}};
const $=id=>els[id]||null;
const created=[];
const document={{createElement:tag=>{{const node={{tag,attrs:{{}},hidden:true,textContent:'',innerHTML:'',
  setAttribute(name,value){{this.attrs[name]=value;}}}};created.push(node);return node;}}}};
els['run-runtime']={{after:node=>{{els[node.id]=node;node.placedAfter='run-runtime';}}}};
{_js_function("renderEngineNotice")}
const out=[];
renderEngineNotice({{id:'kimi'}},'kimi-k3'); out.push(created.length);
renderEngineNotice({{id:'kimi',notice:'<b>Kimi</b> is slow'}},'');
const note=els['engine-notice'];
out.push([note.textContent,note.innerHTML,note.hidden,note.className,note.attrs.role,note.placedAfter]);
renderEngineNotice({{id:'math_harness'}},'kimi-k3'); out.push(note.hidden);
renderEngineNotice({{id:'kimi',notice:'again'}}); out.push([created.length,note.hidden,note.textContent]);
// Another sponsored engine: only while it runs a Kimi model.
renderEngineNotice({{id:'plain',notice:'slow'}},'gpt-5.6-sol'); out.push(note.hidden);
renderEngineNotice({{id:'plain',notice:'slow'}},'kimi-k3'); out.push([note.hidden,note.textContent]);
renderEngineNotice(undefined); out.push(note.hidden);
console.log(JSON.stringify(out));
"""
    result = subprocess.run([NODE, "-e", program], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        0,
        ["<b>Kimi</b> is slow", "", False, "guest-allowance", "status", "run-runtime"],
        True,
        [1, False, "again"],
        True,
        [False, "slow"],
        True,
    ]


@pytest.mark.skipif(not NODE, reason="node is unavailable")
def test_open_tabs_pick_up_a_changed_notice_without_a_reload():
    assert "setInterval(()=>{ if(!document.hidden)refreshEngineNotices(); },60000);" in SCRIPT
    assert (
        "document.addEventListener('visibilitychange',()=>{ if(!document.hidden)refreshEngineNotices(); });"
        in SCRIPT
    )
    program = f"""
const shown=[];
function renderEngineNotice(selected,model){{ shown.push([selected?.id,selected?.notice||null,model]); }}
function currentModel(){{ return 'kimi-k3'; }}
let engine='kimi';
const ENGINES=[{{id:'kimi',label:'Kimi'}},{{id:'plain',label:'Plain',notice:'old'}}];
let answers=[];
async function fetch(path){{ if(path!=='/api/engines')throw new Error(path); const next=answers.shift(); if(next instanceof Error)throw next; return {{json:async()=>next}}; }}
async {_js_function("refreshEngineNotices")}
(async()=>{{
  answers=[{{engines:[{{id:'kimi',notice:'Kimi is slow'}},{{id:'plain'}}]}}];
  await refreshEngineNotices();
  const first=[ENGINES.map(e=>e.notice??null),ENGINES[0].label];
  answers=[new Error('offline')];
  await refreshEngineNotices();
  answers=[{{engines:[{{id:'kimi'}},{{id:'plain'}}]}}];
  await refreshEngineNotices();
  console.log(JSON.stringify([first,shown,ENGINES.map(e=>e.notice??null)]));
}})();
"""
    result = subprocess.run([NODE, "-e", program], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        [["Kimi is slow", None], "Kimi"],
        # A failed refresh keeps what is shown; a recovery hides it again.
        [["kimi", "Kimi is slow", "kimi-k3"], ["kimi", None, "kimi-k3"]],
        [None, None],
    ]
