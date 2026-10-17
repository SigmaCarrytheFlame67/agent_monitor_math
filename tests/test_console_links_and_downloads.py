"""Proof downloads, the Projects link, and controls hidden from guests."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

WEB = Path(__file__).parents[1] / "agent_monitor" / "web"
HTML = (WEB / "console.html").read_text(encoding="utf-8")
SCRIPT = HTML.rsplit("<script>", 1)[1].split("</script>", 1)[0]
NODE = shutil.which("node")
if not NODE and Path("/home/ubuntu/.local/node24/bin/node").is_file():
    NODE = "/home/ubuntu/.local/node24/bin/node"


def _js_function(name: str) -> str:
    start = SCRIPT.index(f"function {name}(")
    if SCRIPT[max(0, start - 6) : start] == "async ":
        start -= 6
    opening = SCRIPT.index("{", SCRIPT.index(")", start))
    depth = 0
    for index in range(opening, len(SCRIPT)):
        if SCRIPT[index] == "{":
            depth += 1
        elif SCRIPT[index] == "}":
            depth -= 1
            if depth == 0:
                return SCRIPT[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


def _run(program: str) -> object:
    result = subprocess.run([NODE, "-e", program], capture_output=True, text=True, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


# A fetch stub, Blob and URL stand-ins, and a document that records the saved link.
_HARNESS = """
const saved=[]; const alerts=[]; const revoked=[];
globalThis.alert=m=>alerts.push(m);
globalThis.setTimeout=fn=>fn();
class Blob{constructor(parts,opts){this.parts=parts;this.type=(opts||{}).type||'';}}
globalThis.Blob=Blob;
globalThis.URL={createObjectURL:b=>{saved.push({blob:b});return 'blob:1';},revokeObjectURL:h=>revoked.push(h)};
globalThis.document={body:{append(){}},createElement:()=>({click(){saved[saved.length-1].name=this.download;saved[saved.length-1].href=this.href;},remove(){}})};
const button={disabled:false};
const $=()=>button;
"""


class ProofDownloadTests(unittest.TestCase):
    def test_download_no_longer_uses_a_link_without_the_mount_path(self) -> None:
        setter = _js_function("setDownloadUrl")
        self.assertNotIn("a.href=url", setter)
        self.assertIn("downloadWorkspaceFile(url", setter)

    @unittest.skipUnless(NODE, "node is unavailable")
    def test_markdown_proof_is_saved_as_its_text_not_the_json_record(self) -> None:
        program = _HARNESS + _js_function("setDownloadUrl") + _js_function("downloadWorkspaceFile") + """
const requests=[];
globalThis.fetch=async(url,opts)=>{requests.push(url);return {ok:true,headers:{get:()=> 'application/json'},
  json:async()=>({path:'proof.md',content:'# Proof\\n\\n$a+b$'})};};
setDownloadUrl('/api/workspace/r1/file?path=proof.md&download=1','proof.md');
(async()=>{await button.onclick();
  console.log(JSON.stringify({requests,saved:saved.map(s=>({name:s.name,parts:s.blob.parts,type:s.blob.type})),alerts,revoked,disabled:button.disabled}));})();
"""
        result = _run(program)
        self.assertEqual(result["requests"], ["/api/workspace/r1/file?path=proof.md&download=1"])
        self.assertEqual(result["saved"], [{"name": "proof.md", "parts": ["# Proof\n\n$a+b$"], "type": "text/markdown;charset=utf-8"}])
        self.assertEqual(result["alerts"], [])
        self.assertEqual(result["revoked"], ["blob:1"])
        self.assertFalse(result["disabled"])

    @unittest.skipUnless(NODE, "node is unavailable")
    def test_pdf_is_saved_as_returned_and_failures_are_reported(self) -> None:
        program = _HARNESS + _js_function("setDownloadUrl") + _js_function("downloadWorkspaceFile") + """
let status=200;
globalThis.fetch=async()=>({ok:status===200,status,headers:{get:()=> 'application/pdf'},blob:async()=>({pdf:true})});
setDownloadUrl('/api/workspace/r1/file?path=proof.pdf&download=1','proof.pdf');
(async()=>{await button.onclick(); status=404; await button.onclick();
  console.log(JSON.stringify({saved:saved.map(s=>({name:s.name,blob:s.blob})),alerts}));})();
"""
        result = _run(program)
        self.assertEqual(result["saved"], [{"name": "proof.pdf", "blob": {"pdf": True}}])
        self.assertEqual(result["alerts"], ["Download failed: HTTP 404"])


class ConsoleLinkTests(unittest.TestCase):
    def test_sidebar_links_to_projects_through_the_console_mount(self) -> None:
        nav = HTML.split('<nav class="sidebar-links"', 1)[1].split("</nav>", 1)[0]
        self.assertIn('<a id="projects-link" href="/projects">Projects ↗</a>', nav)
        self.assertIn("$('projects-link').href=pcUrl('/projects');", SCRIPT)

    def test_sidebar_links_to_the_renamed_problems_page(self) -> None:
        nav = HTML.split('<nav class="sidebar-links"', 1)[1].split("</nav>", 1)[0]
        self.assertIn('<a href="/problems">Problems ↗</a>', nav)
        # /open-problems is only a redirect stub on the docs site now.
        self.assertNotIn("/open-problems", HTML)

    def test_guests_do_not_see_share_or_projects(self) -> None:
        guest_ui = _js_function("applyGuestUi")
        hidden = guest_ui.split("for(const id of [", 1)[1].split("]", 1)[0]
        self.assertIn("'share-run-btn'", hidden)
        self.assertIn("'projects-link'", hidden)
        sharing = (WEB / "sharing.js").read_text(encoding="utf-8")
        # Share is shown only to the run's owner (never a guest), once the run is known.
        self.assertIn("button.style.display='none';side.prepend(button);", sharing)
        self.assertIn("const mine=!!me&&!me.guest&&!!run&&", sharing)

    def test_failed_delete_is_reported(self) -> None:
        delete = _js_function("deleteRun")
        self.assertIn("if(!r.ok)", delete)
        self.assertIn("alert(d.error||'Could not delete this run.')", delete)


class ProjectsPageTests(unittest.TestCase):
    def test_engine_choices_use_their_labels(self) -> None:
        self.assertIn("o.textContent=e.label||e.name||e.id;", (WEB / "projects.js").read_text(encoding="utf-8"))

    def test_project_list_links_the_worked_example(self) -> None:
        page = (WEB / "projects.html").read_text(encoding="utf-8")
        listing = page.split('<section id="project-list"', 1)[1].split("</section>", 1)[0]
        self.assertIn('<a href="__BASE__/examples/erdos-straus">the Erdős–Straus worked example</a>', listing)


if __name__ == "__main__":
    unittest.main()
