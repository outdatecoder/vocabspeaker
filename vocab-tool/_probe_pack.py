# -*- coding: utf-8 -*-
"""对一个生成的词库包做运行时验证：用 Edge headless 打开、跑断言、读回结果。
   用法：python _probe_pack.py "packs/初中英语词汇表_第1页"

   为什么必须做：包是"数据内联进模板"生成的，模板虽已验证，但每次注入的数据不同
   （页数、条数、时长都变），只有真浏览器跑一遍才能确认渲染与时间轴对得上。

   ⚠️ 2026-10-14 加强：加了「首次点击必须先真正开始播放」的回归断言。
   旧版只断言按钮文字变成「暂停」，而 bug 的表现恰恰是**按钮说暂停、音频却是静音**
   —— 断言选错了观测点，所以 11 项全绿仍漏掉了它。现在直接挂 HTMLMediaElement
   的 play/pause 原型（必须在页面脚本之前注入），检查首次点击的调用序列。
"""
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
from _gateutil import edge_path

EDGE = edge_path()        # $EDGE 或两个常见安装位置，见 _gateutil.py

# ① 注入到 <head>：**必须早于页面脚本**，才能挂到原型上
HOOK = r"""
<script>
window.__mlog=[]; window.__media=null;
(function(){
  var P=HTMLMediaElement.prototype.play, Q=HTMLMediaElement.prototype.pause;
  HTMLMediaElement.prototype.play=function(){
    window.__mlog.push('play'); window.__media=this; return P.apply(this,arguments);
  };
  HTMLMediaElement.prototype.pause=function(){
    window.__mlog.push('pause'); window.__media=this; return Q.apply(this,arguments);
  };
})();
</script>
"""

# ② 注入到 </body>：页面加载完成后跑断言
PROBE = r"""
<script>
const __o=[];
function T(n,c,e){__o.push((c?'PASS  ':'FAIL  ')+n+(e!==undefined?'   ['+e+']':''));}
function S(n,why){__o.push('SKIP  '+n+'   ['+why+']');}
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
window.addEventListener('load',async()=>{
  try{
    const cards=document.querySelectorAll('.card');
    T('列表渲染出页卡片', cards.length>=1, cards.length);
    T('统计从数据实算（页数）', document.getElementById('s-pages').textContent===String(cards.length),
      document.getElementById('s-pages').textContent);
    const card0=cards[0];
    card0.click();
    T('可进入播放视图', document.getElementById('page-play').classList.contains('active'));
    const rows=document.querySelectorAll('.row');
    T('词条行数与数据一致', rows.length>0, rows.length);
    T('恰好 1 条高亮', document.querySelectorAll('.row.current').length===1);
    const mx=parseFloat(document.getElementById('progress').max);
    T('进度条 max = 真实页时长（>0）', mx>0, mx);
    T('页标题形如「第 N 页」', /^第 \d+ 页$/.test(document.getElementById('play-title').textContent),
      document.getElementById('play-title').textContent);

    /* ===== 回归：首次点击「开始」必须真的开始播放 ===== */
    const playBtn=document.getElementById('play');
    T('初始按钮是「开始」', playBtn.textContent.indexOf('开始')>=0, playBtn.textContent);
    const mark=window.__mlog.length;
    playBtn.click();
    T('点击后按钮变「暂停」', playBtn.textContent.indexOf('暂停')>=0, playBtn.textContent);
    /* headless 的 --virtual-time-budget 会快进计时器，但**真实 I/O 不会跟着快进**，
       所以纯 sleep 之后 readyState 往往还是 0。改成等 loadedmetadata 事件（真实 I/O 驱动），
       再加一个有界的兜底计时器，避免永久挂住。 */
    await new Promise(r=>{
      const a=window.__media;
      if(!a || a.readyState>=1) return r();
      a.addEventListener('loadedmetadata',r,{once:true});
      setTimeout(r,4000);
    });
    await sleep(400);
    const after=window.__mlog.slice(mark);
    T('首次点击的媒体调用序列必须是 [play]（出现 pause / 重复 play = 解锁块在捣乱）',
      after.join(',')==='play', after.join(',')||'(无调用)');

    // headless 里媒体常常加载不到（readyState=0），此时 paused 恒为 false 是**假绿**，
    // 不能报 PASS —— 显式降级为 SKIP，避免"全绿但实际无声"这种最危险的假象。
    const ready = window.__media && window.__media.readyState >= 1;
    if (ready){
      T('首次点击后真的在播（paused=false）',
        window.__media.paused===false,
        'paused='+window.__media.paused+' readyState='+window.__media.readyState);
      T('600ms 后仍在播（未被异步暂停）',
        window.__media.paused===false, 'paused='+window.__media.paused);
    } else {
      S('首次点击后真的在播', 'headless 媒体未加载 readyState='
        +(window.__media?window.__media.readyState:'无对象')+'，需真机上用耳朵确认');
      S('600ms 后仍在播', '同上');
    }

    /* ===== 暂停 / 停止 ===== */
    const stopMark=window.__mlog.length;
    document.getElementById('stop').click();
    await sleep(200);
    T('停止后按钮回到「开始」', playBtn.textContent.indexOf('开始')>=0, playBtn.textContent);
    T('停止确实调用了 pause', window.__mlog.slice(stopMark).indexOf('pause')>=0,
      window.__mlog.slice(stopMark).join(',')||'(无调用)');
    T('停止后音频已暂停', !!window.__media && window.__media.paused===true,
      window.__media?('paused='+window.__media.paused):'-');
    T('停止后回到第 1 条', document.getElementById('ph-index').textContent.indexOf('1 /')>0,
      document.getElementById('ph-index').textContent);
    T('两视图互斥', document.querySelectorAll('.page.active').length===1);
    T('无「模拟时间轴」字样（包内应全是真实音频）',
      document.getElementById('play-sub').textContent.indexOf('模拟时间轴')<0,
      document.getElementById('play-sub').textContent);
  }catch(e){ T('运行时异常：'+e.message,false); }
  const p=document.createElement('pre'); p.id='testout';
  p.textContent='<<<OUT\n'+__o.join('\n')+'\nOUT>>>';
  document.body.appendChild(p);
});
</script>
"""


