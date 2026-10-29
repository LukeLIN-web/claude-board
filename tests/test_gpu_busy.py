"""Tests for scripts/gpu_busy.py: which cards are free, and which launches go first.

nvidia-smi and the CUDA probe are stubbed; the claims and registrations are real
flocks, in directories of each test's own.
"""
import fcntl
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

import gpu_busy
import gpu_wait

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
UUID = {0: "GPU-a", 1: "GPU-b", 2: "GPU-c", 3: "GPU-d"}
HOLDER = "import fcntl, os, sys, time; fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR); " \
         "fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); time.sleep(60)"
# A launch under a gpu_wait copy that records nothing and hands its job the lock fd: one process,
# registered at 1 ns on every card, holds the registration and the claim lock of the card it claimed.
OLD_LAUNCH = ("import fcntl, os, sys, time; reg = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR); "
              "fcntl.flock(reg, fcntl.LOCK_EX); os.write(reg, b'1 pid 2: bash train.sh\\ncards 0,1,2,3\\n'); "
              "lock = os.open(sys.argv[2], os.O_CREAT | os.O_RDWR); fcntl.flock(lock, fcntl.LOCK_EX); "
              "print('held', flush=True); time.sleep(60)")


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end
        time.sleep(0.05)


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A four-card box: nvidia-smi answers from ``box`` (MiB used per card, compute apps
    as (uuid, pid, MiB, name)); claim locks live in a scratch LOCK_DIR."""
    state = {"used": {u: 0 for u in UUID.values()}, "apps": [], "ecc": set(), "fail": False}
    monkeypatch.setattr(gpu_wait, "LOCK_DIR", str(tmp_path / "locks"))
    os.makedirs(gpu_wait.LOCK_DIR, exist_ok=True)
    monkeypatch.setattr(gpu_wait, "cuda_uuids", lambda: dict(UUID))
    monkeypatch.setattr(gpu_wait, "ecc_bad_gpus", lambda: set(state["ecc"]))

    def smi(what, fields):
        if state["fail"]:
            raise gpu_busy.ProbeFailed("nvidia-smi failed")
        if what == "gpu":
            return "".join(f"{u}, {mb}\n" for u, mb in state["used"].items())
        return "".join(f"{u}, {pid}, {mb}, {name}\n" for u, pid, mb, name in state["apps"])
    monkeypatch.setattr(gpu_busy, "smi", smi)
    return state


def claimable(**kw):
    return [c for c, why in gpu_busy.card_report(**kw).items() if not why]


@pytest.fixture
def other_holder():
    """Hold a lock from another process, as another gpu_wait or launch would."""
    procs = []

    def hold(path):
        p = subprocess.Popen([sys.executable, "-c", HOLDER, path], stdout=subprocess.PIPE, text=True)
        assert p.stdout.readline() == "held\n"
        procs.append(p)
        return p
    yield hold
    for p in procs:
        p.kill()
        p.wait()


def test_a_card_is_claimable_only_with_nobody_on_it(box):
    assert claimable() == [0, 1, 2, 3]
    box["apps"] = [("GPU-b", 4242, 300, "python")]   # anyone's process, however small
    box["used"]["GPU-b"] = 300
    box["used"]["GPU-c"] = gpu_busy.IDLE_MB + 1      # graphics-only: memory used, no compute process
    box["used"]["GPU-d"] = gpu_busy.IDLE_MB
    report = gpu_busy.card_report()
    assert claimable() == [0, 3]
    assert "4242" in report[1] and "MiB used" in report[2]


def test_excluded_ecc_and_unlisted_cards_are_out(box):
    box["ecc"] = {1}
    del box["used"]["GPU-c"]                          # CUDA sees it, nvidia-smi does not
    box["apps"] = [("GPU-hidden", "[N/A]", "[N/A]", "python")]  # as nvidia-smi lists a CUDA-invisible card: bothers nobody
    assert claimable(exclude=frozenset({3})) == [0]


def test_a_card_claimed_by_another_gpu_wait_is_out(box, other_holder):
    other_holder(gpu_wait.claim_lock(2))
    box["apps"] = [("GPU-c", 99, 40000, gpu_busy.BALLAST_NAME)]   # its ballast is somebody else's too
    box["used"]["GPU-c"] = 40000
    assert gpu_busy.card_report()[2] == "claimed by another gpu_wait"
    box["apps"], box["used"]["GPU-c"] = [], 0
    assert gpu_busy.card_report()[2] == "claimed by another gpu_wait"
    other_holder(gpu_wait.claim_lock(1, 2))   # a job on a slot of a shared card
    assert gpu_busy.card_report()[1] == "claimed by another gpu_wait"


def test_a_job_sees_its_own_claimed_card_with_its_ballast_as_free(box):
    fd = os.open(gpu_wait.claim_lock(3), os.O_CREAT | os.O_RDWR)   # as a gpu_wait copy that hands the lock fd to its job does
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        box["apps"] = [("GPU-d", 99, 44000, gpu_busy.BALLAST_NAME)]
        box["used"]["GPU-d"] = 44500
        assert claimable() == [0, 1, 2, 3]
        box["used"]["GPU-d"] = 44000 + gpu_busy.IDLE_MB + 1   # somebody graphics-only beside the ballast
        assert gpu_busy.card_report()[3].endswith("MiB used")
        box["used"]["GPU-d"] = 44500
        box["apps"].append(("GPU-d", 5150, 400, "python"))
        assert "5150" in gpu_busy.card_report()[3]
        box["apps"] = [("GPU-c", 98, 44000, gpu_busy.BALLAST_NAME)]   # a ballast on a card it did not claim
        box["used"]["GPU-c"] = 44000
        assert gpu_busy.BALLAST_NAME in gpu_busy.card_report()[2]
    finally:
        os.close(fd)


def test_a_job_owns_the_claims_whose_holders_read_its_pipe(box):
    """The job holds the write end of the pipe its claim holders read: their claims are its own,
    a claim whose holders read another job's pipe is not."""
    life, heir = os.pipe()   # as gpu_wait leaves its job the pipe
    other_life, other_heir = os.pipe()
    gpu_wait.try_claim(3, ["this job"], life)
    gpu_wait.try_claim(2, ["other job"], other_life)
    other = subprocess.Popen(["sleep", "60"], pass_fds=(other_heir,))
    for end in (life, other_life, other_heir):
        os.close(end)
    try:
        box["apps"] = [("GPU-d", 99, 44000, gpu_busy.BALLAST_NAME), ("GPU-c", 98, 44000, gpu_busy.BALLAST_NAME)]
        box["used"]["GPU-d"] = box["used"]["GPU-c"] = 44500
        report = gpu_busy.card_report()
        assert report[3] == "" and report[2] == "claimed by another gpu_wait"
    finally:
        os.close(heir)
        other.kill()
        other.wait()


