"""화면 전환과 로컬 아이콘 자산 회귀 검사."""
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_tabler_assets():
    icons = ROOT / 'app/static/icons'
    for path in icons.glob('*.svg'):
        assert ET.parse(path).getroot().tag.endswith('svg')
    html = (ROOT / 'app/static/index.html').read_text()
    for name in re.findall(r'/static/icons/([^" ]+)', html):
        assert (icons / name).is_file()
    assert 'MIT' in (icons / 'LICENSE').read_text()


def test_navigation_after_import():
    if not shutil.which('node'):
        pytest.skip('Node is required for the frontend regression check')
    script = r'''
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const source = fs.readFileSync('app/static/app.js', 'utf8').split('// ───────── 시작')[0];
const context = vm.createContext({
  document: { body: { setAttribute(){}, removeAttribute(){} }, control: {disabled:false},
    querySelectorAll(selector){return selector.startsWith("main ") ? [this.control] : [];} },
});
vm.runInContext(source, context);
(async () => {
  await vm.runInContext(`(async () => {
    let renders = 0;
    renderHearing = async () => { renders++; };
    await guarded(async () => {
      if (!document.control.disabled) throw new Error('Controls not locked during request');
      showTab('hearing');
    });
    if (document.control.disabled) throw new Error('Controls not restored');
    await Promise.resolve();
    if (renders !== 1 || state.busy || state.pendingTab) throw new Error('Navigation was dropped');
  })()`, context);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    subprocess.run(['node', '-e', script], cwd=ROOT, check=True, capture_output=True, text=True)


def test_chat_shows_message_before_reply_and_restores_failure():
    if not shutil.which('node'):
        pytest.skip('Node is required for the frontend regression check')
    script = r'''
const fs = require('fs');
const vm = require('vm');
const context = vm.createContext({document: {
  body: {setAttribute(){}, removeAttribute(){}}, querySelectorAll(){return [];}
}});
vm.runInContext(fs.readFileSync('app/static/app.js','utf8').split('// ───────── 시작')[0], context);
vm.runInContext(`
el = (tag, attrs, ...children) => {
  const node = {tag, attrs: attrs || {}, children: children.flat().filter(Boolean), value: '', disabled: false,
    append(...items){this.children.push(...items);}, remove(){this.removed = true;},
    addEventListener(){}, focus(){},
    querySelectorAll(){return flatten(this).filter(n => ['button','input','textarea'].includes(n.tag));}};
  return node;
};
function flatten(node){return [node, ...(node.children || []).flatMap(c => typeof c === 'object' ? flatten(c) : [])];}
notify = () => {};
let draws = 0;
drawHearing = () => { draws++; };
state.settings = {};
async function check(fail){
  const c = {id:'c', facts:{merchants:[], transactions:[]}, turns:[{role:'ai',text:'첫 질문'}], status:'open', count:3, avg_krw:20000, total_krw:60000};
  state.hearing = {id:'h', cases:[c]};
  let resolve, reject;
  api = method => method === 'POST' ? new Promise((yes,no) => {resolve=yes; reject=no;}) : Promise.resolve({id:'h', cases:[c]});
  const panel = chatPanel(c, {}, false);
  const nodes = flatten(panel);
  const input = nodes.find(n => n.tag === 'textarea');
  input.value = '내 의견';
  const button = nodes.find(n => n.tag === 'button' && n.children.includes('보내기'));
  const pending = button.attrs.onclick();
  const log = nodes.find(n => n.attrs.class === 'chat-log');
  if (!flatten(log).some(n => n.attrs?.text === '내 의견')) throw Error('Message missing before response');
  if (!flatten(log).some(n => n.attrs?.class === 'reply-wait')) throw Error('Waiting indicator missing');
  if (input.value !== '' || !input.disabled) throw Error('Input not locked');
  if (fail) reject(Error('network failed')); else resolve({id:'h', cases:[c]});
  await pending;
  if (fail && (input.value !== '내 의견' || input.disabled)) throw Error('Draft not restored');
  if (!fail && draws !== 1) throw Error('Server result not rendered');
}
(async () => { await check(false); await check(true); })()
`, context).catch(error => {console.error(error); process.exitCode=1;});
'''
    subprocess.run(['node', '-e', script], cwd=ROOT, check=True, capture_output=True, text=True)
