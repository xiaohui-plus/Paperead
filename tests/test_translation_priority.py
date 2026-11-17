import importlib
import json
from fastapi.testclient import TestClient


def test_stream_translates_only_requested_blocks_in_order_without_glossary_delay(monkeypatch, tmp_path):
    monkeypatch.setenv('PAPEREAD_DATA', str(tmp_path / 'lib'))
    monkeypatch.setenv('PAPEREAD_TOKEN', 'token')
    import server.main as main
    importlib.reload(main)
    row = main.store.add('priority.pdf', b'%PDF-1.4\n' + b'x' * 400, None)
    blocks = [dict(id=f'b{i}', order=i, page=1, kind='para', text=f'This is paragraph number {i} with enough meaningful text.', bbox=[0,0,100,20], translatable=True, translated='') for i in range(3)]
    main.store.write_blocks(row['id'], blocks)
    called = []
    async def translate(block, *args, **kwargs):
        called.append(block.id)
        return '这是一段足够长的中文译文，用于验证翻译过程。'
    async def glossary(*args, **kwargs):
        raise AssertionError('priority translation must not wait for glossary')
    monkeypatch.setattr(main, 'translate_block', translate)
    monkeypatch.setattr(main, 'build_glossary', glossary)
    payload = {'config': {'api_key':'test-key-123', 'base_url':'https://example.com/v1', 'model':'test', 'timeout':10}, 'pages':[1], 'block_ids':['b2','b1'], 'auto_highlights':False}
    with TestClient(main.app) as client:
        result = client.post(f"/api/papers/{row['id']}/translate/stream", headers={'X-Paperead-Token':'token'}, json=payload)
    assert result.status_code == 200
    assert called == ['b2', 'b1']
    assert main.store.read_blocks(row['id'])[0]['translated'] == ''



def test_failed_block_can_be_requested_again(monkeypatch, tmp_path):
    monkeypatch.setenv('PAPEREAD_DATA', str(tmp_path / 'lib'))
    monkeypatch.setenv('PAPEREAD_TOKEN', 'token')
    import server.main as main
    importlib.reload(main)
    row = main.store.add('retry.pdf', b'%PDF-1.4\n' + b'x' * 400, None)
    main.store.write_blocks(row['id'], [dict(id='b', order=0, page=1, kind='para', text='This paragraph contains a significant amount of ordinary English text.', bbox=[0,0,100,20], translatable=True, translated='')])
    calls = []
    async def translate(block, *args, **kwargs):
        calls.append(block.id)
        if len(calls) <= 2:
            raise RuntimeError('temporary failure')
        return '这是一段足够长的中文译文，用于验证恢复过程。'
    monkeypatch.setattr(main, 'translate_block', translate)
    payload = {'config': {'api_key':'test-key-123', 'base_url':'https://example.com/v1', 'model':'test', 'timeout':10}, 'pages':[1], 'block_ids':['b'], 'auto_highlights':False}
    with TestClient(main.app) as client:
        url = f"/api/papers/{row['id']}/translate/stream"
        assert client.post(url, headers={'X-Paperead-Token':'token'}, json=payload).status_code == 200
        assert not main.store.read_blocks(row['id'])[0]['translated']
        assert client.post(url, headers={'X-Paperead-Token':'token'}, json=payload).status_code == 200
    assert main.store.read_blocks(row['id'])[0]['translated']


def test_retry_sends_already_saved_translation_to_stale_reader(monkeypatch, tmp_path):
    monkeypatch.setenv('PAPEREAD_DATA', str(tmp_path / 'lib'))
    monkeypatch.setenv('PAPEREAD_TOKEN', 'token')
    import server.main as main
    importlib.reload(main)
    row = main.store.add('stale.pdf', b'%PDF-1.4\n' + b'x' * 400, None)
    main.store.write_blocks(row['id'], [dict(id='b', order=0, page=1, kind='para', text='This paragraph was already translated on the server.', bbox=[0,0,100,20], translatable=True, translated='服务端已经保存的译文。')])
    payload = {'config': {'api_key':'test-key-123', 'base_url':'https://example.com/v1', 'model':'test', 'timeout':10}, 'pages':[1], 'block_ids':['b'], 'auto_highlights':False}
    with TestClient(main.app) as client:
        result = client.post(f"/api/papers/{row['id']}/translate/stream", headers={'X-Paperead-Token':'token'}, json=payload)
    events = [json.loads(line[5:]) for line in result.text.splitlines() if line.startswith('data:')]
    assert any(e['type'] == 'block' and e['block']['translated'] == '服务端已经保存的译文。' for e in events)
