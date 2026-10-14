"""Construct real finalized launch records for caller-boundary tests."""

from uxon.infra import launch_records, tmux


def save_launch_record(
    cfg,
    resolved,
    *,
    target_dir,
    name="uxon-repo@claude",
    nonce="a" * 32,
    session_id="$1",
    created="100",
    runtime_id="cid-1",
    epoch="epoch-1",
    cgroup="/box.scope",
):
    pending = launch_records.pending_from_resolved(
        socket_path=tmux.tmux_socket_path(cfg, resolved.launch_user),
        session_name=name,
        resolved=resolved,
        target_dir=target_dir,
        nonce=nonce,
    )
    metadata = launch_records.TmuxSessionMetadata(session_id, created, name, nonce)
    launch_records.create_pending_record(pending)
    path = launch_records.finalize_pending_record(
        pending, metadata, runtime_id=runtime_id, runtime_epoch=epoch, runtime_cgroup=cgroup
    )
    return pending, metadata, path


def session_from_record(pending, metadata):
    """Read authority through its verifier before constructing a captured row."""
    from helpers import make_session

    payload = launch_records.read_verified_record(pending.socket_path, metadata)
    assert payload is not None
    session = make_session(pending.session_name, user=pending.launch_user)
    for field in (
        "agent",
        "profile",
        "launch_user",
        "launch_nonce",
        "execution_backend",
        "execution_fingerprint",
        "runtime",
        "runtime_kind",
        "runtime_resource",
        "runtime_fingerprint",
        "runtime_dir",
        "project_slug",
        "runtime_id",
        "runtime_cgroup",
        "runtime_epoch",
        "tmux_session_id",
        "tmux_session_created",
    ):
        setattr(session, field, payload[field])
    session.launch_record_verified = True
    return session
