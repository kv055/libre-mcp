"""Tests for display-environment recovery (live-mode robustness)."""

import os
import socket

from src.office import display


def _bind_socket(path):
    """Create a real unix-socket file; the path stays usable after close."""
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(path))
    sock.close()
    return path


def test_keeps_explicit_wayland_display(tmp_path):
    _bind_socket(tmp_path / "wayland-9")
    env = {"WAYLAND_DISPLAY": "wayland-9", "XDG_RUNTIME_DIR": str(tmp_path)}
    info = display.ensure_display(env=env)
    assert info.kind == "wayland"
    assert env["WAYLAND_DISPLAY"] == "wayland-9"


def test_keeps_explicit_display():
    env = {"DISPLAY": ":0"}
    info = display.ensure_display(env=env, runtime_dir="/nonexistent")
    assert info.kind == "x11"
    assert info.detail == ":0"
    assert env == {"DISPLAY": ":0"}


def test_stale_wayland_display_is_rediscovered(tmp_path):
    _bind_socket(tmp_path / "wayland-1")
    env = {"WAYLAND_DISPLAY": "wayland-9"}
    info = display.ensure_display(env=env, runtime_dir=str(tmp_path))
    assert info.kind == "wayland"
    assert env["WAYLAND_DISPLAY"] == "wayland-1"


def test_reconstructs_wayland_socket(tmp_path):
    _bind_socket(tmp_path / "wayland-1")
    env = {}
    info = display.ensure_display(env=env, runtime_dir=str(tmp_path))
    assert info.kind == "wayland"
    assert env["WAYLAND_DISPLAY"] == "wayland-1"
    assert env["XDG_RUNTIME_DIR"] == str(tmp_path)


def test_picks_first_socket_in_sorted_order(tmp_path):
    _bind_socket(tmp_path / "wayland-0")
    _bind_socket(tmp_path / "wayland-1")
    env = {}
    info = display.ensure_display(env=env, runtime_dir=str(tmp_path))
    assert info.kind == "wayland"
    assert env["WAYLAND_DISPLAY"] == "wayland-0"


def test_picks_whatever_socket_exists(tmp_path):
    _bind_socket(tmp_path / "wayland-2")
    env = {}
    info = display.ensure_display(env=env, runtime_dir=str(tmp_path))
    assert info.kind == "wayland"
    assert env["WAYLAND_DISPLAY"] == "wayland-2"


def test_macos_needs_no_display_recovery(monkeypatch):
    monkeypatch.setattr(display.sys, "platform", "darwin")
    env = {}
    info = display.ensure_display(env=env, runtime_dir="/nonexistent")
    assert info.kind == "native"
    assert env == {}


def test_ignores_non_socket_files(tmp_path):
    (tmp_path / "wayland-1").write_text("stale junk, not a socket")
    env = {}
    info = display.ensure_display(env=env, runtime_dir=str(tmp_path))
    assert info.kind == "none"
    assert env == {}


def test_recovers_newest_hyprland_signature(tmp_path):
    _bind_socket(tmp_path / "wayland-1")
    hypr = tmp_path / "hypr"
    hypr.mkdir()
    old = hypr / "aaa_old"
    new = hypr / "bbb_new"
    old.mkdir()
    new.mkdir()
    os.utime(old, (1000000000, 1000000000))
    os.utime(new, (1100000000, 1100000000))
    env = {}
    info = display.ensure_display(env=env, runtime_dir=str(tmp_path))
    assert info.kind == "wayland"
    assert env["HYPRLAND_INSTANCE_SIGNATURE"] == "bbb_new"


def test_keeps_existing_hyprland_signature(tmp_path):
    _bind_socket(tmp_path / "wayland-1")
    hypr = tmp_path / "hypr"
    hypr.mkdir()
    (hypr / "other").mkdir()
    env = {"HYPRLAND_INSTANCE_SIGNATURE": "mine"}
    info = display.ensure_display(env=env, runtime_dir=str(tmp_path))
    assert info.kind == "wayland"
    assert env["HYPRLAND_INSTANCE_SIGNATURE"] == "mine"


def test_no_display_leaves_env_untouched(tmp_path):
    env = {}
    info = display.ensure_display(env=env, runtime_dir=str(tmp_path))
    assert info.kind == "none"
    assert env == {}


def test_runtime_dir_defaults_to_xdg_runtime_dir(tmp_path, monkeypatch):
    _bind_socket(tmp_path / "wayland-1")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    env = {}
    info = display.ensure_display(env=env)
    assert info.kind == "wayland"
    assert env["WAYLAND_DISPLAY"] == "wayland-1"