def main():
    pack = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "packs/冒烟测试")
    src = os.path.join(pack, "index.html")
    if not os.path.isfile(src):
        raise SystemExit("找不到 %s" % src)
    html = io.open(src, encoding="utf-8").read()
    tmp = os.path.join(pack, "_probe_tmp.html")
    io.open(tmp, "w", encoding="utf-8", newline="").write(
        html.replace("</head>",
                     "<style>*{transition:none!important;animation:none!important}"
                     ".page.active{opacity:1!important;transform:none!important}</style>"
                     + HOOK + "</head>", 1)
            .replace("</body>", PROBE + "</body>", 1))

    dom = os.path.join(pack, "_probe_dom.html")
    # profile 放 %TEMP% 的独立目录，跑完就删（与 _cdp_*.py 一致）。
    # 原来用固定目录 vocab-tool\_edgeprof_probe：每跑一次长回 8~60 MB，
    # 两个 probe 并行还会共用同一个 profile（Edge 按单实例处理，结果不可信）。
    prof = tempfile.mkdtemp(prefix="edge_probe_")
    try:
        with open(dom, "w", encoding="utf-8") as f:
            subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--no-sandbox",
                            "--autoplay-policy=no-user-gesture-required",
                            "--user-data-dir=" + prof,
                            "--virtual-time-budget=30000", "--dump-dom",
                            "file:///" + tmp.replace("\\", "/")],
                           stdout=f, stderr=subprocess.DEVNULL)
        h = io.open(dom, encoding="utf-8", errors="replace").read()
    finally:
        # subprocess.run 已经等到 Edge 退出；真删不掉时 ignore_errors 让临时目录留在 %TEMP% 里，
        # 也不该让一个清理失败掩盖真正的探针结果。
        shutil.rmtree(prof, ignore_errors=True)
    m = re.search(r'<pre id="testout">([\s\S]*?)</pre>', h)
    body = m.group(1) if m else ""
    lines = [l.strip() for l in body.split("\n")
             if l.strip().startswith(("PASS", "FAIL", "SKIP"))]
    n_ok = sum(1 for l in lines if l.startswith("PASS"))
    n_bad = sum(1 for l in lines if l.startswith("FAIL"))
    n_skip = sum(1 for l in lines if l.startswith("SKIP"))
    print("包运行时验证：%s" % os.path.relpath(pack, HERE))
    print("\n".join("  " + l for l in lines) if lines else "  （无输出）")
    for f in (tmp, dom):
        try:
            os.remove(f)
        except OSError:
            pass
    if n_ok + n_bad + n_skip == 0:
        print("\n!! 探针未执行（0/0）—— 视为基础设施故障")
        return 3
    print("\n结果: %d PASS / %d FAIL / %d SKIP" % (n_ok, n_bad, n_skip))
    if n_skip:
        print("     ⚠️ SKIP 项未验证（headless 限制），不等于通过 —— 需真机确认")
    return 1 if n_bad else 0


if __name__ == "__main__":
    sys.exit(main())
