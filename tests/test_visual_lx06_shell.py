"""用隔离的 UBus/curl/jshn 替身执行完整 LX06 shell，验证归属与清理分支。

状态格式按固件反汇编构造，不冒充实机采集。没有音箱连接或真实 UBus 命令。
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


FAKE = r'''
import json,os,sys,time
from pathlib import Path
root=Path(os.environ['VISUAL_TEST_ROOT'])
name=Path(sys.argv[0]).name
scenario=os.environ['VISUAL_TEST_SCENARIO']
if name=='jsonq':
    data=json.loads(sys.argv[1])
    for part in sys.argv[2].split('/'):
        if part:data=data[int(part)-1] if isinstance(data,list) else data[part]
    action=sys.argv[3]
    if action!='keys': data=data.get(sys.argv[4]) if isinstance(data,dict) else None
    if action=='type':print({str:'string',int:'int',list:'array',dict:'object'}.get(type(data),'null'))
    elif action=='keys':print(' '.join(str(i+1) for i in range(len(data))) if isinstance(data,list) else '')
    elif data is not None:print(data if isinstance(data,str) else json.dumps(data))
elif name=='curl':
    if scenario=='foreign':time.sleep(1)
    sys.stdout.write('000'+({'takeover':'P','broken':''}.get(scenario,'C')))
elif name=='ubus':
    service,method=sys.argv[4:6]
    with (root/'calls').open('a') as f:f.write(service+' '+method+'\n')
    if service=='mediaplayer':print(json.dumps({'code':0,'info':json.dumps({'status':1 if scenario=='busy' else 0})}))
    elif method=='show':
        (root/'state').write_text(sys.argv[6]);print('{"code":0}')
    elif method=='shut':print('{"code":0}')
    elif method=='status':
        if scenario=='malformed':print('{"info":false}');sys.exit()
        state=root/'state'
        if state.exists():
            args=json.loads(state.read_text());effect={'type':args['L'],'pos':args['pos'],'rgb':'0 0 0'}
            if scenario=='foreign':effect['type']=7
            print(json.dumps({'info':json.dumps({'effect':[effect]})}))
        else:print('{"info":""}')
'''

JSHN = r'''
json_load() { JROOT=$1; JPATH=; }
json_get_type() { export "$1=$(jsonq "$JROOT" "$JPATH" type "$2")"; }
json_get_var() { export "$1=$(jsonq "$JROOT" "$JPATH" get "$2")"; }
json_select() { JPATH="$JPATH/$1"; }
json_get_keys() { export "$1=$(jsonq "$JROOT" "$JPATH" keys)"; }
'''


@unittest.skipIf(sys.platform == 'win32', '完整 shell 模拟在 Linux 离线容器运行')
class Lx06ShellTests(unittest.TestCase):
    def run_phase(self, scenario, mode='listening'):
        source=(Path(__file__).parents[1]/'core/services/lx06_visual_phase.sh').read_text()
        source=source.replace('. /usr/share/libubox/jshn.sh || exit 10', JSHN)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('ubus','curl','jsonq'):
                path=root/name
                path.write_text('#!'+sys.executable+'\n'+FAKE)
                path.chmod(0o755)
            # 测试的临时控制文件也限制在隔离目录。
            source=source.replace('/tmp/bridge-lx06-visual.XXXXXX', str(root/'control.XXXXXX'))
            result=subprocess.run(['/bin/sh','-c',source,'sh',mode,'http://unused.invalid', '2'],
                                  env={**os.environ,'PATH':directory+os.pathsep+os.environ['PATH'],
                                       'VISUAL_TEST_ROOT':directory,'VISUAL_TEST_SCENARIO':scenario},
                                  capture_output=True,text=True,timeout=12)
            calls=(root/'calls').read_text() if (root/'calls').exists() else ''
            self.assertEqual(list(root.glob('control.*')), [])
            return result,calls

    def test_normal_listening_and_thinking_clear_only_own_effect(self):
        for mode in ('listening','thinking'):
            with self.subTest(mode=mode):
                result,calls=self.run_phase('normal',mode)
                self.assertEqual(result.returncode,0,result.stderr+result.stdout)
                self.assertEqual(calls.count('led show'),1)
                self.assertEqual(calls.count('led shut'),1)
                self.assertNotIn('shut_all',calls)

    def test_native_takeover_never_shuts_light(self):
        result,calls=self.run_phase('takeover')
        self.assertEqual(result.returncode,26,result.stderr+result.stdout)
        self.assertNotIn('led shut',calls)

    def test_changed_state_never_shuts_light(self):
        result,calls=self.run_phase('foreign')
        self.assertEqual(result.returncode,27,result.stderr+result.stdout)
        self.assertNotIn('led shut',calls)

    def test_missing_end_marker_does_not_guess_ownership(self):
        result,calls=self.run_phase('broken')
        self.assertEqual(result.returncode,24,result.stderr+result.stdout)
        self.assertNotIn('led shut',calls)

    def test_busy_player_or_malformed_status_never_changes_lights(self):
        for scenario in ('busy','malformed'):
            with self.subTest(scenario=scenario):
                result,calls=self.run_phase(scenario)
                self.assertEqual(result.returncode,20,result.stderr+result.stdout)
                self.assertNotIn('led show',calls)
                self.assertNotIn('led shut',calls)

    def test_check_mode_is_read_only(self):
        result,calls=self.run_phase('normal','check')
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        self.assertNotIn('led show',calls)
        self.assertNotIn('led shut',calls)