def test_a_failed_probe_takes_no_card(box, monkeypatch, capsys):
    box["fail"] = True
    with pytest.raises(SystemExit) as e:
        gpu_busy.main(["cards"])
    assert e.value.code == 2 and capsys.readouterr().out == ""
    box["fail"] = False
    monkeypatch.setattr(gpu_busy, "smi", lambda what, fields: "GPU-a, [N/A]\n" if what == "gpu" else "")
    with pytest.raises(gpu_busy.ProbeFailed):
        gpu_busy.card_report()
    monkeypatch.setattr(gpu_wait, "cuda_uuids", dict)
    with pytest.raises(gpu_busy.ProbeFailed):
        gpu_busy.card_report()


def test_cards_prints_the_claimable_ordinals(box, capsys):
    box["apps"] = [("GPU-a", 1, 500, "python")]
    gpu_busy.main(["cards", "--exclude", "2"])
    assert capsys.readouterr().out == "1,3 0,1,2,3\n"


# ── launches ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """``gpu_busy.py`` as a command on the four-card box, with the launches in a scratch REG_DIR
    and the claim locks in the box's LOCK_DIR."""
    monkeypatch.setattr(gpu_busy, "REG_DIR", str(tmp_path / "launches"))
    monkeypatch.setattr(gpu_wait, "LOCK_DIR", str(tmp_path / "locks"))
    os.makedirs(gpu_wait.LOCK_DIR, exist_ok=True)
    script = tmp_path / "gpu_busy_cli.py"
    script.write_text(f"import sys\nsys.path.insert(0, {str(SCRIPTS)!r})\nimport gpu_busy, gpu_wait\n"
                      f"gpu_busy.REG_DIR = {gpu_busy.REG_DIR!r}\ngpu_wait.LOCK_DIR = {gpu_wait.LOCK_DIR!r}\n"
                      f"gpu_wait.cuda_uuids = lambda: {UUID!r}\ngpu_busy.main()\n")
    return [sys.executable, str(script)]


