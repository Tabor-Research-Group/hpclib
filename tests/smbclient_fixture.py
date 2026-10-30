"""Synthetic KDC, runtime and SMB server; never uses real credentials/network."""

FAKE_TOOL = r'''
import datetime, json, os, pathlib, shlex, shutil, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
cache = os.environ.get('KRB5CCNAME', 'KCM:')
def record(kind, **fields):
    if os.environ.get('FAKE_EVENTS'):
        with open(os.environ['FAKE_EVENTS'], 'a') as out:
            out.write(json.dumps(dict(kind=kind, args=args, cache=cache, **fields)) + '\n')
def valid():
    if cache.startswith('KCM:'): return True
    try: return json.loads(pathlib.Path(cache[5:]).read_text()).get('valid', False)
    except (OSError, ValueError): return False
if name == 'stat':
    print('tmpfs'); sys.exit(0)
if name == 'kinit':
    record('kinit', config=os.environ.get('KRB5_CONFIG'))
    assert cache.startswith('FILE:')
    if os.environ.get('FAKE_EXPECT_PASSWORD'):
        assert sys.stdin.readline().rstrip('\n') == os.environ['FAKE_EXPECT_PASSWORD']
    pathlib.Path(cache[5:]).write_text(json.dumps({'valid': True}))
    if os.environ.get('FAKE_KINIT_FAIL'):
        pathlib.Path(cache[5:]).write_text('bad candidate')
        sys.exit('kinit: Password incorrect')
    sys.exit(0)
if name == 'klist':
    if '-V' in args: print('Kerberos fixture'); sys.exit(0)
    if not valid(): sys.exit(1)
    if '-s' not in args:
        print('Ticket cache: ' + cache)
        print('Default principal: me@AUTH.EXAMPLE.EDU')
        print('10/29/2026 14:00:00 10/29/2026 23:00:00 krbtgt/AUTH.EXAMPLE.EDU@AUTH.EXAMPLE.EDU')
    sys.exit(0)
if name == 'kvno':
    record('kvno'); sys.exit(os.environ.get('FAKE_KVNO_ERROR', 'kvno: Server not found in Kerberos database'))
if name == 'kdestroy':
    pathlib.Path(cache[5:]).unlink(missing_ok=True); sys.exit(0)
if name not in ('singularity', 'apptainer'): sys.exit('unexpected fake tool: ' + name)
mode, rest = args[0], args[1:]
binds = []
while rest and rest[0] == '--bind':
    binds.append(rest[1]); rest = rest[2:]
image, command = rest[0], rest[1:]
prefix = 'APPTAINERENV_' if name == 'apptainer' else 'SINGULARITYENV_'
inside_cache = os.environ.get(prefix + 'KRB5CCNAME', 'KCM:')
inside_config = os.environ.get(prefix + 'KRB5_CONFIG', '/missing/image/krb5.conf')
if command and command[0] == 'env':
    command = command[1:]
    while command:
        if command[0] == '-u': command = command[2:]; continue
        if '=' in command[0]:
            key, value = command.pop(0).split('=', 1)
            if key == 'KRB5CCNAME': inside_cache = value
            if key == 'KRB5_CONFIG': inside_config = value
            continue
        break
text = ''
if inside_cache.startswith('FILE:'):
    parent = str(pathlib.Path(inside_cache[5:]).parent)
    assert parent + ':' + parent + ':rw' in binds, 'cache was not mounted'
    config_bind = next((b for b in binds if b.endswith(':' + inside_config + ':ro')), None)
    assert config_bind, 'profile was not mounted'
    text = pathlib.Path(config_bind.rsplit(':', 2)[0]).read_text()
record('image', command=command, binds=binds, inside_cache=inside_cache, profile=text,
       kerberos=os.environ.get('RCLONE_CONFIG_SMB_USE_KERBEROS'),
       has_password=bool(os.environ.get('RCLONE_CONFIG_SMB_PASS')))
if mode == 'shell': sys.exit(0)
if command[:2] == ['smbclient', '--version']:
    if os.environ.get('FAKE_NO_SMB'): sys.exit(1)
    print('Version 4.17.12'); sys.exit(0)
if command[:2] == ['rclone', '--config=/dev/null']:
    command = [command[0]] + command[2:]
if command[:2] == ['rclone', 'version']:
    if os.environ.get('FAKE_NO_RCLONE'): sys.exit(1)
    print('rclone fixture'); sys.exit(0)
if command[:2] == ['rclone', 'obscure']:
    assert len(command) == 3 and command[2] == '-'
    assert sys.stdin.read() == os.environ.get('FAKE_EXPECT_PASSWORD', 'synthetic-password')
    print('encoded-synthetic-password'); sys.exit(0)
if command[:2] == ['rclone', 'rcd']:
    assert '--smb-use-kerberos=false' in command
    assert not any(key.startswith(('RCLONE_SMB_', 'RCLONE_CONFIG_')) for key in os.environ)
    config = pathlib.Path(command[command.index('--config') + 1])
    data = config.read_text()
    assert 'use_kerberos = false' in data and 'use_kerberos = true' not in data
    assert 'pass = encoded-synthetic-password' in data
    assert config.stat().st_mode & 0o777 == 0o600
    assert config.parent.stat().st_mode & 0o777 == 0o700
    record('gui', config_path=str(config), password_in_argv=any(os.environ.get('FAKE_EXPECT_PASSWORD', 'never') in a for a in command))
    if os.environ.get('FAKE_GUI_WAIT'):
        import time
        time.sleep(60)
    sys.exit(0)
if command[0] != 'smbclient': sys.exit('unexpected image command: ' + repr(command))
assert '--use-kerberos=required' in command and '--no-pass' in command
assert inside_cache.startswith('FILE:') and pathlib.Path(inside_cache[5:]).is_file()
assert not any(k.startswith('RCLONE_') for k in os.environ), 'rclone credentials reached Samba'
if os.environ.get('FAKE_SMB_ERROR'):
    print(os.environ['FAKE_SMB_ERROR']); sys.exit(0 if os.environ.get('FAKE_ZERO_EXIT') else 1)
server = pathlib.Path(os.environ['FAKE_SMB'])
service = next((v for v in command if v.startswith('//')), None)
if service and os.environ.get('FAKE_SMB_FQDN_UNKNOWN') and '.' in service.split('/')[2]:
    print('Server principal cifs/' + service.split('/')[2] + '@AUTH.EXAMPLE.EDU not found')
    sys.exit('session setup failed: NT_STATUS_INVALID_PARAMETER')
if not service:
    for p in sorted(server.iterdir()):
        if p.is_dir(): print('Disk|' + p.name + '|fixture')
    sys.exit(0)
share = service.split('/', 3)[3]
root = server / share
text_command = next(v.split('=', 1)[1] for v in command if v.startswith('--command='))
words = shlex.split(text_command)
op = words[0]
def remote(value):
    p = root / value.replace('\\', '/').lstrip('/')
    if not p.is_relative_to(root): sys.exit('unsafe fixture path')
    return p
try:
    if op == 'ls':
        p = remote(words[1].removesuffix('*').rstrip('\\'))
        if not p.is_dir(): raise OSError('NT_STATUS_OBJECT_PATH_NOT_FOUND')
        for name, item in [('.', p), ('..', p.parent)] + [(x.name, x) for x in sorted(p.iterdir())]:
            attrs = 'D' if item.is_dir() else 'A'
            size = 0 if item.is_dir() else item.stat().st_size
            print(f'  {name:<30}{attrs:>7} {size:8d}  Thu Oct 29 12:00:00 2026')
        print('100 blocks of size 4096. 50 blocks available')
    elif op == 'get': shutil.copyfile(remote(words[1]), words[2])
    elif op == 'put': shutil.copyfile(words[1], remote(words[2]))
    elif op == 'mkdir': remote(words[1]).mkdir()
    elif op == 'rename': os.replace(remote(words[1]), remote(words[2]))
    elif op == 'del': remote(words[1]).unlink()
    elif op == 'rmdir': remote(words[1]).rmdir()
    elif op != 'quit': raise OSError('unknown operation: ' + op)
except OSError as e:
    sys.exit('NT_STATUS_UNSUCCESSFUL ' + str(e))
'''
