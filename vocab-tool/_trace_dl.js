/* 验证下载对话框：能打开、文件名可改、保存走对了分支且不报错。
   被 _cdp_inject.py 注入，结果写入 #__trace。 */
(function () {
  var out = [];
  function P(s) { out.push(s); }
  function done() {
    var el = document.getElementById('__trace');
    if (!el) { el = document.createElement('pre'); el.id = '__trace'; document.body.appendChild(el); }
    el.textContent = out.join('\n');
    window.__traceDone = true;
  }
  window.onerror = function (m, f, l, c) { P('JS错误: ' + m + ' @行' + l + ' c' + c); done(); };

  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };

  window.addEventListener('load', function () {
    setTimeout(async function () {
      try {
        P('showSaveFilePicker 可用 = ' + (typeof window.showSaveFilePicker === 'function'));
        document.querySelectorAll('.card')[0].click();
        await sleep(300);

        var dlg = document.getElementById('dlg');
        P('点击前对话框显示 = ' + dlg.classList.contains('show'));
        document.getElementById('dl').click();
        await sleep(200);
        P('点击后对话框显示 = ' + dlg.classList.contains('show'));
        P('标题 = ' + document.getElementById('dlg-title').textContent);
        P('默认文件名 = ' + document.getElementById('dl-name').value);
        P('提示文案含 file:// 说明 = ' + /file:\/\//.test(document.getElementById('dlg-note').textContent));
        P('提示文案含 localhost 说明 = ' + /localhost/.test(document.getElementById('dlg-note').textContent));

        // 改名（含空格，检验键位守卫：空格不该触发播放/暂停）
        var inp = document.getElementById('dl-name');
        inp.value = '我的词汇 第1页.mp3';
        P('改后文件名 = ' + inp.value);

        // 拦截 <a>.click，确认降级分支真的带着自定义名字触发
        var origClick = HTMLAnchorElement.prototype.click, captured = null;
        HTMLAnchorElement.prototype.click = function () {
          captured = { href: this.getAttribute('href'), download: this.getAttribute('download') };
        };
        // 关掉 showSaveFilePicker 强制走降级（模拟 file:// 场景）
        var had = window.showSaveFilePicker;
        try { delete window.showSaveFilePicker; } catch (e) { window.showSaveFilePicker = undefined; }
        document.getElementById('dl-ok').click();
        await sleep(300);
        HTMLAnchorElement.prototype.click = origClick;
        if (had) window.showSaveFilePicker = had;

        P('降级下载捕获 = ' + (captured ? ('name=' + captured.download + ' | href=' + captured.href) : '未触发'));
        P('保存后对话框关闭 = ' + !dlg.classList.contains('show'));
        P('localStorage 记住了名字 = ' + (function () {
          try { return localStorage.getItem('pet_dl_filename'); } catch (e) { return '(不可用)'; }
        })());

        // 再开一次，应回填上次的名字
        document.getElementById('dl').click();
        await sleep(150);
        P('二次打开回填 = ' + document.getElementById('dl-name').value);

        // 取消按钮
        document.getElementById('dl-cancel').click();
        await sleep(100);
        P('取消后关闭 = ' + !dlg.classList.contains('show'));

        // 播放/暂停是否被输入框抢键影响
        P('当前 playing = ' + playing + '（改文件名不应触发播放）');
      } catch (e) { P('运行异常: ' + e.message); }
      done();
    }, 120);
  });
})();