def names(launches):
    return [name for name, *_ in launches]


def holds_of(name):
    return next((holds for n, _, _, _, holds, _ in gpu_busy.launches() if n == name), None)


def asked(out):
    return [line.split()[0] for line in out.splitlines()]


@pytest.fixture
def launch(cli):
    procs = []

    def start(name, *cmd, exclude=""):
        p = subprocess.Popen([*cli, "register", name, *(["--exclude", exclude] if exclude else []), "--",
                              *(cmd or ("sleep", "60"))],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        procs.append(p)
        if not cmd:   # registered once its time reads, which is written with its cards
            wait_for(lambda: any(n == name and ns for n, ns, *_ in gpu_busy.launches()))
        return p
    yield start
    for p in procs:
        p.kill()
        p.wait()


def gpu_wait_cli(path, busy):
    """``gpu_wait.py`` as a command that finds CUDA 1 free and claims it, in the box's LOCK_DIR,
    with ``busy`` its gpu_busy.py; its job sleeps a minute."""
    path.write_text(f"import sys\nsys.path.insert(0, {str(SCRIPTS)!r})\nimport gpu_wait\n"
                    f"gpu_wait.LOCK_DIR = {gpu_wait.LOCK_DIR!r}\ngpu_wait.BUSY = {str(busy)!r}\n"
                    "gpu_wait.ecc_bad_gpus = set\ngpu_wait.query_free_mb = lambda: {1: 40000}\ngpu_wait.main()\n")
    return [sys.executable, str(path), "--need-gb", "1", "--", "sleep", "60"]


@pytest.fixture
def waiter(tmp_path, box, cli):
    """A gpu_wait whose gpu_busy.py is the command on the scratch launches."""
    return gpu_wait_cli(tmp_path / "gpu_wait_cli.py", cli[1])


def test_a_launch_lives_as_long_as_anything_that_inherited_it(launch):
    first = launch("run-a", "sh", "-c", "sleep 60 & echo $!")
    heir = int(first.stdout.readline())
    first.wait()
    assert names(gpu_busy.launches()) == ["run-a"]   # its command is gone, the background child still holds it
    second = launch("run-a")
    assert second.wait(timeout=30) == gpu_busy.ALREADY_ALIVE and "alive" in second.stderr.read()
    os.kill(heir, 9)
    wait_for(lambda: gpu_busy.launches() == [])
    launch("run-a")


def test_a_registration_file_nobody_holds_is_no_launch(cli):
    os.makedirs(gpu_busy.REG_DIR)
    Path(gpu_busy.REG_DIR, "run-old").write_text("1 pid 2: bash runner.sh\n")
    assert gpu_busy.launches() == [] and gpu_busy.ahead() == []


def test_launches_queue_first_come_first_served(cli, launch):
    launch("earlier")
    fd = gpu_busy.take("mine", "this test", None)   # held by this process: its own launch, on every card
    try:
        launch("later")
        assert names(gpu_busy.launches()) == ["earlier", "later", "mine"]
        assert names(gpu_busy.ahead()) == ["earlier"]
    finally:
        os.close(fd)
    assert names(gpu_busy.ahead()) == ["earlier", "later"]


def test_only_earlier_launches_that_may_take_my_cards_are_ahead(cli, launch):
    launch("on-1-2", exclude="0,3")
    launch("on-3", exclude="0,1,2")   # waits for a card mine never takes: mine goes past it
    launch("on-all")
    fd = gpu_busy.take("mine", "this test", frozenset({2}))
    try:
        launch("later", exclude="0,1,3")
        assert names(gpu_busy.ahead()) == ["on-1-2", "on-all"]
        outside = subprocess.run([*cli, "ahead"], capture_output=True, text=True)   # no fd: outside any launch, behind all
        assert outside.returncode == 1 and asked(outside.stdout) == ["later", "mine", "on-1-2", "on-3", "on-all"]
    finally:
        os.close(fd)


def test_a_launch_that_claimed_is_ahead_of_nobody(box, cli, launch, waiter):
    """Waiting, a launch is ahead of later ones on its candidate cards; once its gpu_wait has
    claimed, the claim lock keeps its card from them and it is in nobody's way."""
    waiting = launch("waiting")
    launch("running", *waiter)
    wait_for(lambda: holds_of("running") == {1})
    fd = gpu_busy.take("mine", "this test", None)
    try:
        assert names(gpu_busy.ahead()) == ["waiting"] and claimable() == [0, 2, 3]
    finally:
        os.close(fd)
    outside = subprocess.run([*cli, "ahead"], capture_output=True, text=True)   # it sees every launch, waits for the waiting
    assert outside.returncode == 1 and asked(outside.stdout) == ["running", "waiting"] and "claimed cards 1," in outside.stdout
    waiting.kill()
    waiting.wait()
    outside = subprocess.run([*cli, "ahead"], capture_output=True, text=True)
    assert outside.returncode == 0 and asked(outside.stdout) == ["running"]


def test_a_launch_that_claimed_keeps_no_place_between_batches(box, launch, waiter):
    """Its batch done and its card free again, a launch that claimed still waits ahead of nobody:
    a series that must keep its cards runs under one gpu_wait --hold."""
    batch = shlex.join([*waiter[:5], "true"])
    between = launch("batches", "sh", "-c", f"{batch} > /dev/null && echo between && exec sleep 60")
    assert between.stdout.readline() == "between\n" and holds_of("batches") == {1}
    wait_for(lambda: claimable() == [0, 1, 2, 3])
    fd = gpu_busy.take("mine", "this test", None)
    try:
        assert gpu_busy.ahead() == []
    finally:
        os.close(fd)


def test_each_claim_replaces_the_cards_a_launch_holds(cli, launch):
    """A second gpu_wait under one launch (its next stage) records what it claims in place of
    the first one's; one outside the launch, as in its job, records nothing on it."""
    claim = shlex.join([*cli, "claimed"])
    stages = launch("stages", "sh", "-c", f"{claim} 2,3 && {claim} 1 && echo claimed && exec sleep 60", exclude="0")
    assert stages.stdout.readline() == "claimed\n"
    subprocess.run([*cli, "claimed", "0"], check=True)
    assert [reg[3:5] for reg in gpu_busy.launches()] == [(frozenset({1, 2, 3}), frozenset({1}))]


def test_a_launch_asks_from_its_own_command(cli, launch):
    ask = f"{shlex.join([*cli, 'ahead'])}; echo rc=$?"
    assert launch("solo", "sh", "-c", ask).communicate(timeout=30)[0] == "rc=0\n"   # it does not wait for itself
    launch("earlier", exclude="0")
    assert launch("on-0", "sh", "-c", ask, exclude="1,2,3").communicate(timeout=30)[0] == "rc=0\n"
    assert asked(launch("on-3", "sh", "-c", ask, exclude="0,1,2").communicate(timeout=30)[0]) == ["earlier", "rc=1"]


def test_a_registration_without_readable_cards_is_on_every_card(cli, other_holder):
    """One another writer left without readable cards is on every card, and one without a
    readable holds line still waits; a time that does not read is before mine."""
    os.makedirs(gpu_busy.REG_DIR)
    for name, text in [("old", "1 pid 2: bash runner.sh\n"), ("empty", ""), ("cut-time", "17"),
                       ("cut-cards", "1 pid 2: bash runner.sh\ncards 1"),   # of "cards 1,2\n"
                       ("garbled", "1 pid 2: bash runner.sh\ncards one\n"),
                       ("cut-holds", "1 pid 2: bash runner.sh\ncards 2\nholds 2"),
                       ("garbled-holds", "1 pid 2: bash runner.sh\nholds two\n")]:
        Path(gpu_busy.REG_DIR, name).write_text(text)
        other_holder(os.path.join(gpu_busy.REG_DIR, name))
    fd = gpu_busy.take("mine", "this test", frozenset({2}))
    try:
        assert names(gpu_busy.ahead()) == ["cut-cards", "cut-holds", "cut-time", "empty", "garbled", "garbled-holds", "old"]
    finally:
        os.close(fd)


def test_older_copies_read_a_registration_as_before_its_claim(cli):
    """They read its first line, "<ns> <rest>", and the newer of them the cards on its second."""
    def as_they_read(name):   # line 2 gives them cards only when it is a whole "cards" line
        with open(os.path.join(gpu_busy.REG_DIR, name), "rb") as f:
            first, second = (f.readline().decode(errors="replace") for _ in range(2))
        tag, _, text = second.partition(" ")
        return first, gpu_busy.ordinals(text) if tag == "cards" and text.endswith("\n") else None
    t0 = time.time_ns()
    fds = [gpu_busy.take("mine", "bash -c 'a\nb'", frozenset({1})), gpu_busy.take("plain", "bash runner.sh", None)]
    try:
        before = [as_they_read("mine"), as_they_read("plain")]
        gpu_busy.claimed(frozenset({1}))
        assert [as_they_read("mine"), as_they_read("plain")] == before
        ns, _, rest = before[0][0].strip().partition(" ")
        assert t0 <= int(ns) <= time.time_ns() and rest.endswith(": bash -c 'a b'") and before[0][1] == frozenset({1})
        assert [reg[3:5] for reg in gpu_busy.launches()] == [(frozenset({1}), frozenset({1})), (None, frozenset({1}))]
    finally:
        for fd in fds:
            os.close(fd)


def test_a_launch_whose_gpu_wait_records_nothing_holds_the_card_it_claimed(box, cli):
    """An older gpu_wait writes no holds line, but the process holding its claim lock has the
    registration open: the launch holds that card and is in nobody's way."""
    os.makedirs(gpu_busy.REG_DIR)
    old = subprocess.Popen([sys.executable, "-c", OLD_LAUNCH, os.path.join(gpu_busy.REG_DIR, "old"), gpu_wait.claim_lock(1)],
                           stdout=subprocess.PIPE, text=True)
    try:
        assert old.stdout.readline() == "held\n" and holds_of("old") == {1}
        fd = gpu_busy.take("mine", "this test", None)
        try:
            assert gpu_busy.ahead() == []
        finally:
            os.close(fd)
    finally:
        old.kill()
        old.wait()
    wait_for(lambda: gpu_busy.launches() == [])


def test_the_claim_holders_keep_the_launch_their_gpu_wait_ran_under(tmp_path, box, launch):
    """Their gpu_wait gone into its job, the claim holders keep the registration open: with no
    holds line, the launch still holds their cards."""
    (tmp_path / "records_nothing.py").write_text("")
    launch("running", *gpu_wait_cli(tmp_path / "silent_gpu_wait.py", tmp_path / "records_nothing.py"))
    wait_for(lambda: holds_of("running") == {1})
    assert "holds" not in Path(gpu_busy.REG_DIR, "running").read_text()
    assert gpu_wait.claim_holder(gpu_wait.claim_lock(1)) is not None


def test_register_refuses_set_aside_that_leaves_no_card(cli):
    r = subprocess.run([*cli, "register", "none", "--exclude", "0,1,2,3", "--", "true"], capture_output=True, text=True)
    assert r.returncode == 2 and "no CUDA ordinal" in r.stderr and gpu_busy.launches() == []
