"""The public interactive entry paints while a background host probe is blocked."""

import os

import pytest


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="controlling PTY unavailable")
def test_main_interactive_paints_and_focuses_before_external_probe_finishes(tmp_path):
    from harness.pty_tui import run_python_snippet

    blocked = tmp_path / "blocked"
    release = tmp_path / "release"
    script = r"""
import os,sys,pwd,time,json
from pathlib import Path
from uxon.infra import config_loader,probes,tmux,version_probe
from uxon.infra.config_renderer import render_config
from uxon.cli.main import main
root=Path(ROOT)
user=pwd.getpwuid(os.getuid()).pw_name
os.environ['XDG_STATE_HOME']=str(root/'state')
os.environ['XDG_CACHE_HOME']=str(root/'cache')
os.environ.pop('TMUX',None)
os.chdir(root)
operator=root/'config.toml'
operator.write_text(render_config(dict(default_launch_mode='fixed',default_launch_user=user,session_users=[user],new_project_root=str(root),allowed_roots=[],audit={'enabled':False},tmux_socket_template=str(root/'uxon.sock'))))
config_loader.OPERATOR_CONFIG_PATH=operator
read=Path.read_text
def proc(path,*a,**kw):
    return str(os.getuid()) if path == Path('/proc/self/loginuid') else read(path,*a,**kw)
Path.read_text=proc
import subprocess
def query(argv,*,timeout):
    assert argv == [sys.executable,'-m','uxon.infra.tmux_server_probe','--socket',str(root/'uxon.sock')],argv
    assert timeout == .5
    return subprocess.CompletedProcess(argv,0,json.dumps({'state':'absent','sessions':[],'error':''}),'')
tmux.run_query=query
def discovery(argv,*,timeout):
    assert argv == ['sh','-c','for c in tmux claude codex cursor-agent; do\n    printf "%s\\t%s\\n" "$c" "$(command -v "$c" 2>/dev/null)"\ndone']
    assert timeout == 2.0
    (root/'blocked').write_text('entered')
    deadline=time.monotonic()+30
    while not (root/'release').exists():
        assert time.monotonic()<deadline,'external probe rendezvous timed out'
        time.sleep(.01)
    return subprocess.CompletedProcess(argv,0,'tmux\t/fake/tmux\nclaude\t/fake/claude\ncodex\t/fake/codex\ncursor-agent\t/fake/cursor-agent\n','')
probes.run_query=discovery
checkout=str(Path(version_probe.__file__).resolve().parents[3])
def version(argv,**kw):
    assert kw == {} and argv == ['git','-c',f'safe.directory={checkout}','-C',checkout,'rev-parse','--short','HEAD']
    return subprocess.CompletedProcess(argv,128,'','no checkout')
version_probe.run_query=version
import uxon.tui.app as app_module
from uxon.tui.screens.main import MainScreen
class Ready(app_module.UxonApp):
    CSS_PATH=Path(app_module.__file__).with_name('styles.tcss')
    def on_mount(self):
        super().on_mount()
        self.acknowledged=False
        self.set_interval(.02,lambda:self.call_after_refresh(self.ack))
    def ack(self):
        if self.acknowledged or not isinstance(self.screen,MainScreen) or self.focused is None: return
        row=self.screen.query_one('#action-open')
        if self.focused is not row or row.region.height<=0 or not row.display or not (root/'blocked').exists(): return
        assert not (root/'release').exists()
        fd=os.open('/dev/tty',os.O_RDWR)
        assert os.isatty(fd) and sys.stdin.isatty() and sys.stdout.isatty()
        os.close(fd)
        self.acknowledged=True
        os.write(1,b'BODY_FOCUSED_PROBE_BLOCKED\n')
app_module.UxonApp=Ready
print('EXIT:',main([]),flush=True)
""".replace("ROOT", repr(str(tmp_path)))

    def keys():
        assert blocked.read_text() == "entered"
        assert not release.exists()
        release.write_text("continue")
        yield (10.0, b"q", "EXIT: 0")

    trace = run_python_snippet(
        script,
        keys(),
        initial_wait_for="BODY_FOCUSED_PROBE_BLOCKED",
        initial_drain=20,
        timeout=60,
    )
    assert trace.exit_code == 0
    assert "BODY_FOCUSED_PROBE_BLOCKED" in trace.frames[0]
    assert "Open project" in trace.frames[0]
