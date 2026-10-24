from __future__ import annotations

from html.parser import HTMLParser
import shutil
import subprocess

import pytest

from job_radar.dashboard import _TEMPLATE
from job_radar.dashboard_sync import install_optional_sync


def test_installer_is_idempotent_and_preserves_original_data_and_controls():
    html = install_optional_sync(_TEMPLATE)
    assert install_optional_sync(html) == html
    assert html.count('data-dashboard-optional-sync="1"') == 1
    assert html.count("function installOptionalStatusClient") == 1
    for original in (
        '<script type="application/json" id="dashboard-data">__DASHBOARD_DATA__</script>',
        'id="jobList">__JOB_CARDS__',
        "function setStatus(job,next)",
        "function renderAll()",
        "tracking:DASHBOARD.tracking",
    ):
        assert original in html
    assert "Cloudflare" in html
    assert "disconnect is not remote deletion" in html
    assert html.index('id="syncPrivacy"') < html.index('id="syncConsent"')
    assert "No applicant data is sent by this page." not in html


@pytest.mark.parametrize(
    "anchor",
    [
        "  <script>\n    const DASHBOARD=",
        "function saveState(){localStorage.setItem(STORAGE_KEY,JSON.stringify(state))}",
        '    <section class="recommendations">',
        "  </style>",
        "\n  </script>",
    ],
)
def test_unknown_templates_fail_closed_instead_of_losing_change_or_lock_hooks(anchor):
    with pytest.raises(ValueError):
        install_optional_sync(_TEMPLATE.replace(anchor, "", 1))


def test_duplicate_hook_anchors_are_rejected():
    with pytest.raises(ValueError):
        install_optional_sync(_TEMPLATE + "  <style>\n  </style>")


def test_installer_has_no_python_network_path_or_external_script():
    html = install_optional_sync(_TEMPLATE)
    assert 'src="http' not in html
    assert "fetch('/v1/statuses'" in html
    assert "mode: 'same-origin'" in html
    assert "redirect: 'error'" in html
    assert "legacy_keys: []" in html
    assert "method: 'DELETE'" not in html
    assert "eval(" not in html
    assert "window.token" not in html
    assert "globalThis.token" not in html


def test_installed_javascript_parses_as_one_classic_script():
    class Scripts(HTMLParser):
        def __init__(self):
            super().__init__()
            self.classic = False
            self.scripts: list[str] = []

        def handle_starttag(self, tag, attrs):
            self.classic = tag == "script" and not dict(attrs).get("type")

        def handle_endtag(self, tag):
            if tag == "script":
                self.classic = False

        def handle_data(self, data):
            if self.classic:
                self.scripts.append(data)

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the independent JavaScript syntax check")
    parser = Scripts()
    parser.feed(install_optional_sync(_TEMPLATE))
    assert len(parser.scripts) == 1
    result = subprocess.run(
        [node, "--check"], input=parser.scripts[0], text=True,
        capture_output=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
