/* 逐词播放追踪：验证「点某条 / 上一词下一词 / 暂停恢复」是否都精确落在目标词上。
   被 _cdp_inject.py 以「外链经典脚本」方式注入，结果写入 #__trace。 */
(function () {
  var out = [];
  function P(s) { out.push(s); }
  function done() {
    var el = document.getElementById('__trace');
    if (!el) { el = document.createElement('pre'); el.id = '__trace'; document.body.appendChild(el); }
    el.textContent = out.join('\n');
    window.__traceDone = true;
  }
  window.onerror = function (m, f, l, c) { P('JS错误: ' + m + ' @行' + l + ' 列' + c); done(); };

  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var num = function (v) { return (typeof v === 'number' && isFinite(v)) ? v.toFixed(2) : String(v); };
  var srcName = function () {
    try { return String(audio.currentSrc || audio.src || '').split('/').pop() || '-'; }
    catch (e) { return '?'; }
  };

  function st() {
    try {
      return 'ct=' + num(audio.currentTime)
        + ' idx=' + idx + ' playing=' + playing + ' mode=' + mode
        + ' 文件=' + srcName();
    } catch (e) { return '读取失败: ' + e.message; }
  }
  function rowIdx() {
    var rows = document.querySelectorAll('.row'), n = -1;
    for (var i = 0; i < rows.length; i++) if (rows[i].classList.contains('current')) n = i;
    return n;
  }

  window.addEventListener('load', function () {
    setTimeout(async function () {
      try {
        P('自检: typeof tracks=' + (typeof tracks) + ' 条数=' + (tracks || []).length
          + ' 首个url=' + ((tracks && tracks[0] && tracks[0].url) || '无'));
        P('顶栏版本 = ' + (((document.getElementById('ver') || {}).textContent) || '(无)'));

        document.querySelectorAll('.card')[0].click();
        await sleep(400);
        P('A 进页 高亮=' + rowIdx() + ' ' + st());

        document.getElementById('play').click();
        for (var t = 1; t <= 8; t++) {
          await sleep(1000);
          P('B 播' + t + 's 高亮=' + rowIdx() + ' ' + st());
        }

        P('=== 暂停，按「下一词」两次，再播放（用户质疑的场景）===');
        document.getElementById('play').click();
        await sleep(400);
        var atPause = rowIdx();
        P('C 暂停 高亮=' + atPause + ' ' + st());
        document.getElementById('next').click();
        await sleep(300);
        P('C2 下一词 高亮=' + rowIdx() + ' ' + st());
        document.getElementById('next').click();
        await sleep(300);
        var sel = rowIdx();
        P('C3 再下一词 高亮=' + sel + ' ' + st());
        document.getElementById('play').click();
        for (var k = 0; k < 5; k++) {
          await sleep(300);
          P('D +' + ((k + 1) * 0.3).toFixed(1) + 's 高亮=' + rowIdx() + ' ' + st());
        }
        P('期望：播放中高亮应始终 = ' + sel + '（不漂移到别的词）');

        P('=== 暂停时点第 30 行，再播放 ===');
        document.getElementById('play').click();
        await sleep(300);
        document.querySelectorAll('.row')[29].click();
        await sleep(300);
        P('E 点第30行 高亮=' + rowIdx() + ' ' + st());
        for (var j = 0; j < 4; j++) {
          await sleep(300);
          P('E+' + ((j + 1) * 0.3).toFixed(1) + 's 高亮=' + rowIdx() + ' ' + st());
        }
      } catch (e) { P('运行异常: ' + e.message); }
      done();
    }, 120);
  });
})();
