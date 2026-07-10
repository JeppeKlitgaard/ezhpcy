from contextlib import contextmanager

from ezhpcy.patch import sdist


def test_sdist_for_current_installation_builds_for_editable_install(
    monkeypatch,
    tmp_path,
) -> None:
    sdist_path = tmp_path / "ezhpcy-0.1.0.tar.gz"
    sdist_path.write_bytes(b"sdist")

    @contextmanager
    def fake_build(project_root):
        yield sdist_path

    monkeypatch.setattr(sdist, "is_editable_install", lambda: True)
    monkeypatch.setattr(sdist, "editable_project_root", lambda: tmp_path)
    monkeypatch.setattr(sdist, "_build_sdist_from_project", fake_build)

    with sdist.sdist_for_current_installation() as resolved_sdist:
        assert resolved_sdist.name == "ezhpcy-0.1.0.tar.gz"
        assert resolved_sdist.read_bytes() == b"sdist"


def test_sdist_for_current_installation_uses_bundled_sdist(monkeypatch) -> None:
    class FakeSdist:
        name = "ezhpcy-0.1.0.tar.gz"

    monkeypatch.setattr(sdist, "is_editable_install", lambda: False)
    monkeypatch.setattr(sdist, "_bundled_sdist", lambda: FakeSdist())

    with sdist.sdist_for_current_installation() as resolved_sdist:
        assert resolved_sdist.name == "ezhpcy-0.1.0.tar.gz"
