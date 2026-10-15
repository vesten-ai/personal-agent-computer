"""Private stdio MCP frontend for Codex; connects only to its parent's Unix tool socket."""
import json
import os
import socket
import sys


def relay(method, params):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(os.environ['PAC_CODEX_TOOL_SOCKET'])
        with client.makefile('rw') as stream:
            stream.write(json.dumps({'method': method, 'params': params}) + '\n')
            stream.flush()
            return json.loads(stream.readline())


def main():
    for line in sys.stdin:
        message = json.loads(line)
        if 'id' not in message:
            continue
        method = message['method']
        try:
            if method == 'initialize':
                result = {'protocolVersion': message['params']['protocolVersion'],
                          'serverInfo': {'name': 'pac-computer', 'version': '0.1.0'},
                          'capabilities': {'tools': {}}}
            elif method in ('tools/list', 'tools/call'):
                result = relay(method, message.get('params') or {})
            elif method == 'ping':
                result = {}
            else:
                raise ValueError('unsupported method')
            reply = {'id': message['id'], 'jsonrpc': '2.0', 'result': result}
        except Exception:
            reply = {'id': message['id'], 'jsonrpc': '2.0',
                     'error': {'code': -32603, 'message': 'PAC tool connection failed'}}
        print(json.dumps(reply), flush=True)


if __name__ == '__main__':
    main()
