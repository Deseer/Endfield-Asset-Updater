from pathlib import Path

from zmd_resource_service.manifest import pack_filename, package_bytes, sanitize_manifest


def sample_manifest():
    return {
        "version": "1.2.3",
        "pkg": {
            "total_size": "300",
            "packs": [
                {
                    "url": "https://cdn.example/game.zip.001?auth_key=secret",
                    "md5": "0" * 32,
                    "package_size": "100",
                },
                {
                    "url": "https://cdn.example/game.zip.002?auth_key=secret",
                    "md5": "1" * 32,
                    "package_size": "200",
                },
            ],
        },
    }


def test_sanitize_manifest_removes_signed_queries():
    clean = sanitize_manifest(sample_manifest())
    assert clean["pkg"]["packs"][0]["url"] == "https://cdn.example/game.zip.001"


def test_package_helpers():
    manifest = sample_manifest()
    assert package_bytes(manifest) == 300
    assert pack_filename(manifest["pkg"]["packs"][1]) == "game.zip.002"
