import json
import urllib.error

import pytest

from zmd_resource_service import cli, manifest, resources
from zmd_resource_service.config import source_config
from zmd_resource_service.privacy import public_summary, redact_text


def configure(tmp_path, monkeypatch):
    path = tmp_path / 'service.json'
    value = {'launcherUrl': 'https://launcher.example/api/game/get_latest',
             'appCode': 'test-app', 'channel': '1', 'subChannel': '2'}
    path.write_text(json.dumps(value))
    monkeypatch.setenv('ENDFIELD_SERVICE_CONFIG_PATH', str(path))
    return value


def test_private_source_config_is_required(tmp_path, monkeypatch):
    monkeypatch.setenv('ENDFIELD_SERVICE_CONFIG_PATH', str(tmp_path / 'missing.json'))
    with pytest.raises(ValueError, match='private service JSON'):
        source_config()


@pytest.mark.parametrize('url', ['http://launcher.example/api',
                                  'https://launcher.example/api?token=private',
                                  'https://user:password@launcher.example/api'])
def test_config_rejects_credential_urls(tmp_path, monkeypatch, url):
    config = configure(tmp_path, monkeypatch)
    config['launcherUrl'] = url
    (tmp_path / 'service.json').write_text(json.dumps(config))
    with pytest.raises(ValueError, match='HTTPS endpoint'):
        source_config()


def test_fetches_use_private_configuration(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    requested = []

    class Response:
        def __init__(self, payload):
            self.payload = payload
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return json.dumps(self.payload).encode()

    payload = {'version': '1.2.3', 'pkg': {'packs': [],
               'file_path': 'https://cdn.example/1.2_example-token/files'}}

    def latest(request, **kwargs):
        requested.append(request.full_url)
        return Response(payload)

    def resource(request, **kwargs):
        requested.append(request.full_url)
        return Response({'resources': [{'name': 'main', 'version': 'r1',
                                        'path': 'https://cdn.example/files'}]})

    monkeypatch.setattr(manifest, 'open_with_retry', latest)
    monkeypatch.setattr(resources, 'open_with_retry', resource)
    manifest.fetch_latest()
    resources.fetch_resources(payload)
    assert requested[0].startswith('https://launcher.example/api/game/get_latest?')
    assert 'appcode=test-app' in requested[0]
    assert 'sub_channel=2' in requested[0]
    assert requested[1].startswith('https://launcher.example/api/game/get_latest_resources?')
    assert 'rand_str=example-token' in requested[1]


def test_nested_output_redacts_addresses():
    value = {'path': 'https://cdn.example/private-token/files',
             'items': ['failed https://cdn.example/file?auth_key=secret']}
    result = json.dumps(public_summary(value))
    assert 'https://' not in result
    assert 'private-token' not in result
    assert 'auth_key' not in result
    assert value['path'].startswith('https://')
    assert redact_text('HTTP https://cdn.example/file failed') == 'HTTP [redacted-url] failed'


def test_plan_output_contains_only_package_filename(monkeypatch, capsys, tmp_path):
    payload = {'version': '1', 'pkg': {'total_size': 1, 'packs': [
        {'url': 'https://cdn.example/private-token/game.zip?auth_key=secret',
         'md5': '0' * 32, 'package_size': 1}]}}
    class Pipeline:
        def __init__(self, *args):
            pass
        def latest(self):
            return payload
    monkeypatch.setattr(cli, 'Pipeline', Pipeline)
    monkeypatch.setattr('sys.argv', ['endfield-resource', '--data-root', str(tmp_path), 'plan'])
    assert cli.main() == 0
    output = capsys.readouterr().out
    assert json.loads(output)['first_package'] == 'game.zip'
    assert 'cdn.example' not in output
    assert 'private-token' not in output


def test_cli_error_traceback_redacts_addresses(monkeypatch, capsys):
    def fail():
        raise urllib.error.URLError('https://cdn.example/private?auth_key=secret')
    monkeypatch.setattr(cli, '_main', fail)
    assert cli.main() == 1
    error = capsys.readouterr().err
    assert 'URLError' in error
    assert 'cdn.example' not in error
    assert 'auth_key' not in error
